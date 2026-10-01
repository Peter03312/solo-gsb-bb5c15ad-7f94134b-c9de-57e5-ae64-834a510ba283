"""银行回单批次导入与付款核验：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_receipts.py
"""
import os
import pathlib
import tempfile
import threading
import time

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rcpt_")) / "pgdata"
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

    def import_batch(batch_no, receipts):
        return c.post("/receipts/import",
                      json={"batch_no": batch_no, "receipts": receipts})

    def line(receipt_no, ref, holder, amount):
        return {"receipt_no": receipt_no, "external_ref": ref,
                "holder_code": holder, "amount": amount}

    def diff_by_no(verification):
        return {d["receipt_no"]: d for d in verification["differences"]}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 + 权利人 A/B，协议 60/40，播放 1000 => A 600 / B 400 ----
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
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json={
            "start": "2026-02-01T00:00:00+08:00",
            "end": "2026-03-01T00:00:00+08:00"})

        def pay(holder, ref, amount):
            r = c.post("/payments", json={
                "holder_code": holder, "external_ref": ref, "amount": amount})
            assert r.status_code == 201, r.text
            return r

        # 已登记付款：A 付 400（余 200），B 付 350（余 50）
        pay("A", "PAY-OK", 100)      # 回单 R1 与其完全一致
        pay("B", "PAY-HOLDER", 200)  # 回单 R2 权利人为 A => 权利人不符
        pay("A", "PAY-AMOUNT", 300)  # 回单 R3 金额 301 => 金额不符
        pay("B", "PAY-BOTH", 150)    # 回单 R4 权利人 C 金额 151 => 两项均不符

        # ---- 无效字段：整批拒绝（422），不留任何回单 ----
        bad_batches = []
        for i, bad_line in enumerate([
            line("BL-1", "BR-1", "A", 0),          # 金额为 0
            line("BL-1", "BR-1", "A", -5),         # 金额为负
            line("BL-1", "BR-1", "A", 1.5),        # 金额非整数
            line("BL-1", "BR-1", "A", True),       # 金额为布尔
            {"receipt_no": "BL-1", "external_ref": "BR-1",
             "holder_code": "A", "amount": "100"},  # 金额为字符串
            {"receipt_no": "", "external_ref": "BR-1",
             "holder_code": "A", "amount": 1},      # 空回单编号
            {"receipt_no": "BL-1", "external_ref": "",
             "holder_code": "A", "amount": 1},      # 空流水号
            {"receipt_no": "BL-1", "external_ref": "BR-1",
             "holder_code": "", "amount": 1},       # 空权利人
            {"receipt_no": "BL-1", "external_ref": "BR-1",
             "holder_code": "A"},                   # 缺金额字段
        ]):
            bad = f"BAD-{i}"
            bad_batches.append(bad)
            r = import_batch(bad, [bad_line])
            check(f"invalid field 422 #{i}", r.status_code == 422, r.text)
        # 空批次编号 / 空回单列表
        r = import_batch("", [line("BL-1", "BR-1", "A", 1)])
        check("empty batch_no 422", r.status_code == 422, r.text)
        r = c.post("/receipts/import", json={"batch_no": "BAD-EMPTY", "receipts": []})
        check("empty receipts 422", r.status_code == 422, r.text)
        bad_batches.append("BAD-EMPTY")
        # 批内重复流水号 / 重复回单编号
        r = import_batch("BAD-DUPREF", [
            line("BL-1", "BR-1", "A", 1), line("BL-2", "BR-1", "A", 2)])
        check("dup external_ref in batch 422", r.status_code == 422
              and r.json()["error"] == "duplicate_external_ref_in_batch", r.text)
        bad_batches.append("BAD-DUPREF")
        r = import_batch("BAD-DUPNO", [
            line("BL-1", "BR-1", "A", 1), line("BL-1", "BR-2", "A", 2)])
        check("dup receipt_no in batch 422", r.status_code == 422
              and r.json()["error"] == "duplicate_receipt_no_in_batch", r.text)
        bad_batches.append("BAD-DUPNO")
        # 整批拒绝不留痕：批次不存在，回单编号也未被占用
        for bad in bad_batches:
            check(f"rejected batch {bad} leaves nothing",
                  c.get(f"/receipts/{bad}").status_code == 404)
        r = import_batch("AFTER-BAD", [line("BL-1", "BR-1", "A", 1)])
        check("receipt_no of rejected batch reusable",
              r.status_code == 201 and r.json()["status"] == "created", r.text)

        # ---- 批次 B1：六种结论一次覆盖 ----
        b1_lines = [
            line("R1", "PAY-OK", "A", 100),      # 匹配
            line("R2", "PAY-HOLDER", "A", 200),  # 权利人不符（付款为 B/200）
            line("R3", "PAY-AMOUNT", "A", 301),  # 金额不符（付款为 A/300）
            line("R4", "PAY-BOTH", "C", 151),    # 两项均不符（付款为 B/150）
            line("R5", "PAY-LATE", "A", 50),     # 付款未登记（迟到后补）
            line("R6", "PAY-NEVER", "B", 40),    # 付款未登记
        ]
        r = import_batch("B1", b1_lines)
        body = r.json()
        check("B1 import 201", r.status_code == 201 and body["status"] == "created",
              r.text)
        check("B1 receipts echoed",
              [(x["receipt_no"], x["is_duplicate"]) for x in body["receipts"]]
              == [(f"R{i}", False) for i in range(1, 7)], str(body["receipts"]))
        v1 = body["verification"]
        check("B1 v1 frozen at import",
              v1["version"] == 1
              and v1["counts"] == {"matched": 1, "payment_not_registered": 2,
                                   "mismatched": 3, "duplicate": 0, "total": 6},
              str(v1["counts"]))
        d = diff_by_no(v1)
        check("B1 v1 differences are non-matched lines",
              sorted(d) == ["R2", "R3", "R4", "R5", "R6"], str(sorted(d)))
        check("R2 holder mismatch only",
              d["R2"]["status"] == "mismatched"
              and d["R2"]["differences"] == ["holder_mismatch"]
              and d["R2"]["payment_holder_code"] == "B"
              and d["R2"]["payment_amount"] == 200
              and d["R2"]["receipt_holder_code"] == "A"
              and d["R2"]["receipt_amount"] == 200, str(d["R2"]))
        check("R3 amount mismatch only",
              d["R3"]["differences"] == ["amount_mismatch"]
              and d["R3"]["payment_holder_code"] == "A"
              and d["R3"]["payment_amount"] == 300, str(d["R3"]))
        check("R4 both mismatches listed together",
              d["R4"]["status"] == "mismatched"
              and d["R4"]["differences"] == ["holder_mismatch", "amount_mismatch"]
              and d["R4"]["payment_holder_code"] == "B"
              and d["R4"]["payment_amount"] == 150
              and d["R4"]["receipt_holder_code"] == "C"
              and d["R4"]["receipt_amount"] == 151, str(d["R4"]))
        check("R5/R6 payment not registered",
              d["R5"]["status"] == "payment_not_registered"
              and d["R5"]["differences"] == []
              and d["R5"]["payment_holder_code"] is None
              and d["R5"]["payment_amount"] is None
              and d["R6"]["status"] == "payment_not_registered", str(d["R5"]))

        # ---- 批次同内容重试（次序无关）=> 幂等返回原批次 ----
        r = import_batch("B1", list(reversed(b1_lines)))
        body = r.json()
        check("B1 retry idempotent",
              r.status_code == 200 and body["status"] == "duplicate"
              and body["verification"]["version"] == 1
              and body["verification"]["counts"]["matched"] == 1, r.text)
        check("B1 retry no new version",
              len(c.get("/receipts/B1/versions").json()["versions"]) == 1)

        # ---- 批次号异内容复用 => 409 整批拒绝 ----
        r = import_batch("B1", [line("RX", "PAY-OK", "A", 100)])
        check("batch_no conflict 409", r.status_code == 409
              and r.json()["error"] == "receipt_batch_conflict", r.text)

        # ---- 回单编号异内容复用 => 409 整批拒绝，且整批不留痕 ----
        r = import_batch("B4", [line("R1", "PAY-OK", "A", 999),
                                line("R9", "PAY-FRESH2", "A", 5)])
        check("receipt_no conflict 409", r.status_code == 409
              and r.json()["error"] == "receipt_no_conflict"
              and r.json()["detail"]["receipt_no"] == "R1"
              and r.json()["detail"]["existing_amount"] == 100, r.text)
        check("B4 rejected leaves nothing",
              c.get("/receipts/B4").status_code == 404)
        r = import_batch("B5", [line("R9", "PAY-FRESH2", "A", 5)])
        check("sibling line of rejected batch reusable",
              r.status_code == 201, r.text)

        # ---- 复核：结论不变不新增版本 ----
        r = c.post("/receipts/B1/reverify")
        body = r.json()
        check("reverify unchanged", r.status_code == 200
              and body["status"] == "unchanged"
              and body["verification"]["version"] == 1, r.text)

        # ---- 付款迟到后复核：生成新版本 ----
        pay("A", "PAY-LATE", 50)
        r = c.post("/receipts/B1/reverify")
        body = r.json()
        check("reverify creates v2", r.status_code == 201
              and body["status"] == "created"
              and body["verification"]["version"] == 2, r.text)
        v2 = body["verification"]
        check("v2 counts",
              v2["counts"] == {"matched": 2, "payment_not_registered": 1,
                               "mismatched": 3, "duplicate": 0, "total": 6},
              str(v2["counts"]))
        check("v2 R5 matched and left differences",
              "R5" not in diff_by_no(v2) and "R6" in diff_by_no(v2))

        # 旧版本与原始回单始终可查
        old = c.get("/receipts/B1/versions/1").json()
        check("v1 still queryable with frozen conclusions",
              old["counts"]["payment_not_registered"] == 2
              and diff_by_no(old)["R5"]["status"] == "payment_not_registered",
              str(old["counts"]))
        detail = c.get("/receipts/B1").json()
        check("original receipts queryable",
              detail["line_count"] == 6 and detail["latest_version"] == 2
              and [x["receipt_no"] for x in detail["receipts"]]
              == [f"R{i}" for i in range(1, 7)], str(detail))

        # ---- 并发复核：付款到账后 8 路并发，恰好一个新版本 ----
        pay("B", "PAY-NEVER", 40)
        reverify_results = []

        def fire_reverify():
            rr = c.post("/receipts/B1/reverify")
            reverify_results.append((rr.status_code, rr.json()["status"]))

        ts = [threading.Thread(target=fire_reverify) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        created_n = sum(1 for s, _ in reverify_results if s == 201)
        unchanged_n = sum(1 for s, _ in reverify_results if s == 200)
        check("concurrent reverify exactly one new version",
              created_n == 1 and unchanged_n == 7,
              str(reverify_results))
        versions = c.get("/receipts/B1/versions").json()["versions"]
        check("B1 has 3 versions",
              [v["version"] for v in versions] == [1, 2, 3]
              and versions[2]["counts"]["matched"] == 3
              and versions[2]["counts"]["payment_not_registered"] == 0,
              str(versions))
        v3 = c.get("/receipts/B1/versions/3").json()
        check("v3 all resolved except mismatches",
              v3["counts"] == {"matched": 3, "payment_not_registered": 0,
                               "mismatched": 3, "duplicate": 0, "total": 6},
              str(v3["counts"]))
        check("reverify again unchanged",
              c.post("/receipts/B1/reverify").json()["status"] == "unchanged")

        # ---- 跨批次同一流水号：仅首次导入的参与核验，其余为重复回单 ----
        r = import_batch("B2", [line("R7", "PAY-OK", "A", 100),
                                line("R8", "PAY-FRESH", "A", 10)])
        body = r.json()
        check("B2 import 201", r.status_code == 201, r.text)
        dup_map = {x["receipt_no"]: x["is_duplicate"] for x in body["receipts"]}
        check("cross-batch same ref marked duplicate",
              dup_map == {"R7": True, "R8": False}, str(dup_map))
        check("B2 v1 counts",
              body["verification"]["counts"] == {
                  "matched": 0, "payment_not_registered": 1,
                  "mismatched": 0, "duplicate": 1, "total": 2},
              str(body["verification"]["counts"]))
        d = diff_by_no(body["verification"])
        check("duplicate receipt not verified against payment",
              d["R7"]["status"] == "duplicate"
              and d["R7"]["payment_holder_code"] is None, str(d["R7"]))
        check("B2 original receipts queryable",
              {x["receipt_no"]: x["is_duplicate"]
               for x in c.get("/receipts/B2").json()["receipts"]}
              == {"R7": True, "R8": False})

        # 同内容复用回单编号（跨批次）：不拒绝，按重复回单处理
        r = import_batch("B3", [line("R1", "PAY-OK", "A", 100)])
        body = r.json()
        check("same-content receipt_no reuse allowed as duplicate",
              r.status_code == 201
              and body["receipts"][0]["is_duplicate"] is True
              and body["verification"]["counts"]["duplicate"] == 1, r.text)

        # ---- 404：批次/版本不存在 ----
        check("batch 404", c.get("/receipts/NOPE").status_code == 404)
        check("versions 404", c.get("/receipts/NOPE/versions").status_code == 404)
        check("version 404 batch",
              c.get("/receipts/NOPE/versions/1").status_code == 404)
        r = c.get("/receipts/B1/versions/99")
        check("version 404", r.status_code == 404
              and r.json()["error"] == "receipt_version_not_found", r.text)
        r = c.post("/receipts/NOPE/reverify")
        check("reverify 404", r.status_code == 404
              and r.json()["error"] == "receipt_batch_not_found", r.text)

        # ---- 回单与结论不可变（触发器强制）----
        with db.pool.connection() as db_conn:
            frozen = []
            for sql in (
                "UPDATE receipts SET amount = 1 WHERE receipt_no = 'R1'",
                "DELETE FROM receipts WHERE receipt_no = 'R1'",
                "UPDATE receipt_batches SET line_count = 1 WHERE batch_no = 'B1'",
                "UPDATE receipt_verifications SET version = 9 WHERE batch_id = 1",
                "DELETE FROM receipt_verification_lines",
            ):
                try:
                    db_conn.execute(sql)
                    frozen.append(False)
                except psycopg.Error:
                    frozen.append(True)
        check("receipt data frozen update/delete", all(frozen), str(frozen))
        check("frozen data intact",
              c.get("/receipts/B1").json()["line_count"] == 6
              and len(c.get("/receipts/B1/versions").json()["versions"]) == 3)

        # ---- 并发导入：同批次同内容，恰好一个 created ----
        same_results = []

        def fire_same_import():
            rr = import_batch("B-CONC", [line("RC-1", "REF-CONC-1", "A", 7)])
            same_results.append((rr.status_code, rr.json()["status"]))

        ts = [threading.Thread(target=fire_same_import) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("same batch concurrent import serialized",
              sorted(same_results).count((201, "created")) == 1
              and sorted(same_results).count((200, "duplicate")) == 5,
              str(same_results))
        check("B-CONC single version",
              len(c.get("/receipts/B-CONC/versions").json()["versions"]) == 1)

        # ---- 并发导入：同批次号异内容，恰一个 201、其余 409 ----
        race_outcomes = []

        def fire_race(i):
            rr = import_batch("B-RACE", [line(f"RR-{i}", f"REF-RACE-{i}", "A", 1)])
            race_outcomes.append(rr.status_code)

        ts = [threading.Thread(target=fire_race, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("racing batch_no one created one conflict",
              sorted(race_outcomes) == [201, 409], str(race_outcomes))

        # ---- 并发导入：跨批次同流水号，首次判定确定（恰一个非重复）----
        shared = {}

        def fire_shared(name, rno):
            shared[name] = c.post(
                "/receipts/import",
                json={"batch_no": name,
                      "receipts": [line(rno, "REF-SHARED", "A", 10)]})

        ts = [threading.Thread(target=fire_shared, args=("B-X", "RX-1")),
              threading.Thread(target=fire_shared, args=("B-Y", "RY-1"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("shared ref both imported",
              all(shared[n].status_code == 201 for n in ("B-X", "B-Y")),
              str({n: shared[n].text for n in shared}))
        flags = []
        for n in ("B-X", "B-Y"):
            flags.extend(x["is_duplicate"]
                         for x in c.get(f"/receipts/{n}").json()["receipts"])
        check("exactly one first import participates",
              sorted(flags) == [False, True], str(flags))

        # ---- 并发：付款与复核并发，结论来自同一已提交状态 ----
        # 单线程顺序登记 SNAP-1..6（提交次序确定），另一线程持续复核；
        # 任一版本的"已登记流水号集合"必须是登记次序的前缀（单语句快照）。
        snap_refs = [f"SNAP-{i}" for i in range(1, 7)]
        r = import_batch("B-SNAP", [
            line(f"SR-{i}", snap_refs[i - 1], "A", 20) for i in range(1, 7)])
        check("B-SNAP all not registered",
              r.status_code == 201
              and r.json()["verification"]["counts"]["payment_not_registered"] == 6,
              r.text)

        stop = threading.Event()
        seen_versions = []
        snap_errors = []

        def reverify_loop():
            while not stop.is_set():
                rr = c.post("/receipts/B-SNAP/reverify")
                if rr.status_code not in (200, 201):
                    snap_errors.append((rr.status_code, rr.text))
                    return
                seen_versions.append(rr.json()["verification"])

        t = threading.Thread(target=reverify_loop)
        t.start()
        for ref in snap_refs:
            pay("A", ref, 20)
            time.sleep(0.05)
        time.sleep(0.2)
        stop.set()
        t.join()

        check("no errors during payment/reverify race", not snap_errors,
              str(snap_errors))
        prefix_ok = True
        for v in seen_versions:
            k = v["counts"]["matched"]
            not_reg = {d["external_ref"] for d in v["differences"]
                       if d["status"] == "payment_not_registered"}
            # 同一已提交状态：已登记集合必为提交次序 SNAP-1..SNAP-k 的前缀
            if not_reg != set(snap_refs[k:]):
                prefix_ok = False
        check("every version from one committed snapshot", prefix_ok
              and len(seen_versions) >= 1, f"{len(seen_versions)} versions")
        final = c.post("/receipts/B-SNAP/reverify").json()["verification"]
        check("final reverify all matched",
              final["counts"]["matched"] == 6
              and final["counts"]["payment_not_registered"] == 0,
              str(final["counts"]))
        snap_versions = c.get("/receipts/B-SNAP/versions").json()["versions"]
        check("snap versions contiguous",
              [v["version"] for v in snap_versions]
              == list(range(1, len(snap_versions) + 1)), str(len(snap_versions)))

        # ---- 核验不改写付款、余额或单据 ----
        led_a = c.get("/rightsholders/A/ledger").json()
        led_b = c.get("/rightsholders/B/ledger").json()
        check("verification never rewrites payments/balance/statements",
              led_a["billed_total"] == 600
              and led_a["paid_total"] == 100 + 300 + 50 + 6 * 20
              and led_a["balance"] == 600 - 570
              and len(led_a["statements"]) == 1
              and len(led_a["payments"]) == 9
              and led_b["billed_total"] == 400
              and led_b["paid_total"] == 200 + 150 + 40
              and led_b["balance"] == 10
              and len(led_b["payments"]) == 3,
              f"A={led_a['paid_total']}/{led_a['balance']} "
              f"B={led_b['paid_total']}/{led_b['balance']}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL RECEIPT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
