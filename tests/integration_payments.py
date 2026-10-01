"""付款登记与权利人对账：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_payments.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_pay_")) / "pgdata"
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

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 + 两名权利人 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})

        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}

        # 无单据：余额为 0，付款被拒（余额非正）
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E0", "amount": 1})
        check("no bill => reject", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance", r.text)

        # 权利人不存在
        r = c.post("/payments", json={"holder_code": "NOBODY", "external_ref": "E1", "amount": 1})
        check("holder missing 404", r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)

        # 金额非正整数
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E1", "amount": 0})
        check("amount zero 422", r.status_code == 422, r.text)
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E1", "amount": -5})
        check("amount negative 422", r.status_code == 422, r.text)
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E1", "amount": 1.5})
        check("amount non-int 422", r.status_code == 422, r.text)
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E1", "amount": True})
        check("amount bool 422", r.status_code == 422, r.text)

        # ---- 出具 100 分单据：A=60，B=40 ----
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=period)

        led = c.get("/rightsholders/A/ledger").json()
        check("ledger A after bill",
              led["billed_total"] == 60 and led["paid_total"] == 0
              and led["balance"] == 60 and len(led["statements"]) == 1
              and led["statements"][0]["kind"] == "initial", str(led))
        check("ledger 404",
              c.get("/rightsholders/NOBODY/ledger").status_code == 404)

        # 超额拒绝：61 > 60
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "EBIG", "amount": 61})
        check("overpay reject", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["balance"] == 60, r.text)
        # 超额拒绝不留付款
        led = c.get("/rightsholders/A/ledger").json()
        check("reject leaves no payment", led["paid_total"] == 0 and led["balance"] == 60)

        # 恰好等于余额入账，登记后余额 0
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA1", "amount": 60})
        body = r.json()
        check("pay exact balance 201", r.status_code == 201
              and body["status"] == "created" and body["balance_after"] == 0
              and body["payment_id"] >= 1, r.text)

        # 余额为 0，再付 1 分被拒
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA2", "amount": 1})
        check("zero balance blocks pay", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance", r.text)

        # 幂等：同流水号同内容重试 => 原记录 + 当前余额，不重复入账
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA1", "amount": 60})
        body2 = r.json()
        check("idempotent retry", r.status_code == 200 and body2["status"] == "duplicate"
              and body2["payment_id"] == body["payment_id"]
              and body2["balance_after"] == 0, r.text)
        led = c.get("/rightsholders/A/ledger").json()
        check("retry no double count", led["paid_total"] == 60
              and len(led["payments"]) == 1 and led["balance"] == 0)

        # 已登记付款不可修改/删除（负额追补也不撤销历史付款，DB 触发器强制）
        with db.pool.connection() as db_conn:
            try:
                db_conn.execute("UPDATE payments SET amount = 1 WHERE id = %s",
                                (body["payment_id"],))
                frozen_update = False
            except psycopg.Error:
                frozen_update = True
            try:
                db_conn.execute("DELETE FROM payments WHERE id = %s", (body["payment_id"],))
                frozen_delete = False
            except psycopg.Error:
                frozen_delete = True
        check("payment frozen update/delete", frozen_update and frozen_delete)
        check("payment still present after attempts",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 60)

        # 同流水号异内容（金额不同 / 权利人不同）=> 409，且不改账
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA1", "amount": 30})
        check("ref conflict amount", r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 60, r.text)
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "PA1", "amount": 60})
        check("ref conflict holder", r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)
        check("B untouched", c.get("/rightsholders/B/ledger").json()["paid_total"] == 0)

        # ---- 跨期更正产生负额追补：100 -> 50，A 30 / B 20 ----
        c.post("/plays/import", json={
            "import_id": "P1R1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 50,
            "corrects_id": "P1"})
        r = c.post("/statements/issue", json=period)
        check("negative supplement issued", r.json()["kind"] == "supplement", r.text)

        # 历史付款 PA1 不撤销：A 单据累计 30、已付 60、余额 -30
        led = c.get("/rightsholders/A/ledger").json()
        check("A negative balance retained",
              led["billed_total"] == 30 and led["paid_total"] == 60
              and led["balance"] == -30
              and len(led["statements"]) == 2
              and led["statements"][1]["amount"] == -30, str(led))
        check("payments frozen list",
              len(led["payments"]) == 1 and led["payments"][0]["external_ref"] == "PA1")

        # 负余额禁止付款
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PAN", "amount": 1})
        check("negative balance blocks pay", r.status_code == 409
              and r.json()["detail"]["balance"] == -30, r.text)

        # B 当前余额 20，先付 10
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "PB1", "amount": 10})
        check("B partial pay", r.status_code == 201 and r.json()["balance_after"] == 10, r.text)

        # 后续正额抵扣：50 -> 200，A 120 / B 80；追补 +90 / +60
        c.post("/plays/import", json={
            "import_id": "P1R2", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 200,
            "corrects_id": "P1R1"})
        r = c.post("/statements/issue", json=period)
        body = r.json()
        got = {x["holder_code"]: x["delta"] for x in body["payable"]}
        check("positive supplement", body["kind"] == "supplement"
              and got == {"A": 90, "B": 60}, str(got))

        # A: 120 - 60 = 60（负额已被正额抵扣）；B: 80 - 10 = 70
        led = c.get("/rightsholders/A/ledger").json()
        check("A recovered", led["billed_total"] == 120
              and led["paid_total"] == 60 and led["balance"] == 60, str(led))
        led = c.get("/rightsholders/B/ledger").json()
        check("B balance", led["billed_total"] == 80
              and led["paid_total"] == 10 and led["balance"] == 70
              and [p["external_ref"] for p in led["payments"]] == ["PB1"], str(led))

        # 恢复后可付款，但仍不得超付
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA3", "amount": 60})
        check("A pay after recovery", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "PA4", "amount": 61})
        check("A still no overpay", r.status_code == 409, r.text)

        # ---- 试算与待处理不入账 ----
        # 新作品无协议 => 待处理；另作一段期间的试算，均不影响 A 余额
        c.post("/works", json={"code": "W2", "title": "待处理作品"})
        c.post("/plays/import", json={
            "import_id": "P9", "work_code": "W2",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 999})
        p_other = {"start": "2026-05-01T00:00:00+08:00",
                   "end": "2026-06-01T00:00:00+08:00"}
        c.get("/statements/preview", params=p_other)
        c.post("/statements/issue", json=p_other)  # 仅待处理，无明细行
        check("preview/pending excluded",
              c.get("/rightsholders/A/ledger").json()["balance"] == 0)

        # ---- 并发：同一流水号同内容并发，恰好一笔登记，其余幂等返回原记录 ----
        # 用新权利人 C 做干净的并发场景
        c.post("/rightsholders", json={"code": "C", "name": "丙"})
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        # 让 P9 归到 C：W2 协议后补后重新出具 5 月期间
        # （无变化的初次单已存在 => 协议后补产出追补单 +999）
        r = c.post("/statements/issue", json=p_other)
        check("C supplement 999",
              r.json()["kind"] == "supplement"
              and {x["holder_code"]: x["delta"] for x in r.json()["payable"]}
              == {"C": 999}, r.text)
        led = c.get("/rightsholders/C/ledger").json()
        check("C billed 999", led["billed_total"] == 999 and led["balance"] == 999, str(led))

        same_results = []

        def fire_same():
            rr = c.post("/payments",
                        json={"holder_code": "C", "external_ref": "PCX", "amount": 50})
            same_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_same) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["payment_id"] for _, b in same_results}
        created = sum(1 for _, b in same_results if b["status"] == "created")
        dup = sum(1 for _, b in same_results if b["status"] == "duplicate")
        check("same-ref concurrent serialized",
              len(ids) == 1 and created == 1 and dup == 7,
              f"{[(s, b['status']) for s, b in same_results]}")
        check("C paid 50 once",
              c.get("/rightsholders/C/ledger").json()["paid_total"] == 50)

        # ---- 并发：不同流水号对 C（余 949）大量抢付，总额不得超付 ----
        outcomes = {"created": 0, "rejected": 0, "amounts": 0, "errors": []}

        def fire_distinct(i):
            try:
                rr = c.post("/payments",
                            json={"holder_code": "C", "external_ref": f"PD-{i}", "amount": 100})
                if rr.status_code == 201:
                    outcomes["created"] += 1
                    outcomes["amounts"] += rr.json()["amount"]
                elif rr.status_code == 409 and rr.json()["error"] == "payment_exceeds_balance":
                    outcomes["rejected"] += 1
                else:
                    outcomes["errors"].append((rr.status_code, rr.text))
            except Exception as e:  # noqa
                outcomes["errors"].append(repr(e))

        ts = [threading.Thread(target=fire_distinct, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        led = c.get("/rightsholders/C/ledger").json()
        # 已付 50，剩余 949；每笔 100 => 至多 9 笔成功（900），其余超额拒绝
        check("concurrent distinct no overpay",
              not outcomes["errors"]
              and outcomes["created"] == 9 and outcomes["rejected"] == 11
              and led["paid_total"] == 950 and led["billed_total"] == 999
              and led["balance"] == 49,
              f"{outcomes} {led}")

        # ---- 并发：出单（改变单据累计）与付款多轮同时触发，不得超付 ----
        # A 当前余额 0；每轮导入 100 分新播放（A 得 60），
        # 出单与付款在 Barrier 上同时放行，重复多轮以覆盖两种先后次序。
        rounds = 15
        stats = {"pay_created": 0, "pay_rejected": 0, "errors": []}

        def one_round(i):
            barrier = threading.Barrier(2)

            def issue_once():
                barrier.wait()
                c.post("/plays/import", json={
                    "import_id": f"PR-{i}", "work_code": "W",
                    "occurred_at": f"2026-02-{(i % 20) + 1}T20:00:00+08:00",
                    "amount": 100})
                c.post("/statements/issue", json=period)

            def pay_once():
                barrier.wait()
                rr = c.post("/payments",
                            json={"holder_code": "A",
                                  "external_ref": f"PRACE-{i}", "amount": 60})
                if rr.status_code == 201:
                    stats["pay_created"] += 1
                elif rr.status_code == 409 and rr.json()["error"] == "payment_exceeds_balance":
                    stats["pay_rejected"] += 1
                else:
                    stats["errors"].append((rr.status_code, rr.text))

            ti = threading.Thread(target=issue_once)
            tp = threading.Thread(target=pay_once)
            ti.start(); tp.start(); ti.join(); tp.join()

        for i in range(rounds):
            one_round(i)

        led = c.get("/rightsholders/A/ledger").json()
        # 不变量：全部请求要么成功要么明确超额拒绝；成功付款总额不超过单据累计，
        # 每轮成功至多 1 笔；最终余额必非负（无超付）。
        check("issue/pay multi-round no overpay",
              not stats["errors"]
              and stats["pay_created"] + stats["pay_rejected"] == rounds
              and stats["pay_created"] <= rounds
              and led["paid_total"] <= led["billed_total"]
              and led["balance"] == led["billed_total"] - led["paid_total"] >= 0
              and stats["pay_created"] >= 1,  # 多轮中必然观察到"先出单后付款"
              f"{stats} {led}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PAYMENT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
