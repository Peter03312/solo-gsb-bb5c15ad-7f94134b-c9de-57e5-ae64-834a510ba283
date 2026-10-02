"""权利人余额核对底稿冻结：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_balance_snapshots.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_bsnap_")) / "pgdata"
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
        print("PASS" if cond else "FAIL", name, extra)
        if not cond:
            failures.append(name)

    def freeze(snapshot_no, holder_codes=None):
        return c.post(
            "/balance-snapshots",
            json={"snapshot_no": snapshot_no,
                  "holder_codes": holder_codes or []},
        )

    def page(snapshot_no, **params):
        return c.get(f"/balance-snapshots/{snapshot_no}", params=params)

    def line_map(payload):
        return {l["holder_code"]: l for l in payload["lines"]}

    def rec_map(payload):
        return {l["holder_code"]: l for l in payload["records"]}

    def expect_ledger(holder):
        led = c.get(f"/rightsholders/{holder}/ledger").json()
        return {
            "billed_total": led["billed_total"],
            "paid_total": led["paid_total"],
            "refunded_total": led["refunded_total"],
            "reserved_total": led["reserved_total"],
            "balance": led["balance"],
            "available_balance": led["available_balance"],
        }

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 W + 权利人 A/B/C/D，A/B 协议 60/40，播放 1000 => A 600 / B 400 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        for code, name in (("A", "甲"), ("B", "乙"), ("C", "丙"), ("D", "丁")):
            c.post("/rightsholders", json={"code": code, "name": name})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1000})
        issue = c.post("/statements/issue", json={
            "start": "2026-02-01T00:00:00+08:00",
            "end": "2026-03-01T00:00:00+08:00"}).json()
        check("initial statement A/B 600/400",
              {l["holder_code"]: l["amount"] for l in issue["payable"]}
              == {"A": 600, "B": 400})

        # 业务流水：A 付款 100（余 500）；B 付款 300（余 100）后退回 50（余 150）
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "PAY-A-1", "amount": 100})
        check("pay A 100", r.status_code == 201 and r.json()["balance_after"] == 500)
        r = c.post("/payments", json={
            "holder_code": "B", "external_ref": "PAY-B-1", "amount": 300})
        check("pay B 300", r.status_code == 201 and r.json()["balance_after"] == 100)
        r = c.post("/refunds", json={
            "payment_ref": "PAY-B-1", "refund_ref": "REF-B-1", "amount": 50})
        check("refund B 50", r.status_code == 201 and r.json()["balance_after"] == 150)
        # B 预留 120：reserved=120，available=30
        r = c.post("/payment-reservations", json={
            "holder_code": "B", "instruction_ref": "CMD-B-1", "amount": 120})
        check("reserve B 120",
              r.status_code == 201
              and r.json()["reserved_total"] == 120
              and r.json()["available_balance"] == 30, r.text)

        # ---- 1. 指定集合冻结（A/B）：值与对账单一致；口径校验 ----
        expected_a = expect_ledger("A")
        expected_b = expect_ledger("B")
        check("ledger A", expected_a == {
            "billed_total": 600, "paid_total": 100, "refunded_total": 0,
            "reserved_total": 0, "balance": 500, "available_balance": 500},
            str(expected_a))
        check("ledger B", expected_b == {
            "billed_total": 400, "paid_total": 300, "refunded_total": 50,
            "reserved_total": 120, "balance": 150, "available_balance": 30},
            str(expected_b))

        r = freeze("SNAP-AB", ["B", "A", "A"])
        check("duplicate holder code 422 whole reject",
              r.status_code == 422
              and r.json()["error"] == "duplicate_holder_code", r.text)
        check("rejected snapshot leaves nothing",
              page("SNAP-AB").status_code == 404)

        r = freeze("SNAP-NOBODY", ["NOBODY", "A"])
        check("unknown holder 404 whole reject",
              r.status_code == 404 and r.json()["error"] == "holder_not_found"
              and r.json()["detail"]["holder_code"] == "NOBODY", r.text)
        check("rejected unknown-holder snapshot leaves nothing",
              page("SNAP-NOBODY").status_code == 404)

        r = freeze("SNAP-AB", ["B", "A"])
        check("freeze subset 201",
              r.status_code == 201 and r.json()["status"] == "created", r.text)
        snap = r.json()
        check("subset header normalized/scope",
              snap["scope_all"] is False
              and snap["holder_codes"] == ["A", "B"]
              and snap["holder_count"] == 2)
        lines = line_map(snap)
        check("subset line A matches ledger",
              {k: lines["A"][k] for k in expected_a} == expected_a)
        check("subset line B matches ledger",
              {k: lines["B"][k] for k in expected_b} == expected_b)
        check("subset per-line formulas",
              lines["A"]["balance"]
                  == lines["A"]["billed_total"] - lines["A"]["paid_total"]
                  + lines["A"]["refunded_total"]
              and lines["B"]["available_balance"]
                  == lines["B"]["balance"] - lines["B"]["reserved_total"])
        check("subset totals",
              snap["totals"] == {
                  "billed_total": 1000, "paid_total": 400,
                  "refunded_total": 50, "reserved_total": 120,
                  "balance": 650, "available_balance": 530},
              str(snap["totals"]))

        # ---- 2. 同号同集合重试 => 200 duplicate，返回原快照 ----
        r2 = freeze("SNAP-AB", ["A", "B"])
        check("same no+set retry 200 duplicate",
              r2.status_code == 200 and r2.json()["status"] == "duplicate",
              r2.text[:200])
        check("retry identical content",
              r2.json()["lines"] == snap["lines"]
              and r2.json()["created_at"] == snap["created_at"])
        # 提交次序不同但集合相同 => 仍幂等
        r3 = freeze("SNAP-AB", ["B", "A"])
        check("same set different order idempotent",
              r3.status_code == 200 and r3.json()["status"] == "duplicate")

        # ---- 3. 同号异集合 => 409 snapshot_ref_conflict ----
        r = freeze("SNAP-AB", ["A"])
        check("same no different set 409",
              r.status_code == 409
              and r.json()["error"] == "snapshot_ref_conflict"
              and r.json()["detail"]["existing_holder_codes"] == ["A", "B"]
              and r.json()["detail"]["request_holder_codes"] == ["A"], r.text)
        # 同号：原指定集合 vs 全体
        r = freeze("SNAP-AB", [])
        check("same no subset vs all 409",
              r.status_code == 409
              and r.json()["error"] == "snapshot_ref_conflict"
              and r.json()["detail"]["existing_scope_all"] is False
              and r.json()["detail"]["request_scope_all"] is True)

        # ---- 4. 全体快照：含无任何流水的 C/D（各项为 0），顺序升序 ----
        r = freeze("SNAP-ALL", [])
        check("freeze all 201",
              r.status_code == 201 and r.json()["status"] == "created", r.text)
        sall = r.json()
        check("all scope header",
              sall["scope_all"] is True
              and sall["holder_codes"] == ["A", "B", "C", "D"]
              and sall["holder_count"] == 4)
        lall = line_map(sall)
        check("all includes zero holders C/D",
              all(lall[h] == {
                  "holder_code": h, "billed_total": 0, "paid_total": 0,
                  "refunded_total": 0, "reserved_total": 0,
                  "balance": 0, "available_balance": 0}
                  for h in ("C", "D")))
        check("all A/B consistent",
              {k: lall["A"][k] for k in expected_a} == expected_a
              and {k: lall["B"][k] for k in expected_b} == expected_b)
        check("all totals equal subset totals + zeros",
              sall["totals"] == snap["totals"])
        # 省略 holder_codes 字段同样表示全体
        r = c.post("/balance-snapshots", json={"snapshot_no": "SNAP-ALL2"})
        check("omitted holder_codes means all",
              r.status_code == 201 and r.json()["scope_all"] is True
              and r.json()["holder_count"] == 4)

        # ---- 5. 分页读取冻结明细及总计 ----
        r = page("SNAP-ALL", page=1, page_size=2)
        check("page1 200 size=2",
              r.status_code == 200 and len(r.json()["records"]) == 2
              and r.json()["total_records"] == 4
              and [x["holder_code"] for x in r.json()["records"]] == ["A", "B"],
              r.text[:200])
        p1 = r.json()
        check("page1 totals are whole-snapshot",
              p1["totals"] == sall["totals"]
              and p1["holder_codes"] == ["A", "B", "C", "D"])
        r = page("SNAP-ALL", page=3, page_size=2)
        check("page beyond range empty records",
              r.status_code == 200 and r.json()["records"] == []
              and r.json()["totals"] == sall["totals"])
        r = page("SNAP-ALL", page=0)
        check("page <1 422", r.status_code == 422)
        r = page("SNAP-ALL", page_size=501)
        check("page_size>500 422", r.status_code == 422)
        # 指定集合快照同样可读
        r = page("SNAP-AB", page=1, page_size=50)
        check("subset page totals",
              r.status_code == 200
              and [x["holder_code"] for x in r.json()["records"]] == ["A", "B"]
              and r.json()["totals"] == snap["totals"])

        # ---- 6. 未知快照号明确报错 ----
        r = page("SNAP-MISSING")
        check("unknown snapshot 404",
              r.status_code == 404
              and r.json()["error"] == "balance_snapshot_not_found")

        # ---- 7. 后续流水与新建权利人不改历史 ----
        # A 再付款 400（余 100）；新建权利人 E；新增单据追补
        c.post("/payments", json={
            "holder_code": "A", "external_ref": "PAY-A-2", "amount": 400})
        c.post("/rightsholders", json={"code": "E", "name": "戊"})
        # B 取消预留：reserved 归零、available 恢复
        r = c.post("/payment-reservations/CMD-B-1/cancel")
        check("cancel B reservation", r.status_code == 200)
        cur_a = expect_ledger("A")
        check("ledger A changed after flows",
              cur_a["paid_total"] == 500 and cur_a["balance"] == 100,
              str(cur_a))

        r = page("SNAP-AB", page=1, page_size=50)
        frozen = rec_map(r.json())
        check("frozen snapshot unchanged by later payments",
              {k: frozen["A"][k] for k in expected_a} == expected_a,
              str(frozen["A"]))
        check("frozen snapshot unchanged by reservation cancel",
              {k: frozen["B"][k] for k in expected_b} == expected_b,
              str(frozen["B"]))
        check("frozen totals unchanged", r.json()["totals"] == snap["totals"])
        r = page("SNAP-ALL", page=1, page_size=10)
        check("new holder not added to all-snapshot",
              r.json()["holder_count"] == 4
              and r.json()["total_records"] == 4
              and "E" not in rec_map(r.json()))
        # 创建响应（重试）同样是旧值
        r = freeze("SNAP-AB", ["A", "B"])
        still = line_map(r.json())
        check("idempotent retry returns frozen old values",
              {k: still["A"][k] for k in expected_a} == expected_a
              and {k: still["B"][k] for k in expected_b} == expected_b)

        # ---- 8. 数据库层不可变：UPDATE/DELETE 冻结行与快照头均被触发器拒绝 ----
        assert db.pool is not None
        with db.pool.connection() as raw:
            blocked = []
            for stmt in (
                "UPDATE balance_snapshot_lines SET balance = 0 "
                "WHERE holder_code = 'A'",
                "DELETE FROM balance_snapshot_lines WHERE holder_code = 'A'",
                "UPDATE balance_snapshots SET scope_all = false WHERE snapshot_no = 'SNAP-ALL'",
                "DELETE FROM balance_snapshots WHERE snapshot_no = 'SNAP-ALL'",
            ):
                try:
                    raw.execute(stmt)
                    blocked.append(False)
                except psycopg.Error:
                    raw.rollback()
                    blocked.append(True)
        check("frozen rows/header reject update/delete",
              blocked == [True, True, True, True], str(blocked))

        # ---- 9. 空库快照（全新独立库）：零行、零总计 ----
        with db.pool.connection() as admin:
            admin.execute("CREATE DATABASE royalty_bsnap_empty")
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(server.get_uri())
        empty_url = urlunsplit(
            (parts.scheme, parts.netloc, "/royalty_bsnap_empty",
             parts.query, parts.fragment)
        )
        econ = psycopg.connect(
            empty_url, autocommit=True, row_factory=psycopg.rows.dict_row
        )
        econ.execute(db.SCHEMA_SQL)
        from app.balance_snapshots_service import create_snapshot, get_snapshot_page
        try:
            res = create_snapshot(econ, "EMPTY-1", [])
            check("empty db snapshot created with zero lines",
                  res["status"] == "created" and res["scope_all"] is True
                  and res["holder_count"] == 0 and res["lines"] == []
                  and res["totals"] == {
                      "billed_total": 0, "paid_total": 0, "refunded_total": 0,
                      "reserved_total": 0, "balance": 0, "available_balance": 0},
                  str(res["totals"]))
            res2 = create_snapshot(econ, "EMPTY-1", [])
            check("empty snapshot idempotent", res2["status"] == "duplicate")
            pg = get_snapshot_page(econ, "EMPTY-1", 1, 50)
            check("empty snapshot page zero totals",
                  pg["records"] == [] and pg["total_records"] == 0
                  and pg["totals"]["balance"] == 0)
        finally:
            econ.close()

        # ---- 10. 同号并发创建：恰一笔 created，其余 duplicate（同集合）----
        outcomes = []
        lock = threading.Lock()

        def fire_same(i):
            rr = freeze(f"SNAP-CONC-{i % 3}", ["A", "B"])
            with lock:
                outcomes.append((rr.status_code, rr.json().get("status")))

        ts = [threading.Thread(target=fire_same, args=(i,)) for i in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        created = [o for o in outcomes if o == (201, "created")]
        dup = [o for o in outcomes if o == (200, "duplicate")]
        check("concurrent same-no creates exactly one",
              len(created) == 3 and len(dup) == 9 and len(outcomes) == 12,
              str(outcomes))

        # ---- 11. 同号并发但集合相异：恰一笔成功，其余按"已存在异集合"409 ----
        outcomes2 = []

        def fire_diff(i):
            subset = ["A"] if i % 2 == 0 else ["B"]
            rr = freeze("SNAP-CONC-DIFF", subset)
            with lock:
                outcomes2.append((rr.status_code, rr.json().get("error")))

        ts = [threading.Thread(target=fire_diff, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        created2 = [o for o in outcomes2 if o[0] == 201]
        conflict2 = [o for o in outcomes2
                     if o == (409, "snapshot_ref_conflict")]
        dup2 = [o for o in outcomes2 if o[0] == 200]
        check("concurrent diff-set exactly one created",
              len(created2) == 1, str(outcomes2))
        # 与已建快照同集合的请求幂等（200），异集合 409；无其他结果
        check("concurrent diff-set rest idempotent or conflict",
              len(created2) + len(conflict2) + len(dup2) == 8
              and len(conflict2) + len(dup2) == 7, str(outcomes2))
        # 4 个 A 4 个 B：最终必有一类整体冲突、一类（与已建同集合）幂等
        check("concurrent diff-set both branches exercised",
              conflict2 and dup2, str(outcomes2))

        # ---- 12. 与出单/付款/退票/预留确认或取消并发：不得混合前后状态 ----
        # 每轮新建独立作品（两名新权利人各 50%）+ 两笔播放各 1000 => 各 1000；
        # barrier 同时放行"冻结全体"与"一笔（或一串）写操作"。快照内每位
        # 权利人的六项必须整体取自某一已提交时点（对该轮受影响权利人而言
        # 即操作前或操作后），不得混合；若 REPEATABLE READ 失效会出现
        # "余额已变而付款仍旧 / 预留与可用额不自洽"等混合值。

        # 固定背景权利人 A/B 在并发轮次中保持不变
        bg_state = {"A": expect_ledger("A"), "B": expect_ledger("B")}

        rounds = 16
        no_mix_violations = []
        op_errors = []

        def setup_round(i):
            h1, h2 = f"K{i}A", f"K{i}B"
            c.post("/rightsholders", json={"code": h1})
            c.post("/rightsholders", json={"code": h2})
            c.post("/works", json={"code": f"WC{i}", "title": f"并发作品{i}"})
            c.post("/agreements", json={
                "work_code": f"WC{i}",
                "valid_from": "2026-01-01T00:00:00+08:00",
                "valid_to": "2027-01-01T00:00:00+08:00",
                "shares": [{"holder_code": h1, "percent": "50"},
                           {"holder_code": h2, "percent": "50"}]})
            c.post("/plays/import", json={
                "import_id": f"CP{i}-1", "work_code": f"WC{i}",
                "occurred_at": "2026-04-10T20:00:00+08:00", "amount": 1000})
            c.post("/plays/import", json={
                "import_id": f"CP{i}-2", "work_code": f"WC{i}",
                "occurred_at": "2026-04-11T20:00:00+08:00", "amount": 1000})
            c.post("/statements/issue", json={
                "start": "2026-04-01T00:00:00+08:00",
                "end": "2026-05-01T00:00:00+08:00"})
            # 确认轮/取消轮：预留发生在 barrier 之前，使"操作前"状态已含预留
            if i % 4 in (2, 3):
                amount = 300 if i % 4 == 2 else 400
                rr = c.post("/payment-reservations", json={
                    "holder_code": h1, "instruction_ref": f"CMD-C{i}",
                    "amount": amount})
                if rr.status_code != 201:
                    op_errors.append(("reserve", i, rr.status_code, rr.text))
            return h1, h2

        def run_writes(i, h1):
            """barrier 后执行；返回本次涉及的全部 HTTP 状态码。"""
            codes = []
            if i % 4 == 0:      # 直接付款 200
                codes.append(c.post("/payments", json={
                    "holder_code": h1, "external_ref": f"PAY-C{i}",
                    "amount": 200}).status_code)
            elif i % 4 == 1:    # 付款 500 后退回 100
                codes.append(c.post("/payments", json={
                    "holder_code": h1, "external_ref": f"PAY-C{i}",
                    "amount": 500}).status_code)
                codes.append(c.post("/refunds", json={
                    "payment_ref": f"PAY-C{i}", "refund_ref": f"REF-C{i}",
                    "amount": 100}).status_code)
            elif i % 4 == 2:    # 确认预留 300（原子付款 + 结束预留）
                codes.append(c.post(
                    f"/payment-reservations/CMD-C{i}/confirm",
                    json={"external_ref": f"PAY-C{i}"}).status_code)
            else:               # 取消预留 400
                codes.append(
                    c.post(f"/payment-reservations/CMD-C{i}/cancel")
                    .status_code)
            # 偶数轮并发出单：更正首笔播放 1000 -> 700 后追补（每人 -150）
            if i % 2 == 0:
                c.post("/plays/import", json={
                    "import_id": f"CP{i}-1R", "work_code": f"WC{i}",
                    "occurred_at": "2026-04-10T20:00:00+08:00",
                    "amount": 700, "corrects_id": f"CP{i}-1"})
                codes.append(c.post("/statements/issue", json={
                    "start": "2026-04-01T00:00:00+08:00",
                    "end": "2026-05-01T00:00:00+08:00"}).status_code)
            return codes

        def one_round(i):
            h1, h2 = setup_round(i)
            pre = {h1: expect_ledger(h1), h2: expect_ledger(h2)}

            snapshot_result = {}
            write_codes = {}
            barrier = threading.Barrier(2)

            def snap_thread():
                barrier.wait()
                rr = freeze(f"SNAP-C{i}", [])
                snapshot_result["code"] = rr.status_code
                if rr.status_code in (200, 201):
                    snapshot_result["lines"] = line_map(rr.json())

            def write_thread():
                barrier.wait()
                write_codes["codes"] = run_writes(i, h1)

            t1 = threading.Thread(target=snap_thread)
            t2 = threading.Thread(target=write_thread)
            t1.start(); t2.start(); t1.join(); t2.join()

            if not all(s in (200, 201) for s in write_codes["codes"]):
                op_errors.append((i, write_codes["codes"]))

            post = {h1: expect_ledger(h1), h2: expect_ledger(h2)}

            if snapshot_result.get("code") not in (200, 201):
                no_mix_violations.append(
                    (i, "snapshot not created", snapshot_result.get("code")))
                return
            smap = snapshot_result["lines"]
            # h1（受写操作影响）与 h2、背景 A/B（本轮不变）的六个字段
            # 必须整体等于操作前或操作后状态（h2/背景两者相同）。
            for h, before in ((h1, pre[h1]), (h2, pre[h2]),
                              ("A", bg_state["A"]), ("B", bg_state["B"])):
                after = post.get(h, before)
                got = {k: smap[h][k] for k in before}
                if got != before and got != after:
                    no_mix_violations.append((i, h, got, before, after))
            if h1 not in smap or h2 not in smap:
                no_mix_violations.append((i, "missing conc holders"))

        for i in range(rounds):
            one_round(i)

        check("concurrent writes all valid (200/201)",
              not op_errors, str(op_errors[:5]))
        check("snapshots never mix pre/post state across issue/pay/refund/"
              "confirm/cancel",
              not no_mix_violations, str(no_mix_violations[:3]))

        # ---- 13. 既有余额/付款/对账接口保持兼容 ----
        led_a = c.get("/rightsholders/A/ledger")
        check("ledger API still 200 with all fields",
              led_a.status_code == 200
              and {"billed_total", "paid_total", "refunded_total",
                   "reserved_total", "balance", "available_balance",
                   "statements", "payments", "refunds", "instructions"}
              <= set(led_a.json().keys()))
        r = c.post("/payments", json={
            "holder_code": "C", "external_ref": "PAY-C-NOBAL", "amount": 1})
        check("payment still rejected when no balance (compat)",
              r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL BALANCE SNAPSHOT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
