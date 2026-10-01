"""付款额度预留指令（先预留、后确认付款）：端到端集成测试。

pgserver 便携 PostgreSQL，无需 Docker。
运行: PYTHONPATH=. python3 tests/integration_reservations.py

场景按权利人/作品隔离，避免共享单据累计造成数字纠缠：
- 作品 W（A 60% / B 40%）：创建/幂等/冲突/校验/取消/负额追补阻止确认/退回/恢复后确认
- 作品 W2（C 100%）：负额追补 FIFO（更早创建的待确认指令优先受保护）
- 作品 W3（D 100%）：并发创建、并发确认 + 并发直接付款不超付
- 作品 W4（E 100%）：同指令并发确认序列化、取消 vs 确认竞态互斥
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rsv_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    period = {"start": "2026-02-01T00:00:00+08:00",
              "end": "2026-03-01T00:00:00+08:00"}

    def setup_work(code, shares):
        """登记作品 + 权利人和协议（shares: {holder: percent-int}）。"""
        c.post("/works", json={"code": code, "title": code})
        for h in shares:
            c.post("/rightsholders", json={"code": h, "name": h})
        c.post("/agreements", json={
            "work_code": code,
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": h, "percent": str(p)}
                       for h, p in shares.items()]})

    def play_and_issue(import_id, work, amount, corrects=None):
        r = c.post("/plays/import", json={
            "import_id": import_id, "work_code": work,
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": amount,
            "corrects_id": corrects})
        assert r.status_code in (200, 201), r.text
        r = c.post("/statements/issue", json=period)
        assert r.status_code in (200, 201), r.text
        return r.json()

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        setup_work("W", {"A": "60", "B": "40"})
        setup_work("W2", {"C": "100"})
        setup_work("W3", {"D": "100"})
        setup_work("W4", {"E": "100"})

        # =================================================================
        # 一、W / 权利人 A：创建、幂等、冲突、校验、取消、确认、退回、追补
        # =================================================================
        play_and_issue("P1", "W", 100)   # A 60 / B 40

        # 1. 创建预留：余额 60，预留 50 => 可用 10，余额不变
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "R1", "amount": 50})
        b = r.json()
        check("reserve created 201",
              r.status_code == 201 and b["status"] == "created"
              and b["instruction_status"] == "pending"
              and b["balance"] == 60 and b["reserved_total"] == 50
              and b["available_balance"] == 10 and b["instruction_id"] >= 1, r.text)
        r1_id = b["instruction_id"]

        # GET 原指令 / 不存在 404
        r = c.get("/payment-reservations/R1")
        check("get reservation", r.status_code == 200
              and r.json()["instruction_status"] == "pending"
              and r.json()["available_balance"] == 10, r.text)
        r = c.get("/payment-reservations/NOPE")
        check("get missing 404",
              r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)

        # 余额扣除全部待确认预留后不足：再预留 11（可用仅 10）=> 拒绝
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "R2", "amount": 11})
        check("reserve over available rejected",
              r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["available"] == 10
              and r.json()["detail"]["reserved_total"] == 50
              and r.json()["detail"]["balance"] == 60, r.text)
        # 恰好等于可用额 10 => 成功，可用额归 0
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "R2", "amount": 10})
        check("reserve exact available 201",
              r.status_code == 201 and r.json()["available_balance"] == 0, r.text)

        # 被拒的 11 分不留任何额外预留（R2 现存的是金额 10 的成功指令）
        led = c.get("/rightsholders/A/ledger").json()
        check("rejected reserve leaves no trace",
              len(led["instructions"]) == 2 and led["reserved_total"] == 60
              and led["balance"] == 60 and led["available_balance"] == 0
              and led["paid_total"] == 0, str(led))

        # 2. 新付款必须避开待确认预留：可用额 0 时即使余额 60 也拒绝直接付款
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "PDIRECT-1", "amount": 1})
        check("direct pay avoids reservations",
              r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["available"] == 0
              and r.json()["detail"]["reserved_total"] == 60, r.text)
        check("direct pay rejected leaves nothing",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 0)

        # 3. 同号同内容重试 => 原指令 duplicate（200），不新增预留
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "R1", "amount": 50})
        check("reserve idempotent retry",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["instruction_id"] == r1_id
              and r.json()["reserved_total"] == 60, r.text)
        check("retry no double reserve",
              len(c.get("/rightsholders/A/ledger").json()["instructions"]) == 2)

        # 同号异内容（金额/权利人不同）=> 409
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "R1", "amount": 49})
        check("reserve conflict amount",
              r.status_code == 409
              and r.json()["error"] == "reservation_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 50
              and r.json()["detail"]["existing_status"] == "pending", r.text)
        r = c.post("/payment-reservations", json={
            "holder_code": "B", "instruction_ref": "R1", "amount": 50})
        check("reserve conflict holder",
              r.status_code == 409
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)

        # 权利人不存在 / 金额非法
        r = c.post("/payment-reservations", json={
            "holder_code": "NOBODY", "instruction_ref": "RX", "amount": 1})
        check("reserve holder missing 404",
              r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        for bad in (0, -1, 1.5, True):
            r = c.post("/payment-reservations", json={
                "holder_code": "A", "instruction_ref": f"RB-{bad}", "amount": bad})
            check(f"reserve bad amount {bad!r} 422", r.status_code == 422, r.text)

        # 4. 取消 R2（10）=> 释放额度，可用额回到 10；重复取消幂等
        r = c.post("/payment-reservations/R2/cancel")
        b = r.json()
        check("cancel pending 200",
              r.status_code == 200 and b["status"] == "cancelled"
              and b["instruction_status"] == "cancelled"
              and b["cancelled_at"] is not None
              and b["reserved_total"] == 50 and b["available_balance"] == 10, r.text)
        r = c.post("/payment-reservations/R2/cancel")
        check("cancel idempotent",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["instruction_status"] == "cancelled", r.text)
        r = c.post("/payment-reservations/NOPE/cancel")
        check("cancel missing 404",
              r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)

        # 取消释放后直接付款 10 可用（避开剩余预留 R1=50）；
        # 付款后账面余额 50（与 POST /payments 兼容：balance_after 为余额）
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "PDIRECT-2", "amount": 10})
        check("direct pay after cancel ok",
              r.status_code == 201 and r.json()["status"] == "created"
              and r.json()["balance_after"] == 50, r.text)

        # 5. 负额追补冲减余额：100 -> 50，A 单据 60 -> 30，余额 30-10=20
        play_and_issue("P1R1", "W", 50, corrects="P1")
        led = c.get("/rightsholders/A/ledger").json()
        check("negative supplement hits A",
              led["billed_total"] == 30 and led["balance"] == 20
              and led["reserved_total"] == 50 and led["available_balance"] == -30,
              str(led))

        # 确认 R1（50）被拒：余额 20 不足"更早待确认 0 + 本指令 50"，
        # 指令保持 pending、不留付款
        r = c.post("/payment-reservations/R1/confirm",
                   json={"external_ref": "BANK-C-R1"})
        check("confirm blocked while balance short",
              r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["required"] == 50
              and r.json()["detail"]["balance"] == 20
              and r.json()["detail"]["earlier_pending_total"] == 0, r.text)
        check("R1 still pending after reject",
              c.get("/payment-reservations/R1").json()["instruction_status"]
              == "pending")
        check("rejected confirm no payment",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 10
              and "BANK-C-R1" not in [
                  p["external_ref"]
                  for p in c.get("/rightsholders/A/ledger").json()["payments"]])

        # 6. 退回加回余额：PDIRECT-2 全额退回 10 => 余额 30；仍不足 50
        r = c.post("/refunds", json={
            "payment_ref": "PDIRECT-2",
            "refund_ref": "REF-1", "amount": 10})
        check("refund restores balance",
              r.status_code == 201 and r.json()["balance_after"] == 30, r.text)
        r = c.post("/payment-reservations/R1/confirm",
                   json={"external_ref": "BANK-C-R1"})
        check("confirm still short after refund",
              r.status_code == 409
              and r.json()["detail"]["balance"] == 30, r.text)

        # 7. 恢复正额：50 -> 100（A 60），余额 = 60 - 10 + 10(退回) = 60
        play_and_issue("P1R2", "W", 100, corrects="P1R1")
        check("balance recovered to 60",
              c.get("/rightsholders/A/ledger").json()["balance"] == 60)
        r = c.post("/payment-reservations/R1/confirm",
                   json={"external_ref": "BANK-C-R1"})
        b = r.json()
        check("R1 confirmed after recovery",
              r.status_code == 201 and b["status"] == "created"
              and b["payment_id"] >= 1 and b["amount"] == 50
              and b["holder_code"] == "A"
              and b["balance_after"] == 10
              and b["instruction_status"] == "confirmed"
              and b["instruction_ref"] == "R1", r.text)
        check("R1 payment registered atomically",
              "BANK-C-R1" in [p["external_ref"]
                              for p in c.get("/rightsholders/A/ledger")
                              .json()["payments"]])

        # 同流水号确认重试 => 原付款 duplicate（200），不重复登记
        paid_before = c.get("/rightsholders/A/ledger").json()["paid_total"]
        r = c.post("/payment-reservations/R1/confirm",
                   json={"external_ref": "BANK-C-R1"})
        check("confirm idempotent same ref",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["external_ref"] == "BANK-C-R1"
              and r.json()["payment_id"] == b["payment_id"], r.text)
        check("retry no double payment",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == paid_before)

        # 已确认指令换流水号 => 409 reservation_already_confirmed
        r = c.post("/payment-reservations/R1/confirm",
                   json={"external_ref": "BANK-OTHER"})
        check("confirmed diff ref rejected",
              r.status_code == 409
              and r.json()["error"] == "reservation_already_confirmed"
              and r.json()["detail"]["existing_external_ref"] == "BANK-C-R1",
              r.text)
        # 已确认/已取消指令不可取消
        r = c.post("/payment-reservations/R1/cancel")
        check("cancel confirmed rejected",
              r.status_code == 409
              and r.json()["error"] == "reservation_not_pending", r.text)

        # 8. pending 指令使用"已被占用的流水号"确认 => 拒绝且指令不变
        # A 当前余额 10、无预留：OCC=5 创建成功
        r = c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "OCC", "amount": 5})
        check("OCC created", r.status_code == 201 and r.json()["available_balance"] == 5,
              r.text)
        # BANK-C-R1 已被 R1 的付款占用
        r = c.post("/payment-reservations/OCC/confirm",
                   json={"external_ref": "BANK-C-R1"})
        check("occupied ref rejected",
              r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["existing_payment_id"] == b["payment_id"],
              r.text)
        check("OCC still pending after occupied",
              c.get("/payment-reservations/OCC").json()["instruction_status"]
              == "pending")
        # 直接付款先占用新流水号（5 在可用额 5 内），指令再用同号 => 拒绝
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "DIRECT-OCC", "amount": 5})
        check("direct 5 fits available", r.status_code == 201
              and r.json()["balance_after"] == 5, r.text)
        r = c.post("/payment-reservations/OCC/confirm",
                   json={"external_ref": "DIRECT-OCC"})
        check("direct-occupied ref rejected",
              r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict", r.text)
        check("OCC still pending after direct occupied",
              c.get("/payment-reservations/OCC").json()["instruction_status"]
              == "pending")
        # 换未占用流水号确认成功（此时余额 5，本指令 5 + 更早 0 => 恰好）
        r = c.post("/payment-reservations/OCC/confirm",
                   json={"external_ref": "BANK-OCC"})
        check("OCC confirmed with free ref",
              r.status_code == 201 and r.json()["status"] == "created"
              and r.json()["balance_after"] == 0, r.text)

        # 已取消指令确认 => 409；不存在指令确认 => 404；空流水号 => 422
        r = c.post("/payment-reservations/R2/confirm",
                   json={"external_ref": "X"})
        check("confirm cancelled rejected",
              r.status_code == 409
              and r.json()["error"] == "reservation_not_pending", r.text)
        r = c.post("/payment-reservations/NOPE/confirm",
                   json={"external_ref": "X"})
        check("confirm missing 404",
              r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)
        r = c.post("/payment-reservations/OCC/confirm", json={"external_ref": ""})
        check("confirm empty ref 422", r.status_code == 422, r.text)

        # 9. 对账单可见待确认预留、可用额与指令状态（留一笔 VIEW pending）
        # 先让 A 再生余额：100 -> 120（A 72），余额增加 12
        play_and_issue("P1R3", "W", 120, corrects="P1R2")
        c.post("/payment-reservations", json={
            "holder_code": "A", "instruction_ref": "VIEW", "amount": 3})
        led = c.get("/rightsholders/A/ledger").json()
        statuses = {i["instruction_ref"]: i["status"]
                    for i in led["instructions"]}
        check("ledger instructions view",
              statuses == {
                  "R1": "confirmed", "R2": "cancelled",
                  "OCC": "confirmed", "VIEW": "pending"}
              and led["reserved_total"] == 3
              and led["available_balance"] == led["balance"] - 3, str(statuses))
        r1_view = next(i for i in led["instructions"]
                       if i["instruction_ref"] == "R1")
        check("confirmed instruction bound",
              r1_view["external_ref"] == "BANK-C-R1"
              and r1_view["payment_id"] is not None
              and r1_view["confirmed_at"] is not None, str(r1_view))

        # 10. 指令历史不可改写（触发器：pending 内容/状态、终态、删除全部拒绝）
        with db.pool.connection() as db_conn:
            view_id = db_conn.execute(
                "SELECT id FROM payment_instructions WHERE instruction_ref='VIEW'"
            ).fetchone()["id"]
            denied = []
            for sql, args in (
                ("UPDATE payment_instructions SET amount=1 WHERE id=%s", (view_id,)),
                ("UPDATE payment_instructions SET status='confirmed' WHERE id=%s",
                 (view_id,)),
                ("UPDATE payment_instructions SET external_ref='X' WHERE id=%s",
                 (view_id,)),
                ("DELETE FROM payment_instructions WHERE id=%s", (view_id,)),
            ):
                try:
                    db_conn.execute(sql, args)
                    denied.append(False)
                except psycopg.Error:
                    denied.append(True)
        check("instruction frozen", all(denied), str(denied))
        check("VIEW still pending after attempts",
              c.get("/payment-reservations/VIEW").json()["instruction_status"]
              == "pending")
        with db.pool.connection() as db_conn:
            r2_id = db_conn.execute(
                "SELECT id FROM payment_instructions WHERE instruction_ref='R2'"
            ).fetchone()["id"]
            try:
                db_conn.execute(
                    "UPDATE payment_instructions SET amount=1 WHERE id=%s", (r2_id,))
                term_frozen = False
            except psycopg.Error:
                term_frozen = True
        check("terminal instruction frozen", term_frozen)

        # =================================================================
        # 二、W2 / 权利人 C：负额追补下的 FIFO 预留保护
        # =================================================================
        play_and_issue("P2", "W2", 100)             # C 100
        # 更早 EARLY=40、更晚 LATE=20，预留后可用 40
        r = c.post("/payment-reservations", json={
            "holder_code": "C", "instruction_ref": "EARLY", "amount": 40})
        check("C EARLY created", r.status_code == 201, r.text)
        r = c.post("/payment-reservations", json={
            "holder_code": "C", "instruction_ref": "LATE", "amount": 20})
        check("C LATE created", r.status_code == 201
              and r.json()["reserved_total"] == 60
              and r.json()["available_balance"] == 40, r.text)
        # 超出可用额的第三笔预留被拒
        r = c.post("/payment-reservations", json={
            "holder_code": "C", "instruction_ref": "TOOBIG", "amount": 41})
        check("C third reserve rejected", r.status_code == 409
              and r.json()["detail"]["available"] == 40, r.text)

        # 负额追补：100 -> 50，C 余额 50
        play_and_issue("P2R1", "W2", 50, corrects="P2")
        check("C balance 50",
              c.get("/rightsholders/C/ledger").json()["balance"] == 50)

        # 更晚的 LATE 即使自身 20 <= 50，也必须为更早 EARLY(40) 留足：
        # required = 40 + 20 = 60 > 50 => 拒绝，指令不变
        r = c.post("/payment-reservations/LATE/confirm",
                   json={"external_ref": "BANK-LATE"})
        check("FIFO protects earlier pending",
              r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["earlier_pending_total"] == 40
              and r.json()["detail"]["required"] == 60
              and r.json()["detail"]["balance"] == 50, r.text)
        check("LATE unchanged pending",
              c.get("/payment-reservations/LATE").json()["instruction_status"]
              == "pending")

        # 更早 EARLY：required 40 <= 50 => 恰好确认，余额 10
        r = c.post("/payment-reservations/EARLY/confirm",
                   json={"external_ref": "BANK-EARLY"})
        check("EARLY confirmed exactly",
              r.status_code == 201 and r.json()["balance_after"] == 10, r.text)
        # EARLY 走掉后 LATE 仍待确认：余额 10 < 20 => 拒绝
        r = c.post("/payment-reservations/LATE/confirm",
                   json={"external_ref": "BANK-LATE"})
        check("LATE rejected after EARLY consumed",
              r.status_code == 409
              and r.json()["detail"]["earlier_pending_total"] == 0
              and r.json()["detail"]["required"] == 20, r.text)

        # 恢复正额：50 -> 100，C 余额 100-40=60，LATE 确认成功
        play_and_issue("P2R2", "W2", 100, corrects="P2R1")
        check("C balance 60",
              c.get("/rightsholders/C/ledger").json()["balance"] == 60)
        r = c.post("/payment-reservations/LATE/confirm",
                   json={"external_ref": "BANK-LATE"})
        check("LATE confirmed after recovery",
              r.status_code == 201 and r.json()["balance_after"] == 40, r.text)
        led = c.get("/rightsholders/C/ledger").json()
        c_statuses = {i["instruction_ref"]: i["status"]
                      for i in led["instructions"]}
        check("C ledger settled",
              led["billed_total"] == 100 and led["paid_total"] == 60
              and led["balance"] == 40 and led["reserved_total"] == 0
              and c_statuses == {"EARLY": "confirmed", "LATE": "confirmed"}
              and "TOOBIG" not in c_statuses, str(led))

        # =================================================================
        # 三、W3 / 权利人 D：并发
        # =================================================================
        play_and_issue("P3", "W3", 100)             # D 100

        # 1) 同指令号同内容 8 并发创建 => 恰 1 笔 created，7 笔 duplicate
        results = []

        def fire_create():
            rr = c.post("/payment-reservations", json={
                "holder_code": "D", "instruction_ref": "DCON", "amount": 20})
            results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_create) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        created = sum(1 for _, x in results if x["status"] == "created")
        dup = sum(1 for _, x in results if x["status"] == "duplicate")
        ids = {x["instruction_id"] for _, x in results}
        check("concurrent same-ref reserve",
              created == 1 and dup == 7 and len(ids) == 1, str(results))
        check("reserved once",
              c.get("/rightsholders/D/ledger").json()["reserved_total"] == 20)

        # 再凑满 100：30 + 25 + 25（与 20 合计 100，可用 0）
        for ref, amt in (("D1", 30), ("D2", 25), ("D3", 25)):
            rr = c.post("/payment-reservations", json={
                "holder_code": "D", "instruction_ref": ref, "amount": amt})
            assert rr.status_code == 201, rr.text
        check("D fully reserved",
              c.get("/rightsholders/D/ledger").json()["reserved_total"] == 100
              and c.get("/rightsholders/D/ledger").json()["available_balance"] == 0)

        # 2) 4 条指令并发确认（不同流水号）+ 5 笔并发直接付款各 1 分：
        #    4 条全部按 FIFO 确认成功（合计 100）；直接付款须全部避开预留而被拒，
        #    paid_total 恰 100、余额 0，绝不超付、无死锁。
        outcomes = {"confirmed": 0, "confirm_rejected": 0,
                    "direct_created": 0, "direct_rejected": 0, "errors": []}
        barrier = threading.Barrier(9)

        def fire_confirm(ref, ext):
            barrier.wait()
            try:
                rr = c.post(f"/payment-reservations/{ref}/confirm",
                            json={"external_ref": ext})
                if rr.status_code == 201:
                    outcomes["confirmed"] += 1
                elif rr.status_code == 409 and \
                        rr.json()["error"] == "payment_exceeds_balance":
                    outcomes["confirm_rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        def fire_direct(i):
            barrier.wait()
            try:
                rr = c.post("/payments", json={
                    "holder_code": "D",
                    "external_ref": f"DDIRECT-{i}", "amount": 1})
                if rr.status_code == 201:
                    outcomes["direct_created"] += 1
                elif rr.status_code == 409:
                    outcomes["direct_rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        threads = [
            threading.Thread(target=fire_confirm, args=("DCON", "DBANK-0")),
            threading.Thread(target=fire_confirm, args=("D1", "DBANK-1")),
            threading.Thread(target=fire_confirm, args=("D2", "DBANK-2")),
            threading.Thread(target=fire_confirm, args=("D3", "DBANK-3")),
        ]
        threads += [threading.Thread(target=fire_direct, args=(i,))
                    for i in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        led = c.get("/rightsholders/D/ledger").json()
        check("concurrent confirms FIFO no overpay",
              not outcomes["errors"]
              and outcomes["confirmed"] == 4
              and outcomes["confirm_rejected"] == 0
              and outcomes["direct_created"] == 0
              and outcomes["direct_rejected"] == 5
              and led["paid_total"] == 100 and led["reserved_total"] == 0
              and led["balance"] == 0, f"{outcomes} {led}")

        # =================================================================
        # 四、W4 / 权利人 E：同指令并发确认、取消 vs 确认竞态
        # =================================================================
        play_and_issue("P4", "W4", 100)             # E 100

        # 同指令同流水号 6 并发确认 => 恰登记一笔付款，其余幂等
        c.post("/payment-reservations", json={
            "holder_code": "E", "instruction_ref": "ECON", "amount": 70})
        e_results = []
        e_barrier = threading.Barrier(6)

        def fire_e_confirm():
            e_barrier.wait()
            rr = c.post("/payment-reservations/ECON/confirm",
                        json={"external_ref": "EBANK-1"})
            e_results.append((rr.status_code, rr.json().get("status"),
                              rr.json().get("payment_id")))

        ts = [threading.Thread(target=fire_e_confirm) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        e_created = [r for r in e_results if r[1] == "created"]
        e_dup = [r for r in e_results if r[1] == "duplicate"]
        check("concurrent same confirm serialized",
              len(e_created) == 1 and len(e_dup) == 5
              and len({r[2] for r in e_results}) == 1, str(e_results))
        check("E paid exactly 70",
              c.get("/rightsholders/E/ledger").json()["paid_total"] == 70
              and c.get("/rightsholders/E/ledger").json()["balance"] == 30)

        # 取消 vs 确认 竞态（10 轮，每轮独立 1 分预留）：
        # 每笔恰一个终态生效——confirmed 且付款登记，或 cancelled 且无付款。
        race_errors = []
        confirm_wins = 0
        for i in range(10):
            ref = f"ERACE-{i}"
            rr = c.post("/payment-reservations", json={
                "holder_code": "E", "instruction_ref": ref, "amount": 1})
            assert rr.status_code == 201, rr.text
            b2 = threading.Barrier(2)
            res = {}

            def rc(ref=ref, b2=b2, res=res):
                b2.wait()
                r2 = c.post(f"/payment-reservations/{ref}/confirm",
                            json={"external_ref": f"EBANK-{ref}"})
                res["confirm"] = r2.status_code

            def rx(ref=ref, b2=b2, res=res):
                b2.wait()
                r2 = c.post(f"/payment-reservations/{ref}/cancel")
                res["cancel"] = (r2.status_code,
                                 r2.json().get("instruction_status"))

            t1 = threading.Thread(target=rc)
            t2 = threading.Thread(target=rx)
            t1.start(); t2.start(); t1.join(); t2.join()
            conf_ok = res.get("confirm") == 201
            canc_ok = res.get("cancel") == (200, "cancelled")
            if not (conf_ok ^ canc_ok):
                race_errors.append(res)
            if conf_ok:
                confirm_wins += 1
        led = c.get("/rightsholders/E/ledger").json()
        check("cancel/confirm race mutually exclusive",
              not race_errors, str(race_errors[:3]))
        check("E no overpay after races",
              led["paid_total"] + led["reserved_total"] <= led["billed_total"]
              and led["paid_total"] == 70 + confirm_wins
              and led["reserved_total"] == 0, str(led))

        # =================================================================
        # 五、建表脚本可重复执行（幂等迁移），历史数据不改写
        # =================================================================
        with db.pool.connection() as db_conn:
            db_conn.execute(db.SCHEMA_SQL)
        check("schema idempotent re-run",
              c.get("/health").json()["status"] == "ok"
              and c.get("/payment-reservations/ECON").json()["instruction_status"]
              == "confirmed"
              and c.get("/rightsholders/A/ledger").json()["reserved_total"] == 3)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL RESERVATION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
