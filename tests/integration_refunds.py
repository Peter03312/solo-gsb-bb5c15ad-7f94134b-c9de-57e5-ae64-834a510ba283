"""银行退票（付款退回）登记：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

覆盖：
- 登记退回（部分/全额）、登记后余额、原付款与单据不可变；
- 退回流水号同内容幂等、异内容冲突；原付款不存在 404；金额非正整数 422；
  累计退回超过原付款金额拒绝且不留记录；
- 对账单保留付款总额、另列退回总额、逐笔退回及余额；
- 退回加回余额后，后续付款仍只允许在正余额内登记；
- 并发：同退回流水号串行、同原付款多笔退回不合计超额、
  退回/付款/出单并发余额校验一致且无死锁；
- 回单核验：有效回单仍按原付款字段判定；导入/复核冻结累计退回额，
  仅结论或退回额变化才产生新版本，旧版不改写，重复回单不参与核验。

运行: PYTHONPATH=. python3 tests/integration_refunds.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rfd_")) / "pgdata"
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

    def refund(payment_ref, refund_ref, amount):
        return c.post("/refunds", json={
            "payment_ref": payment_ref,
            "refund_ref": refund_ref,
            "amount": amount})

    def pay(holder, ref, amount):
        return c.post("/payments", json={
            "holder_code": holder, "external_ref": ref, "amount": amount})

    def import_batch(batch_no, receipts):
        return c.post("/receipts/import",
                      json={"batch_no": batch_no, "receipts": receipts})

    def rline(receipt_no, ref, holder, amount):
        return {"receipt_no": receipt_no, "external_ref": ref,
                "holder_code": holder, "amount": amount}

    period = {"start": "2026-02-01T00:00:00+08:00",
              "end": "2026-03-01T00:00:00+08:00"}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 + 权利人 A/B，协议 60/40，播放 100 => A 60 / B 40 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=period)

        # ---- 拒绝类：原付款不存在 / 金额非正整数 ----
        r = refund("NOPE-PAY", "RF-NOPE", 1)
        check("original payment missing 404", r.status_code == 404
              and r.json()["error"] == "payment_not_found"
              and r.json()["detail"]["payment_ref"] == "NOPE-PAY", r.text)
        for i, bad_amount in enumerate([0, -1, 1.5, True, "10"]):
            r = refund("PAY-A", f"RF-BAD-{i}", bad_amount)
            check(f"amount invalid 422 #{i}", r.status_code == 422, r.text)
        r = c.post("/refunds", json={"payment_ref": "", "refund_ref": "X", "amount": 1})
        check("empty payment_ref 422", r.status_code == 422, r.text)
        r = c.post("/refunds", json={"payment_ref": "PAY-A", "refund_ref": "", "amount": 1})
        check("empty refund_ref 422", r.status_code == 422, r.text)
        # 拒绝不留任何退回
        led = c.get("/rightsholders/A/ledger").json()
        check("rejected refunds leave nothing",
              led["refunded_total"] == 0 and led["refunds"] == [])

        # ---- A 付清 60，登记两笔退回 20 + 10 ----
        r = pay("A", "PAY-A", 60)
        check("A pay 60", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)

        r = refund("PAY-A", "RF-A-1", 20)
        body = r.json()
        check("partial refund 201", r.status_code == 201
              and body["status"] == "created"
              and body["payment_ref"] == "PAY-A"
              and body["refund_ref"] == "RF-A-1"
              and body["amount"] == 20
              and body["balance_after"] == 20
              and body["refund_id"] >= 1 and body["payment_id"] >= 1, r.text)
        first_refund_id = body["refund_id"]

        led = c.get("/rightsholders/A/ledger").json()
        check("ledger after first refund",
              led["billed_total"] == 60 and led["paid_total"] == 60
              and led["refunded_total"] == 20 and led["balance"] == 20
              and len(led["payments"]) == 1 and len(led["refunds"]) == 1
              and led["refunds"][0] == {
                  "refund_id": first_refund_id,
                  "refund_ref": "RF-A-1",
                  "payment_id": body["payment_id"],
                  "payment_ref": "PAY-A",
                  "amount": 20,
                  "registered_at": led["refunds"][0]["registered_at"]},
              str(led))

        r = refund("PAY-A", "RF-A-2", 10)
        check("second refund balance 30", r.status_code == 201
              and r.json()["balance_after"] == 30, r.text)

        # ---- 同退回流水号同内容重试：幂等返回原记录（当前余额），不重复入账 ----
        r = refund("PAY-A", "RF-A-1", 20)
        body2 = r.json()
        check("refund idempotent retry", r.status_code == 200
              and body2["status"] == "duplicate"
              and body2["refund_id"] == first_refund_id
              and body2["payment_ref"] == "PAY-A"
              and body2["amount"] == 20
              and body2["balance_after"] == 30, r.text)
        led = c.get("/rightsholders/A/ledger").json()
        check("retry no double refund",
              led["refunded_total"] == 30 and len(led["refunds"]) == 2
              and led["balance"] == 30, str(led))

        # ---- 同退回流水号异内容（原付款流水号不同 / 金额不同）=> 409 ----
        r = refund("PAY-A", "RF-A-1", 19)
        check("refund conflict amount", r.status_code == 409
              and r.json()["error"] == "refund_ref_conflict"
              and r.json()["detail"]["existing_payment_ref"] == "PAY-A"
              and r.json()["detail"]["existing_amount"] == 20, r.text)
        # 先给 B 登一笔付款，作为异内容的原付款流水号
        pay("B", "PAY-B", 40)
        r = refund("PAY-B", "RF-A-1", 20)
        check("refund conflict payment_ref", r.status_code == 409
              and r.json()["error"] == "refund_ref_conflict"
              and r.json()["detail"]["existing_payment_ref"] == "PAY-A",
              r.text)
        check("conflict leaves no refund",
              c.get("/rightsholders/A/ledger").json()["refunded_total"] == 30
              and c.get("/rightsholders/B/ledger").json()["refunded_total"] == 0)

        # ---- 累计退回超过原付款金额：拒绝且不留记录 ----
        # PAY-A 已退 30，再退 31 => 累计 61 > 60
        r = refund("PAY-A", "RF-A-OVER", 31)
        check("cumulative over refund rejected", r.status_code == 409
              and r.json()["error"] == "refund_exceeds_payment"
              and r.json()["detail"]["payment_amount"] == 60
              and r.json()["detail"]["already_refunded"] == 30
              and r.json()["detail"]["requested_amount"] == 31, r.text)
        led = c.get("/rightsholders/A/ledger").json()
        check("over refund leaves nothing",
              led["refunded_total"] == 30 and len(led["refunds"]) == 2
              and led["balance"] == 30, str(led))
        # 恰好退到原付款额（30/60）=> 全额退清
        r = refund("PAY-A", "RF-A-FULL", 30)
        check("exact full refund accepted", r.status_code == 201
              and r.json()["balance_after"] == 60, r.text)
        # 已全额退清，再退 1 分也拒绝
        r = refund("PAY-A", "RF-A-ONE", 1)
        check("fully refunded rejects more", r.status_code == 409
              and r.json()["error"] == "refund_exceeds_payment"
              and r.json()["detail"]["already_refunded"] == 60, r.text)

        # ---- 原付款与单据保持不可变（触发器强制），退回本身也不可变 ----
        with db.pool.connection() as db_conn:
            frozen = []
            for sql in (
                "UPDATE payments SET amount = 1 WHERE external_ref = 'PAY-A'",
                "DELETE FROM payments WHERE external_ref = 'PAY-A'",
                f"UPDATE payment_refunds SET amount = 1 WHERE id = {first_refund_id}",
                f"DELETE FROM payment_refunds WHERE id = {first_refund_id}",
            ):
                try:
                    db_conn.execute(sql)
                    frozen.append(False)
                except psycopg.Error:
                    frozen.append(True)
        check("payments and refunds frozen", all(frozen), str(frozen))
        led = c.get("/rightsholders/A/ledger").json()
        check("frozen data intact",
              led["paid_total"] == 60 and led["refunded_total"] == 60
              and len(led["payments"]) == 1 and len(led["refunds"]) == 3
              and led["balance"] == 60, str(led))

        # ---- 退回加回余额后：后续付款仍只允许在正余额内登记 ----
        r = pay("A", "PAY-A2", 59)
        check("re-pay within restored balance", r.status_code == 201
              and r.json()["balance_after"] == 1, r.text)
        r = pay("A", "PAY-A3", 2)
        check("re-pay over balance still rejected", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["balance"] == 1, r.text)
        # 付款总额保留、退回总额另列
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger keeps both totals",
              led["billed_total"] == 60 and led["paid_total"] == 119
              and led["refunded_total"] == 60 and led["balance"] == 1,
              str(led))

        # ---- 并发：同一退回流水号 8 路，恰好 1 笔登记，其余幂等 ----
        same_results = []

        def fire_same():
            rr = refund("PAY-B", "RF-SAME", 10)
            same_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_same) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["refund_id"] for _, b in same_results}
        created = sum(1 for _, b in same_results if b["status"] == "created")
        dup = sum(1 for _, b in same_results if b["status"] == "duplicate")
        check("same refund-ref concurrent serialized",
              len(ids) == 1 and created == 1 and dup == 7,
              f"{[(s, b['status']) for s, b in same_results]}")
        check("B refunded once",
              c.get("/rightsholders/B/ledger").json()["refunded_total"] == 10)

        # ---- 并发：同一原付款大量退回，合计绝不超额 ----
        # PAY-B = 40，已退 10，剩可退 30；20 路各退 5 => 至多 6 笔成功（30）
        outcomes = {"created": 0, "rejected": 0, "errors": []}

        def fire_distinct(i):
            try:
                rr = refund("PAY-B", f"RF-D-{i}", 5)
                if rr.status_code == 201:
                    outcomes["created"] += 1
                elif rr.status_code == 409 and \
                        rr.json()["error"] == "refund_exceeds_payment":
                    outcomes["rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        ts = [threading.Thread(target=fire_distinct, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        led = c.get("/rightsholders/B/ledger").json()
        check("concurrent refunds never overshoot payment",
              not outcomes["errors"]
              and outcomes["created"] == 6 and outcomes["rejected"] == 14
              and led["refunded_total"] == 40 and led["paid_total"] == 40
              and led["balance"] == 40,
              f"{outcomes} {led}")

        # ---- 并发：退回与付款同时抢同一权利人余额，校验一致、无超付 ----
        # 干净场景：权利人 C 单据 100、付款 100（PAY-C），余额 0。
        c.post("/works", json={"code": "W2", "title": "另一作品"})
        c.post("/rightsholders", json={"code": "C", "name": "丙"})
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        c.post("/plays/import", json={
            "import_id": "Q1", "work_code": "W2",
            "occurred_at": "2026-02-11T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json={
            "start": "2026-02-01T00:00:00+08:00",
            "end": "2026-03-01T00:00:00+08:00"})
        r = pay("C", "PAY-C", 100)
        check("C pay 100 balance 0", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)

        stats = {"refund_created": 0, "refund_rejected": 0,
                 "pay_created": 0, "pay_rejected": 0, "errors": []}

        def fire_refund(i):
            # 每笔退 10，合计 10 笔即可退清 100；多发制造超额竞争
            rr = refund("PAY-C", f"RF-C-{i}", 10)
            if rr.status_code == 201:
                stats["refund_created"] += 1
            elif rr.status_code == 409 and \
                    rr.json()["error"] == "refund_exceeds_payment":
                stats["refund_rejected"] += 1
            else:
                stats["errors"].append(("refund", rr.status_code, rr.text))

        def fire_pay(i):
            rr = pay("C", f"PAY-C-NEW-{i}", 10)
            if rr.status_code == 201:
                stats["pay_created"] += 1
            elif rr.status_code == 409 and \
                    rr.json()["error"] == "payment_exceeds_balance":
                stats["pay_rejected"] += 1
            else:
                stats["errors"].append(("pay", rr.status_code, rr.text))

        threads = []
        for i in range(12):
            threads.append(threading.Thread(target=fire_refund, args=(i,)))
            threads.append(threading.Thread(target=fire_pay, args=(i,)))
        [t.start() for t in threads]
        [t.join() for t in threads]
        led = c.get("/rightsholders/C/ledger").json()
        check("refund/pay concurrency consistent",
              not stats["errors"]
              and stats["refund_created"] == 10 and stats["refund_rejected"] == 2
              and stats["pay_created"] + stats["pay_rejected"] == 12
              and led["refunded_total"] == 100
              and led["paid_total"] == 100 + 10 * stats["pay_created"]
              and led["balance"] == 100 - (led["paid_total"] - 100)
              and led["balance"] >= 0,
              f"{stats} {led}")

        # ---- 并发：出单（改变单据累计）与退回多轮同时触发，无死锁、余额恒一致 ----
        rounds = 12
        rstats = {"refund_created": 0, "refund_rejected": 0, "errors": []}

        def one_round(i):
            barrier = threading.Barrier(2)

            def issue_once():
                barrier.wait()
                c.post("/plays/import", json={
                    "import_id": f"QR-{i}", "work_code": "W2",
                    "occurred_at": f"2026-03-{(i % 20) + 1}T20:00:00+08:00",
                    "amount": 100})
                c.post("/statements/issue", json={
                    "start": "2026-03-01T00:00:00+08:00",
                    "end": "2026-04-01T00:00:00+08:00"})

            def refund_once():
                barrier.wait()
                # PAY-C 已退满 100 => 多数轮会超额拒绝；关键在无死锁/无 500
                rr = refund("PAY-C", f"RF-RACE-{i}", 10)
                if rr.status_code == 201:
                    rstats["refund_created"] += 1
                elif rr.status_code == 409 and \
                        rr.json()["error"] == "refund_exceeds_payment":
                    rstats["refund_rejected"] += 1
                else:
                    rstats["errors"].append((rr.status_code, rr.text))

            ti = threading.Thread(target=issue_once)
            tr = threading.Thread(target=refund_once)
            ti.start(); tr.start(); ti.join(); tr.join()

        for i in range(rounds):
            one_round(i)
        led = c.get("/rightsholders/C/ledger").json()
        check("issue/refund multi-round no deadlock, consistent",
              not rstats["errors"]
              and rstats["refund_created"] + rstats["refund_rejected"] == rounds
              and led["refunded_total"] <= 100
              and led["balance"] == led["billed_total"] - led["paid_total"]
                  + led["refunded_total"],
              f"{rstats} {led}")

        # ---- 回单核验：匹配仍按原付款字段；冻结累计退回额；退回额变化产生新版本 ----
        # A: billed 60, paid 119(PAY-A=60, PAY-A2=59), refunded 60, balance 1
        # 批次：R1 对 PAY-A 匹配；R2 对 PAY-B 权利人不符；R3 未登记；
        #       R4 与 PAY-OK（新付款）匹配；R5 跨批次重复。
        pay("A", "PAY-OK", 1)  # A 余额 1，恰好可付
        batch = [
            rline("RC-1", "PAY-A", "A", 60),
            rline("RC-2", "PAY-B", "A", 40),    # 付款权利人 B => 不符
            rline("RC-3", "PAY-LATE2", "B", 5),  # 未登记（迟到后由 B 补登）
            rline("RC-4", "PAY-OK", "A", 1),
        ]
        r = import_batch("RB-1", batch)
        check("receipt import with refunds frozen", r.status_code == 201, r.text)
        v1 = r.json()["verification"]
        lines1 = {x["receipt_no"]: x for x in v1["lines"]}
        check("v1 matched line by original payment fields",
              lines1["RC-1"]["status"] == "matched"
              and lines1["RC-1"]["payment_holder_code"] == "A"
              and lines1["RC-1"]["payment_amount"] == 60
              # PAY-A 已全额退回 60，但匹配结论不变；导入时冻结累计退回额
              and lines1["RC-1"]["payment_refunded_total"] == 60,
              str(lines1["RC-1"]))
        check("v1 mismatch line freezes refunded total",
              lines1["RC-2"]["status"] == "mismatched"
              and lines1["RC-2"]["payment_holder_code"] == "B"
              and lines1["RC-2"]["payment_refunded_total"] == 40,
              str(lines1["RC-2"]))
        check("v1 not-registered has no refunded snapshot",
              lines1["RC-3"]["status"] == "payment_not_registered"
              and lines1["RC-3"]["payment_refunded_total"] is None,
              str(lines1["RC-3"]))
        check("v1 fresh match refunded zero",
              lines1["RC-4"]["status"] == "matched"
              and lines1["RC-4"]["payment_refunded_total"] == 0,
              str(lines1["RC-4"]))

        # 无任何变化（结论与退回额都不变）=> 不新增版本
        r = c.post("/receipts/RB-1/reverify")
        check("reverify unchanged after no refund change", r.status_code == 200
              and r.json()["status"] == "unchanged"
              and r.json()["verification"]["version"] == 1, r.text)

        # PAY-OK 退回 1（A 余额 0 -> 1）=> 复核：结论仍 matched，但冻结退回额
        # 0 -> 1，视为结论变化，产生新版本
        r = refund("PAY-OK", "RF-OK-1", 1)
        check("refund PAY-OK", r.status_code == 201
              and r.json()["balance_after"] == 1, r.text)
        r = c.post("/receipts/RB-1/reverify")
        check("refund change creates new version", r.status_code == 201
              and r.json()["status"] == "created"
              and r.json()["verification"]["version"] == 2, r.text)
        v2 = r.json()["verification"]
        lines2 = {x["receipt_no"]: x for x in v2["lines"]}
        check("v2 still matched, refunded total updated",
              lines2["RC-4"]["status"] == "matched"
              and lines2["RC-4"]["payment_refunded_total"] == 1
              and lines2["RC-1"]["payment_refunded_total"] == 60
              and v2["counts"]["matched"] == 2, str(v2["counts"]))
        # 再复核：退回额无变化 => 不新增版本
        r = c.post("/receipts/RB-1/reverify")
        check("reverify unchanged again", r.status_code == 200
              and r.json()["status"] == "unchanged", r.text)
        # 旧版本不改写：v1 的 RC-4 冻结退回额仍为 0
        old = c.get("/receipts/RB-1/versions/1").json()
        old_lines = {x["receipt_no"]: x for x in old["lines"]}
        check("old version refund snapshot not rewritten",
              old_lines["RC-4"]["payment_refunded_total"] == 0
              and old_lines["RC-1"]["payment_refunded_total"] == 60,
              str(old_lines["RC-4"]))
        check("only two versions",
              [v["version"] for v in
               c.get("/receipts/RB-1/versions").json()["versions"]] == [1, 2])

        # 迟到付款也改变结论 => 正常产生新版本（原能力保留）
        pay("B", "PAY-LATE2", 5)
        r = c.post("/receipts/RB-1/reverify")
        check("late payment still versions", r.status_code == 201
              and r.json()["verification"]["version"] == 3
              and r.json()["verification"]["counts"]["payment_not_registered"] == 0,
              r.text)

        # 重复回单仍不参与核验，冻结退回额为 None
        r = import_batch("RB-2", [rline("RC-9", "PAY-A", "A", 60)])
        body = r.json()
        check("duplicate receipt excluded from verification",
              body["receipts"][0]["is_duplicate"] is True
              and body["verification"]["lines"][0]["status"] == "duplicate"
              and body["verification"]["lines"][0]["payment_refunded_total"] is None,
              r.text)

        # 核验不改写任何账务
        led = c.get("/rightsholders/A/ledger").json()
        check("verification never rewrites ledger A",
              led["billed_total"] == 60
              and led["paid_total"] == 60 + 59 + 1
              and led["refunded_total"] == 60 + 1
              and led["balance"] == 60 - (60 + 59 + 1) + (60 + 1),
              str(led))
        led_b = c.get("/rightsholders/B/ledger").json()
        check("verification never rewrites ledger B",
              led_b["billed_total"] == 40 and led_b["paid_total"] == 40 + 5
              and led_b["refunded_total"] == 40 and led_b["balance"] == 35,
              str(led_b))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL REFUND INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
