"""端到端集成测试（使用 pgserver 便携 PostgreSQL，无需 Docker）。

运行: python3 tests/integration_pgserver.py
"""
import os
import pathlib
import tempfile

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    with TestClient(app) as c:
        # 健康检查
        check("health", c.get("/health").json()["status"] == "ok")

        # 作品登记 + 幂等 + 冲突
        r = c.post("/works", json={"code": "W1", "title": "作品一"})
        check("work create", r.status_code == 200 and r.json()["created"] is True, r.text)
        r = c.post("/works", json={"code": "W1", "title": "作品一"})
        check("work idempotent", r.json()["created"] is False)
        r = c.post("/works", json={"code": "W1", "title": "别的名字"})
        check("work conflict 409", r.status_code == 409)

        # 先导入一条没有协议的播放
        r = c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        check("play create", r.status_code == 201, r.text)

        # 同内容幂等
        r = c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        check("play duplicate", r.json()["status"] == "duplicate")
        # 异内容拒绝
        r = c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 200})
        check("play conflict 409", r.status_code == 409)
        # 缺作品
        r = c.post("/plays/import", json={
            "import_id": "PX", "work_code": "NOPE",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1})
        check("play unknown work 422", r.status_code == 422)

        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}

        # 无协议出具：应在待处理，payable 为空
        r = c.post("/statements/issue", json=period)
        body = r.json()
        check("initial no-agr batch", r.status_code == 200 and body["kind"] == "initial")
        check("pending listed", len(body["pending"]) == 1 and body["pending"][0]["import_id"] == "P1")
        check("no payable yet", body["payable"] == [])

        # 同期间再出具：无变化，不产生新单
        r = c.post("/statements/issue", json=period)
        check("no dup when unchanged", r.json()["batch_id"] == body["batch_id"])
        r = c.get("/statements", params=period)
        check("only one batch so far", len(r.json()) == 1)

        # 登记协议：比例合计 != 100 拒绝
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"}]})
        check("agreement bad total 422", r.status_code == 422, r.text)
        # 合法协议 62.5/30/7.5
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "62.5"},
                       {"holder_code": "B", "percent": "30"},
                       {"holder_code": "C", "percent": "7.5"}]})
        check("agreement create", r.status_code == 201, r.text)
        # 重叠协议拒绝
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-06-01T00:00:00+08:00",
            "valid_to": "2026-08-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        check("agreement overlap 409", r.status_code == 409, r.text)
        # 相邻协议允许（半开区间）
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        check("agreement adjacent ok", r.status_code == 201, r.text)

        # 协议后补，重新出具 => 追补单：100 => 63/30/7
        # （精确 62.5/30/7.5，floor 62/30/7 余 1；A、C 同为 .5，编号升序给 A）
        r = c.post("/statements/issue", json=period)
        body = r.json()
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in body["payable"]}
        check("supplement after agreement", body["kind"] == "supplement", r.text)
        check("split 63/30/7", got == {"A": (63, 63), "B": (30, 30), "C": (7, 7)}, str(got))
        check("pending cleared", body["pending"] == [])

        # 更正 P1 -> P1R1 金额 80（引用原编号、新编号）
        r = c.post("/plays/import", json={
            "import_id": "P1R1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 80,
            "corrects_id": "P1"})
        check("correction create", r.status_code == 201 and r.json()["chain_tail"] is True)
        # 链查询：P1 不再是尾
        r = c.get("/plays/P1/chain")
        chain = r.json()["chain"]
        check("chain length 2", len(chain) == 2)
        check("old not tail", chain[0]["chain_tail"] is False and chain[1]["import_id"] == "P1R1")
        # 分叉：再更正 P1
        r = c.post("/plays/import", json={
            "import_id": "P1RX", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 90,
            "corrects_id": "P1"})
        check("fork 409", r.status_code == 409)
        # 引用不存在
        r = c.post("/plays/import", json={
            "import_id": "P1RY", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 90,
            "corrects_id": "GHOST"})
        check("correct missing 422", r.status_code == 422)
        # 自引用
        r = c.post("/plays/import", json={
            "import_id": "P1R2", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 30,
            "corrects_id": "P1R2"})
        check("self correct 422", r.status_code == 422)

        # 再次更正链尾 P1R1 -> P1R2，金额 30 => 19/9/2
        r = c.post("/plays/import", json={
            "import_id": "P1R2", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 30,
            "corrects_id": "P1R1"})
        check("chain correction ok", r.status_code == 201, r.text)

        # 出具追补单：累计此前 63/30/7，新值 19/9/2，差额 -44/-21/-5
        r = c.post("/statements/issue", json=period)
        body = r.json()
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in body["payable"]}
        check("negative supplement", body["kind"] == "supplement")
        check("delta -44/-21/-5", got == {"A": (19, -44), "B": (9, -21), "C": (2, -5)}, str(got))

        # 累计校验：初次 + 追补1 + 追补2 之和应等于当前应付 19/9/2
        r = c.get("/statements", params=period)
        batches = r.json()
        totals = {}
        for b in batches:
            for l in b["lines"]:
                totals[l["holder_code"]] = totals.get(l["holder_code"], 0) + l["amount"]
        check("cumulative equals current", totals == {"A": 19, "B": 9, "C": 2}, str(totals))
        check("3 batches", len(batches) == 3, str(len(batches)))

        # preview 与当前一致、delta=0
        r = c.get("/statements/preview", params=period)
        pv = {x["holder_code"]: x["delta"] for x in r.json()["payable"]}
        check("preview delta zero", pv == {"A": 0, "B": 0, "C": 0}, str(pv))

        # 并发同期间出具：只应成功插入 1 张初始单（用另一期间做并发测试）
        import threading
        p2 = {"start": "2027-02-01T00:00:00+08:00", "end": "2027-03-01T00:00:00+08:00"}
        ids = []
        errs = []

        def fire():
            try:
                rr = c.post("/statements/issue", json=p2)
                ids.append(rr.json().get("batch_id"))
            except Exception as e:  # noqa
                errs.append(repr(e))

        threads = [threading.Thread(target=fire) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("concurrent single batch", len(set(ids)) == 1 and not errs, f"{ids} {errs}")

        # 批量导入整批事务：含冲突 => 整批拒绝
        before = c.get("/works").json()
        r = c.post("/plays/bulk-import", json={"plays": [
            {"import_id": "B1", "work_code": "W1",
             "occurred_at": "2026-02-11T20:00:00+08:00", "amount": 10},
            {"import_id": "P1", "work_code": "W1",
             "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 999}]})
        check("bulk rejects whole", r.status_code == 409, r.text)

        # 并发登记重叠协议：恰好一个成功，其余 409（验证应用层互斥）
        c.post("/works", json={"code": "WRACE", "title": "并发作品"})
        statuses = []

        def fire_agreement(i):
            rr = c.post("/agreements", json={
                "work_code": "WRACE",
                "valid_from": f"2030-01-0{i+1}T00:00:00+08:00",
                "valid_to": f"2030-06-0{i+1}T00:00:00+08:00",
                "shares": [{"holder_code": "A", "percent": "100"}]})
            statuses.append(rr.status_code)

        ts = [threading.Thread(target=fire_agreement, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("overlap concurrency serialized",
              statuses.count(201) == 1 and statuses.count(409) == 3,
              str(statuses))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
