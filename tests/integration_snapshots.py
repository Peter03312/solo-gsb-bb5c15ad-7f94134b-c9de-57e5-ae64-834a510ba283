"""权利人余额核对快照（财务冻结底稿）：端到端集成测试。

pgserver 便携 PostgreSQL，无需 Docker。
运行: PYTHONPATH=. python3 tests/integration_snapshots.py

场景：
- 空库快照（无任何权利人）返回零总计；
- 全体/子集快照冻结口径与权利人对账单一致（余额=单据净额-付款+退回，
  可用额=余额-待确认预留），各项总计=逐行之和；
- 同号同集合重试幂等返回原快照（含省略 holder_codes 的默认全体）；
  同号异集合（含"全体"与"显式列全"）409；编号重复 422、
  权利人不存在 404，均整次拒绝不留快照；
- 分页读取冻结明细与各项总计；未知快照号 404；
- 冻结不改历史：后续出单/付款/退票/预留确认与取消/权利人建档后，
  旧快照（GET 与幂等重试）原样返回，新快照反映新状态；
- 并发：同号同集合并发创建恰一份快照；同号异集合并发恰一方成功；
  快照与付款/出单/退票/预留确认/取消并发时，整张快照来自同一
  已提交视图（全体权利人要么全是快照前状态、要么全是快照后状态）。
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_snap_")) / "pgdata"
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

    SNAP_KEYS = ("billed_total", "paid_total", "refunded_total",
                 "reserved_total", "balance", "available_balance")

    def ledger_state(holder):
        d = c.get(f"/rightsholders/{holder}/ledger").json()
        return {k: d[k] for k in SNAP_KEYS}

    def line_state(line):
        return {k: line[k] for k in SNAP_KEYS}

    def lines_by_holder(payload):
        return {l["holder_code"]: l for l in payload["lines"]}

    def snap_totals(states):
        return {
            "holder_count": len(states),
            **{k: sum(s[k] for s in states) for k in SNAP_KEYS},
        }

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # =============================================================
        # 一、空库快照：无任何权利人 => 零行、零总计
        # =============================================================
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-EMPTY", "holder_codes": []})
        check("empty db snapshot 201", r.status_code == 201, r.text)
        body = r.json()
        check("empty db snapshot zero totals",
              body["status"] == "created"
              and body["scope_all"] is True
              and body["holder_codes"] == []
              and body["lines"] == []
              and body["totals"] == {
                  "holder_count": 0, "billed_total": 0, "paid_total": 0,
                  "refunded_total": 0, "reserved_total": 0,
                  "balance": 0, "available_balance": 0},
              str(body))
        g = c.get("/balance-snapshots/SNAP-EMPTY")
        check("empty db snapshot GET zero totals",
              g.status_code == 200
              and g.json()["total_lines"] == 0
              and g.json()["lines"] == []
              and g.json()["totals"]["balance"] == 0
              and g.json()["totals"]["holder_count"] == 0, g.text)

        # =============================================================
        # 二、建档与流水：SA/SB 有单据、付款、退回、预留；SC 仅建档
        # =============================================================
        period1 = {"start": "2026-02-01T00:00:00+08:00",
                   "end": "2026-03-01T00:00:00+08:00"}
        c.post("/works", json={"code": "W-S1", "title": "快照作品一"})
        for h in ("SA", "SB", "SC"):
            c.post("/rightsholders", json={"code": h, "name": h})
        c.post("/agreements", json={
            "work_code": "W-S1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "SA", "percent": "60"},
                       {"holder_code": "SB", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P-S1", "work_code": "W-S1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json=period1)   # SA 600 / SB 400

        # SA：付款 100、退回 30、预留 60 后取消、预留 70 确认付款、预留 25 待确认
        c.post("/payments", json={
            "holder_code": "SA", "external_ref": "SPAY-1", "amount": 100})
        c.post("/refunds", json={
            "payment_ref": "SPAY-1", "refund_ref": "SRET-1", "amount": 30})
        c.post("/payment-reservations", json={
            "holder_code": "SA", "instruction_ref": "SCMD-A1", "amount": 60})
        c.post("/payment-reservations/SCMD-A1/cancel")
        c.post("/payment-reservations", json={
            "holder_code": "SA", "instruction_ref": "SCMD-A2", "amount": 70})
        c.post("/payment-reservations/SCMD-A2/confirm",
               json={"external_ref": "SPAY-2"})
        c.post("/payment-reservations", json={
            "holder_code": "SA", "instruction_ref": "SCMD-A3", "amount": 25})
        # SB：预留 50 待确认
        c.post("/payment-reservations", json={
            "holder_code": "SB", "instruction_ref": "SCMD-B1", "amount": 50})

        expected_pre = {
            "SA": {"billed_total": 600, "paid_total": 170,
                   "refunded_total": 30, "reserved_total": 25,
                   "balance": 460, "available_balance": 435},
            "SB": {"billed_total": 400, "paid_total": 0,
                   "refunded_total": 0, "reserved_total": 50,
                   "balance": 400, "available_balance": 350},
            "SC": {"billed_total": 0, "paid_total": 0,
                   "refunded_total": 0, "reserved_total": 0,
                   "balance": 0, "available_balance": 0},
        }
        for h, want in expected_pre.items():
            check(f"ledger matches expected for {h}",
                  ledger_state(h) == want,
                  f"{ledger_state(h)} != {want}")

        # =============================================================
        # 三、全体快照：口径与对账单一致、总计=逐行之和、恒等式成立
        # =============================================================
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-ALL", "holder_codes": []})
        check("create SNAP-ALL 201", r.status_code == 201, r.text)
        snap_all = r.json()
        check("SNAP-ALL meta",
              snap_all["status"] == "created"
              and snap_all["scope_all"] is True
              and snap_all["holder_codes"] == []
              and snap_all["totals"]["holder_count"] == 3, str(snap_all))
        got = lines_by_holder(snap_all)
        check("SNAP-ALL lines match ledger",
              set(got) == {"SA", "SB", "SC"}
              and all(line_state(got[h]) == expected_pre[h] for h in got),
              str(got))
        check("SNAP-ALL totals equal sum of lines",
              snap_all["totals"] == snap_totals(
                  [line_state(l) for l in snap_all["lines"]]),
              str(snap_all["totals"]))
        check("SNAP-ALL arithmetic identity per line",
              all(l["balance"] == l["billed_total"] - l["paid_total"]
                  + l["refunded_total"]
                  and l["available_balance"] == l["balance"]
                  - l["reserved_total"]
                  for l in snap_all["lines"]))

        # 同号同集合重试（显式空集合 / 省略 holder_codes）=> 200 原快照
        r1 = c.post("/balance-snapshots",
                    json={"snapshot_no": "SNAP-ALL", "holder_codes": []})
        r2 = c.post("/balance-snapshots", json={"snapshot_no": "SNAP-ALL"})
        check("SNAP-ALL retry duplicate 200",
              r1.status_code == 200 and r1.json()["status"] == "duplicate"
              and r2.status_code == 200
              and r2.json()["status"] == "duplicate", r1.text)
        for retry in (r1.json(), r2.json()):
            same = {k: v for k, v in retry.items() if k != "status"}
            orig = {k: v for k, v in snap_all.items() if k != "status"}
            check("SNAP-ALL retry returns original snapshot",
                  same == orig, str(retry))

        # 同号异集合 => 409（子集；"显式列全"也不同于"全体"）
        for codes in (["SA"], ["SA", "SB", "SC"]):
            r = c.post("/balance-snapshots",
                       json={"snapshot_no": "SNAP-ALL",
                             "holder_codes": codes})
            check("SNAP-ALL different set 409",
                  r.status_code == 409
                  and r.json()["error"] == "snapshot_conflict", r.text)
        check("SNAP-ALL intact after conflicts",
              c.get("/balance-snapshots/SNAP-ALL").json()["totals"]
              == snap_all["totals"])

        # =============================================================
        # 四、子集快照：集合无序幂等；显式集合规范化落库
        # =============================================================
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-SUB",
                         "holder_codes": ["SC", "SA"]})
        check("create SNAP-SUB 201", r.status_code == 201, r.text)
        snap_sub = r.json()
        check("SNAP-SUB canonical set and lines",
              snap_sub["scope_all"] is False
              and snap_sub["holder_codes"] == ["SA", "SC"]
              and [l["holder_code"] for l in snap_sub["lines"]]
              == ["SA", "SC"]
              and snap_sub["totals"]["holder_count"] == 2
              and snap_sub["totals"]["balance"] == 460, str(snap_sub))
        # 次序不同、集合相同 => 幂等返回原快照
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-SUB",
                         "holder_codes": ["SA", "SC"]})
        check("SNAP-SUB order-insensitive duplicate",
              r.status_code == 200 and r.json()["status"] == "duplicate"
              and r.json()["created_at"] == snap_sub["created_at"], r.text)
        # 集合不同 => 409
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-SUB", "holder_codes": ["SA"]})
        check("SNAP-SUB different set 409",
              r.status_code == 409
              and r.json()["error"] == "snapshot_conflict", r.text)

        # =============================================================
        # 五、整次拒绝：编号重复 422 / 权利人不存在 404，均不留快照
        # =============================================================
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-DUP",
                         "holder_codes": ["SA", "SB", "SA"]})
        check("duplicate holder code 422",
              r.status_code == 422
              and r.json()["error"] == "duplicate_holder_code", r.text)
        check("SNAP-DUP left nothing",
              c.get("/balance-snapshots/SNAP-DUP").status_code == 404)
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-GHOST",
                         "holder_codes": ["SA", "NOBODY"]})
        check("unknown holder 404",
              r.status_code == 404
              and r.json()["error"] == "holder_not_found"
              and r.json()["detail"]["missing_holder_codes"] == ["NOBODY"],
              r.text)
        check("SNAP-GHOST left nothing",
              c.get("/balance-snapshots/SNAP-GHOST").status_code == 404)
        # 非法入参 422：空快照号 / 空编号 / holder_codes 非数组
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "", "holder_codes": []})
        check("empty snapshot_no 422", r.status_code == 422, r.text)
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-V1", "holder_codes": [""]})
        check("blank holder code 422", r.status_code == 422, r.text)
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-V2", "holder_codes": "SA"})
        check("holder_codes not a list 422", r.status_code == 422, r.text)
        check("invalid requests left nothing",
              c.get("/balance-snapshots/SNAP-V1").status_code == 404
              and c.get("/balance-snapshots/SNAP-V2").status_code == 404)
        # 省略 holder_codes 等同空集合（全体）
        r = c.post("/balance-snapshots", json={"snapshot_no": "SNAP-DEFAULT"})
        check("omitted holder_codes means all",
              r.status_code == 201 and r.json()["scope_all"] is True
              and r.json()["totals"]["holder_count"] == 3, r.text)

        # =============================================================
        # 六、分页读取与未知快照号
        # =============================================================
        g1 = c.get("/balance-snapshots/SNAP-ALL?page=1&page_size=2").json()
        g2 = c.get("/balance-snapshots/SNAP-ALL?page=2&page_size=2").json()
        g3 = c.get("/balance-snapshots/SNAP-ALL?page=3&page_size=2").json()
        check("pagination pages",
              [l["holder_code"] for l in g1["lines"]] == ["SA", "SB"]
              and [l["holder_code"] for l in g2["lines"]] == ["SC"]
              and g3["lines"] == []
              and g1["total_lines"] == 3 and g2["total_lines"] == 3
              and g1["page"] == 1 and g2["page"] == 2
              and g1["page_size"] == 2, f"{g1} / {g2}")
        check("pagination totals constant across pages",
              g1["totals"] == g2["totals"] == g3["totals"]
              == snap_all["totals"])
        check("paginated lines concatenate to full set",
              [l["holder_code"] for l in g1["lines"] + g2["lines"]]
              == [l["holder_code"] for l in snap_all["lines"]])
        r = c.get("/balance-snapshots/SNAP-ALL?page=0")
        check("page 0 -> 422", r.status_code == 422, r.text)
        r = c.get("/balance-snapshots/SNAP-ALL?page_size=0")
        check("page_size 0 -> 422", r.status_code == 422, r.text)
        r = c.get("/balance-snapshots/SNAP-ALL?page_size=501")
        check("page_size 501 -> 422", r.status_code == 422, r.text)
        r = c.get("/balance-snapshots/SNAP-NOPE")
        check("unknown snapshot 404",
              r.status_code == 404
              and r.json()["error"] == "snapshot_not_found", r.text)

        # 冻结前的 GET 留档，供"不改历史"对照
        get_all_before = c.get("/balance-snapshots/SNAP-ALL").json()

        # =============================================================
        # 七、冻结不改历史：后续流水与建档后，旧快照原样、新快照更新
        # =============================================================
        # 追补：播放 1000 -> 1200（SA +120 / SB +80）
        c.post("/plays/import", json={
            "import_id": "P-S1-R1", "work_code": "W-S1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 1200,
            "corrects_id": "P-S1"})
        c.post("/statements/issue", json=period1)
        # SB：付款 80、退回 10、确认预留 SCMD-B1（付款 50）
        c.post("/payments", json={
            "holder_code": "SB", "external_ref": "SPAY-3", "amount": 80})
        c.post("/refunds", json={
            "payment_ref": "SPAY-3", "refund_ref": "SRET-9", "amount": 10})
        c.post("/payment-reservations/SCMD-B1/confirm",
               json={"external_ref": "SPAY-4"})
        # SA：取消待确认预留 SCMD-A3
        c.post("/payment-reservations/SCMD-A3/cancel")
        # 新权利人建档
        c.post("/rightsholders", json={"code": "SD", "name": "后建"})

        expected_post = {
            "SA": {"billed_total": 720, "paid_total": 170,
                   "refunded_total": 30, "reserved_total": 0,
                   "balance": 580, "available_balance": 580},
            "SB": {"billed_total": 480, "paid_total": 130,
                   "refunded_total": 10, "reserved_total": 0,
                   "balance": 360, "available_balance": 360},
            "SC": {"billed_total": 0, "paid_total": 0,
                   "refunded_total": 0, "reserved_total": 0,
                   "balance": 0, "available_balance": 0},
            "SD": {"billed_total": 0, "paid_total": 0,
                   "refunded_total": 0, "reserved_total": 0,
                   "balance": 0, "available_balance": 0},
        }
        for h, want in expected_post.items():
            check(f"post-flow ledger matches expected for {h}",
                  ledger_state(h) == want,
                  f"{ledger_state(h)} != {want}")

        check("SNAP-ALL GET unchanged after later flows",
              c.get("/balance-snapshots/SNAP-ALL").json() == get_all_before)
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-ALL", "holder_codes": []})
        check("SNAP-ALL duplicate retry still returns original",
              r.status_code == 200
              and r.json()["lines"] == snap_all["lines"]
              and r.json()["totals"] == snap_all["totals"], r.text)
        check("SNAP-SUB unchanged after later flows",
              c.get("/balance-snapshots/SNAP-SUB").json()["totals"]
              == snap_sub["totals"])

        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-ALL-2", "holder_codes": []})
        check("create SNAP-ALL-2 201", r.status_code == 201, r.text)
        got2 = lines_by_holder(r.json())
        check("SNAP-ALL-2 reflects new state incl. new holder",
              set(got2) == {"SA", "SB", "SC", "SD"}
              and all(line_state(got2[h]) == expected_post[h] for h in got2),
              str(got2))
        check("SNAP-ALL-2 totals",
              r.json()["totals"] == {
                  "holder_count": 4, "billed_total": 1200,
                  "paid_total": 300, "refunded_total": 40,
                  "reserved_total": 0, "balance": 940,
                  "available_balance": 940}, str(r.json()["totals"]))

        # =============================================================
        # 八、并发：同号同集合恰一份；同号异集合恰一方成功
        # =============================================================
        def fire_create(results, no, codes, barrier):
            barrier.wait()
            rr = c.post("/balance-snapshots",
                        json={"snapshot_no": no, "holder_codes": codes})
            results.append((rr.status_code,
                            rr.json().get("status"),
                            rr.json() if rr.status_code in (200, 201)
                            else rr.json().get("error")))

        results = []
        barrier = threading.Barrier(8)
        ts = [threading.Thread(target=fire_create,
                               args=(results, "SNAP-CONC", ["RA", "RB"],
                                     barrier))
              for _ in range(8)]
        # RA/RB 尚未建档 => 先建档再并发（否则全部 404）
        c.post("/rightsholders", json={"code": "RA", "name": "甲"})
        c.post("/rightsholders", json={"code": "RB", "name": "乙"})
        [t.start() for t in ts]
        [t.join() for t in ts]
        n_created = sum(1 for x in results if x[1] == "created")
        n_dup = sum(1 for x in results if x[1] == "duplicate")
        check("concurrent same no+set: exactly one created",
              n_created == 1 and n_dup == 7, str(results))
        payloads = {
            str(sorted((k, v) for k, v in x[2].items() if k != "status"))
            for x in results if x[1] in ("created", "duplicate")
        }
        check("concurrent same no+set: identical snapshot",
              len(payloads) == 1, str(results))

        # 同号异集合并发：恰一方集合成功（1 created + 3 duplicate），
        # 另一方全部 409 snapshot_conflict
        results2 = []
        barrier2 = threading.Barrier(8)
        ts = []
        for i in range(4):
            ts.append(threading.Thread(target=fire_create,
                                       args=(results2, "SNAP-CONC2", ["RA"],
                                             barrier2)))
            ts.append(threading.Thread(target=fire_create,
                                       args=(results2, "SNAP-CONC2", ["RB"],
                                             barrier2)))
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = [x for x in results2 if x[1] in ("created", "duplicate")]
        conflict = [x for x in results2 if x[2] == "snapshot_conflict"]
        win_codes = None
        if ok:
            win_codes = ok[0][2]["holder_codes"]
        check("concurrent same no different sets: exactly one set wins",
              len(ok) == 4 and len(conflict) == 4
              and sum(1 for x in ok if x[1] == "created") == 1
              and sum(1 for x in ok if x[1] == "duplicate") == 3
              and all(x[2]["holder_codes"] == win_codes for x in ok),
              str(results2))
        check("SNAP-CONC2 final set is the winning set",
              c.get("/balance-snapshots/SNAP-CONC2").json()["holder_codes"]
              == win_codes)

        # =============================================================
        # 九、并发一致性：快照与流水并发时不混合前后状态
        # =============================================================
        period2 = {"start": "2026-04-01T00:00:00+08:00",
                   "end": "2026-05-01T00:00:00+08:00"}
        c.post("/works", json={"code": "W-S2", "title": "快照作品二"})
        c.post("/agreements", json={
            "work_code": "W-S2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "RA", "percent": "50"},
                       {"holder_code": "RB", "percent": "50"}]})
        c.post("/plays/import", json={
            "import_id": "P-S2", "work_code": "W-S2",
            "occurred_at": "2026-04-10T20:00:00+08:00", "amount": 2000})
        c.post("/statements/issue", json=period2)   # RA 1000 / RB 1000
        # R3/R4 要用的预留指令提前建好
        c.post("/payment-reservations", json={
            "holder_code": "RB", "instruction_ref": "RCMD-B", "amount": 40})

        def op_payment(holder, ref, amount):
            def fn():
                rr = c.post("/payments", json={
                    "holder_code": holder, "external_ref": ref,
                    "amount": amount})
                assert rr.status_code == 201, rr.text
            return fn

        def op_correct_and_issue():
            def fn():
                rr = c.post("/plays/import", json={
                    "import_id": "P-S2-R1", "work_code": "W-S2",
                    "occurred_at": "2026-04-10T20:00:00+08:00",
                    "amount": 1600, "corrects_id": "P-S2"})
                assert rr.status_code in (200, 201), rr.text
                rr = c.post("/statements/issue", json=period2)
                assert rr.status_code in (200, 201), rr.text
            return fn

        def op_refund(payment_ref, refund_ref, amount):
            def fn():
                rr = c.post("/refunds", json={
                    "payment_ref": payment_ref, "refund_ref": refund_ref,
                    "amount": amount})
                assert rr.status_code == 201, rr.text
            return fn

        def op_confirm(instr, ref):
            def fn():
                rr = c.post(f"/payment-reservations/{instr}/confirm",
                            json={"external_ref": ref})
                assert rr.status_code == 201, rr.text
            return fn

        def op_reserve_and_cancel(instr, amount):
            # 先建好待确认指令，竞态时取消（释放预留）
            def fn():
                rr = c.post(f"/payment-reservations/{instr}/cancel")
                assert rr.status_code == 200, rr.text
            c.post("/payment-reservations", json={
                "holder_code": "RA", "instruction_ref": instr,
                "amount": amount})
            return fn

        # 每个并发操作声明其对对账口径的原子影响（effects，按字段加减）。
        # 一致性判据：快照必须等于"流水前状态 + 本轮操作的某个子集整体生效"
        # —— 任何操作要么完整落在快照视图之前、要么完整落在之后，
        # 绝不出现半个操作（如出单追补只改了 RA 没改 RB）的混合状态。
        def pay_fx(holder, amount):
            return {holder: {"paid_total": amount, "balance": -amount,
                             "available_balance": -amount}}

        def issue_fx(delta):
            return {h: {"billed_total": delta, "balance": delta,
                        "available_balance": delta} for h in ("RA", "RB")}

        def refund_fx(holder, amount):
            return {holder: {"refunded_total": amount, "balance": amount,
                             "available_balance": amount}}

        def confirm_fx(holder, amount):
            return {holder: {"paid_total": amount, "reserved_total": -amount,
                             "balance": -amount}}

        def cancel_fx(holder, amount):
            return {holder: {"reserved_total": -amount,
                             "available_balance": amount}}

        rounds = [
            ("SNAP-R1", [(op_payment("RA", "RPAY-A1", 10), pay_fx("RA", 10)),
                         (op_payment("RB", "RPAY-B1", 20), pay_fx("RB", 20))]),
            ("SNAP-R2", [(op_correct_and_issue(), issue_fx(-200)),
                         (op_payment("RA", "RPAY-A2", 30), pay_fx("RA", 30))]),
            ("SNAP-R3", [(op_refund("RPAY-A1", "RRET-1", 5),
                          refund_fx("RA", 5)),
                         (op_confirm("RCMD-B", "RPAY-B2"),
                          confirm_fx("RB", 40))]),
            ("SNAP-R4", [(op_reserve_and_cancel("RCMD-A", 15),
                          cancel_fx("RA", 15)),
                         (op_payment("RB", "RPAY-B3", 25),
                          pay_fx("RB", 25))]),
        ]
        for snap_no, ops in rounds:
            pre = {h: ledger_state(h) for h in ("RA", "RB")}
            barrier = threading.Barrier(len(ops) + 1)
            snap_result = []

            def fire_snap(barrier=barrier, snap_no=snap_no,
                          snap_result=snap_result):
                barrier.wait()
                rr = c.post("/balance-snapshots",
                            json={"snapshot_no": snap_no,
                                  "holder_codes": ["RA", "RB"]})
                snap_result.append(rr)

            threads = [threading.Thread(target=fire_snap)]
            for op, _fx in ops:
                def wrapped(op=op, barrier=barrier):
                    barrier.wait()
                    op()
                threads.append(threading.Thread(target=wrapped))
            [t.start() for t in threads]
            [t.join() for t in threads]

            rr = snap_result[0]
            check(f"{snap_no} created under concurrency",
                  rr.status_code == 201, rr.text)
            lines = lines_by_holder(rr.json())
            # 恒等式：每行 余额=单据净额-付款+退回、可用额=余额-待确认预留
            check(f"{snap_no} arithmetic identity",
                  all(l["balance"] == l["billed_total"] - l["paid_total"]
                      + l["refunded_total"]
                      and l["available_balance"] == l["balance"]
                      - l["reserved_total"]
                      for l in lines.values()), str(lines))
            # 不混合前后状态：存在某个操作子集 S，使快照 = 前状态 + S 整体生效
            # （每个并发操作作为原子单位，要么全在视图内、要么全在视图外）
            got = {h: line_state(lines[h]) for h in ("RA", "RB")}
            reachable = False
            for mask in range(1 << len(ops)):
                state = {h: dict(s) for h, s in pre.items()}
                for i, (_op, fx) in enumerate(ops):
                    if not (mask >> i) & 1:
                        continue
                    for h, delta in fx.items():
                        for k, d in delta.items():
                            state[h][k] += d
                if state == got:
                    reachable = True
                    break
            check(f"{snap_no} single committed view (no torn ops)",
                  reachable, f"got={got} pre={pre}")

        # 终态对账：全部流水落定后，实时对账单 = 新快照
        final_expected = {
            "RA": {"billed_total": 800, "paid_total": 40,
                   "refunded_total": 5, "reserved_total": 0,
                   "balance": 765, "available_balance": 765},
            "RB": {"billed_total": 800, "paid_total": 85,
                   "refunded_total": 0, "reserved_total": 0,
                   "balance": 715, "available_balance": 715},
        }
        for h, want in final_expected.items():
            check(f"final ledger {h}", ledger_state(h) == want,
                  f"{ledger_state(h)} != {want}")
        r = c.post("/balance-snapshots",
                   json={"snapshot_no": "SNAP-FINAL",
                         "holder_codes": ["RA", "RB"]})
        gotf = lines_by_holder(r.json())
        check("final snapshot matches live ledger",
              all(line_state(gotf[h]) == final_expected[h]
                  for h in ("RA", "RB")), str(gotf))

        # =============================================================
        # 十、既有接口兼容 + 建表脚本幂等重跑
        # =============================================================
        led = c.get("/rightsholders/SA/ledger").json()
        check("ledger interface compatible",
              all(k in led for k in (
                  "billed_total", "paid_total", "refunded_total",
                  "reserved_total", "balance", "available_balance",
                  "statements", "payments", "refunds", "instructions"))
              and led["balance"] == 580, str(led))
        with db.pool.connection() as db_conn:
            db_conn.execute(db.SCHEMA_SQL)
        check("schema idempotent re-run",
              c.get("/health").json()["status"] == "ok"
              and c.get("/balance-snapshots/SNAP-ALL").json()
              == get_all_before)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL SNAPSHOT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
