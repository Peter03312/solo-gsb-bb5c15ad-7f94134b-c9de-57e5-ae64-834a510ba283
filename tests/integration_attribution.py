"""逐笔归因证据的端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_attribution.py
"""
import os
import pathlib
import tempfile

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_attr_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    from app.main import app
    from app import db as dbmod

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def agree(c, work, frm, to, shares):
        r = c.post("/agreements", json={
            "work_code": work, "valid_from": frm, "valid_to": to,
            "shares": [{"holder_code": h, "percent": p} for h, p in shares]})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    with TestClient(app) as c:
        c.post("/works", json={"code": "W1", "title": "作品一"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        c.post("/works", json={"code": "W3", "title": "旧账作品"})
        aid1 = agree(c, "W1", "2026-01-01T00:00:00+08:00",
                     "2027-01-01T00:00:00+08:00",
                     [("A", "62.5"), ("B", "30"), ("C", "7.5")])
        feb = {"start": "2026-02-01T00:00:00+08:00",
               "end": "2026-03-01T00:00:00+08:00"}
        mar = {"start": "2026-03-01T00:00:00+08:00",
               "end": "2026-04-01T00:00:00+08:00"}
        apr = {"start": "2026-04-01T00:00:00+08:00",
               "end": "2026-05-01T00:00:00+08:00"}

        # ---- 两条播放，出具初次单 ----
        for pid, amt, day in (("P1", 100, "10"), ("P2", 80, "11")):
            r = c.post("/plays/import", json={
                "import_id": pid, "work_code": "W1",
                "occurred_at": f"2026-02-{day}T20:00:00+08:00", "amount": amt})
            check(f"{pid} created", r.status_code == 201)

        r = c.post("/statements/issue", json=feb)
        b0 = r.json()
        check("initial traceable", b0["traceable"] is True, r.text)
        # 100 => 63/30/7；80 => 50/24/6；合计 113/54/13
        line_amt = {x["holder_code"]: x["amount"] for x in b0["payable"]}
        check("initial payable", line_amt == {"A": 113, "B": 54, "C": 13}, str(line_amt))

        attr0 = c.get(f"/statements/{b0['batch_id']}/attributions").json()
        recs = {(x["root_import_id"], x["holder_code"]): x for x in attr0["records"]}
        # 按更正链原始编号归集，冻结链尾/作品/时间/协议/比例/逐笔分摊
        row = recs[("P1", "A")]
        check("initial frozen ctx",
              row["change_type"] == "added" and row["tail_import_id"] == "P1"
              and row["work_code"] == "W1" and row["agreement_id"] == aid1
              and row["tail_amount"] == 100 and row["share_percent"] == "62.5000"
              and row["amount"] == 63, str(row))
        check("initial per-play amounts",
              recs[("P1", "B")]["amount"] == 30 and recs[("P1", "C")]["amount"] == 7
              and recs[("P2", "A")]["amount"] == 50 and recs[("P2", "B")]["amount"] == 24
              and recs[("P2", "C")]["amount"] == 6)
        check("initial totals reconcile",
              all(t["reconciles"] for t in attr0["holder_totals"]),
              str(attr0["holder_totals"]))
        totals0 = {t["holder_code"]: t["attributed_total"] for t in attr0["holder_totals"]}
        check("initial totals equal lines", totals0 == line_amt, str(totals0))
        check("total records 6", attr0["total_records"] == 6 and len(attr0["records"]) == 6)

        # ---- 分页 + 权利人过滤 ----
        p1 = c.get(f"/statements/{b0['batch_id']}/attributions",
                   params={"page": 1, "page_size": 2}).json()
        p2 = c.get(f"/statements/{b0['batch_id']}/attributions",
                   params={"page": 3, "page_size": 2}).json()
        check("paging sizes", len(p1["records"]) == 2 and len(p2["records"]) == 2
              and p1["total_records"] == 6 and p1["holder_totals"] == p2["holder_totals"])
        pa = c.get(f"/statements/{b0['batch_id']}/attributions",
                   params={"holder_code": "A"}).json()
        check("holder filter", pa["total_records"] == 2
              and {t["holder_code"] for t in pa["holder_totals"]} == {"A"}
              and pa["holder_totals"][0]["attributed_total"] == 113)

        # ---- 更正 P1 100->80 后追补：撤销与新增可辨，负额保留 ----
        r = c.post("/plays/import", json={
            "import_id": "P1R1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 80,
            "corrects_id": "P1"})
        check("correction", r.status_code == 201)
        r = c.post("/statements/issue", json=feb)
        b1 = r.json()
        check("supplement kind", b1["kind"] == "supplement", r.text)
        delta = {x["holder_code"]: x["delta"] for x in b1["payable"]}
        # P1 链 63/30/7 -> 50/24/6，差额 -13/-6/-1
        check("supplement deltas", delta == {"A": -13, "B": -6, "C": -1}, str(delta))

        attr1 = c.get(f"/statements/{b1['batch_id']}/attributions").json()
        recs1 = {(x["root_import_id"], x["holder_code"], x["change_type"]): x
                 for x in attr1["records"]}
        check("supplement record count", len(attr1["records"]) == 6, str(len(attr1["records"])))
        ra = recs1[("P1", "A", "removed")]
        check("removed negative kept",
              ra["amount"] == -63 and ra["tail_import_id"] == "P1"
              and ra["tail_amount"] == 100 and ra["share_percent"] == "62.5000",
              str(ra))
        na = recs1[("P1", "A", "added")]
        check("added new tail",
              na["amount"] == 50 and na["tail_import_id"] == "P1R1"
              and na["tail_amount"] == 80 and na["root_import_id"] == "P1",
              str(na))
        check("removed b/c",
              recs1[("P1", "B", "removed")]["amount"] == -30
              and recs1[("P1", "C", "removed")]["amount"] == -7
              and recs1[("P1", "B", "added")]["amount"] == 24
              and recs1[("P1", "C", "added")]["amount"] == 6)
        # P2 未变化，不得在追补证据中重复出现
        check("unchanged chain absent",
              all(x["root_import_id"] != "P2" for x in attr1["records"]))
        totals1 = {t["holder_code"]: (t["attributed_total"], t["reconciles"])
                   for t in attr1["holder_totals"]}
        check("supplement totals reconcile",
              totals1 == {"A": (-13, True), "B": (-6, True), "C": (-1, True)},
              str(totals1))

        # ---- 历史证据不可变：初次单证据仍是旧链尾 P1/100 ----
        old = c.get(f"/statements/{b0['batch_id']}/attributions").json()
        old_p1a = [x for x in old["records"]
                   if x["root_import_id"] == "P1" and x["holder_code"] == "A"]
        check("history immutable",
              len(old_p1a) == 1 and old_p1a[0]["tail_import_id"] == "P1"
              and old_p1a[0]["amount"] == 63, str(old_p1a))

        # 数据库层冻结触发器：UPDATE/DELETE 直接拒绝
        with dbmod.pool.connection() as conn:
            for sql in (
                "UPDATE statement_attributions SET amount = 1 WHERE batch_id = %s"
                % b0["batch_id"],
                "DELETE FROM statement_attributions WHERE batch_id = %s"
                % b0["batch_id"],
            ):
                try:
                    conn.execute(sql)
                    check("frozen trigger blocks " + sql[:6], False)
                except Exception as exc:  # noqa
                    check("frozen trigger blocks " + sql[:6], "frozen" in str(exc), str(exc))

        # ---- 无应付变化：不出新单、不生成证据 ----
        before = c.get("/statements", params=feb).json()
        r = c.post("/statements/issue", json=feb)
        check("no new batch", r.json()["batch_id"] == b1["batch_id"])
        after = c.get("/statements", params=feb).json()
        check("batch list unchanged", len(before) == len(after))

        # ---- 待处理播放不计入应付；协议后补再追补 ----
        r = c.post("/plays/import", json={
            "import_id": "Q1", "work_code": "W2",
            "occurred_at": "2026-03-05T21:00:00+08:00", "amount": 200})
        check("q1 import", r.status_code == 201)
        r = c.post("/statements/issue", json=mar)
        mb0 = r.json()
        check("march initial pending", mb0["kind"] == "initial"
              and mb0["payable"] == []
              and len(mb0["pending"]) == 1 and mb0["pending"][0]["import_id"] == "Q1")
        mattr = c.get(f"/statements/{mb0['batch_id']}/attributions").json()
        check("pending no evidence",
              mattr["total_records"] == 0 and mattr["holder_totals"] == [])
        agree(c, "W2", "2026-01-01T00:00:00+08:00", None, [("X", "100")])
        r = c.post("/statements/issue", json=mar)
        mb1 = r.json()
        check("march supplement payable",
              mb1["kind"] == "supplement"
              and mb1["payable"][0]["holder_code"] == "X"
              and mb1["payable"][0]["delta"] == 200, r.text)
        mattr1 = c.get(f"/statements/{mb1['batch_id']}/attributions").json()
        check("backfilled evidence", len(mattr1["records"]) == 1
              and mattr1["records"][0]["root_import_id"] == "Q1"
              and mattr1["records"][0]["change_type"] == "added"
              and mattr1["records"][0]["amount"] == 200
              and mattr1["holder_totals"][0]["reconciles"] is True,
              str(mattr1))

        # ---- 存量无证据单据：不可追溯；旧账差额单列 legacy ----
        with dbmod.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO statement_batches
                        (period_start, period_end, kind, evidence_status)
                    VALUES (TIMESTAMPTZ '2026-04-01T00:00:00+08:00',
                            TIMESTAMPTZ '2026-05-01T00:00:00+08:00',
                            'initial', 'legacy_untraceable')
                    RETURNING id
                    """
                ).fetchone()
                legacy_batch_id = row["id"]
                conn.execute(
                    "INSERT INTO statement_lines(batch_id, holder_code, amount) "
                    "VALUES (%s, 'X', 50)",
                    (legacy_batch_id,))
                # 当时的播放如今仍在库里，但没有证据
                conn.execute(
                    """
                    INSERT INTO plays(import_id, work_code, occurred_at, amount)
                    VALUES ('L1', 'W3', TIMESTAMPTZ '2026-04-05T20:00:00+08:00', 50)
                    """)

        leg = c.get(f"/statements/{legacy_batch_id}/attributions").json()
        check("legacy flagged", leg["traceable"] is False and leg["records"] == []
              and leg["total_records"] == 0, str(leg))
        check("legacy totals not reconcilable",
              leg["holder_totals"] == [{
                  "holder_code": "X", "attributed_total": 0,
                  "statement_line_amount": 50, "reconciles": False}],
              str(leg["holder_totals"]))
        lst = c.get("/statements", params=apr).json()
        check("list flags legacy",
              any(b["batch_id"] == legacy_batch_id and b["traceable"] is False
                  for b in lst), str(lst))

        # 给 W3 补协议（100% 给 X），并把旧播放更正为 30 后追补：
        # 旧账 -50 单列 legacy，当前链 L1->L2 新增 30，单据差额 -20
        agree(c, "W3", "2026-01-01T00:00:00+08:00", None, [("X", "100")])
        r = c.post("/plays/import", json={
            "import_id": "L2", "work_code": "W3",
            "occurred_at": "2026-04-05T20:00:00+08:00", "amount": 30,
            "corrects_id": "L1"})
        check("l2 correction", r.status_code == 201, r.text)
        r = c.post("/statements/issue", json=apr)
        ab1 = r.json()
        check("legacy-era supplement delta",
              ab1["kind"] == "supplement"
              and ab1["payable"][0]["delta"] == -20, r.text)
        aattr = c.get(f"/statements/{ab1['batch_id']}/attributions").json()
        by_type = {}
        for x in aattr["records"]:
            by_type.setdefault(x["change_type"], []).append(x)
        check("legacy row separate",
              len(by_type.get("legacy", [])) == 1
              and by_type["legacy"][0]["amount"] == -50
              and by_type["legacy"][0]["root_import_id"] is None
              and by_type["legacy"][0]["tail_import_id"] is None,
              str(by_type.get("legacy")))
        check("current play not masquerading",
              len(by_type.get("added", [])) == 1
              and by_type["added"][0]["root_import_id"] == "L1"
              and by_type["added"][0]["tail_import_id"] == "L2"
              and by_type["added"][0]["amount"] == 30,
              str(by_type.get("added")))
        check("no fabricated removed over legacy", "removed" not in by_type)
        check("legacy-era reconcile",
              aattr["holder_totals"] == [{
                  "holder_code": "X", "attributed_total": -20,
                  "statement_line_amount": -20, "reconciles": True}],
              str(aattr["holder_totals"]))

        # 再更正 L2(30)->L3(60)：逐链撤销 -30 + 新增 60，legacy 不重复出现
        r = c.post("/plays/import", json={
            "import_id": "L3", "work_code": "W3",
            "occurred_at": "2026-04-05T20:00:00+08:00", "amount": 60,
            "corrects_id": "L2"})
        check("l3 correction", r.status_code == 201, r.text)
        r = c.post("/statements/issue", json=apr)
        ab2 = r.json()
        check("second supplement +30", ab2["payable"][0]["delta"] == 30, r.text)
        aattr2 = c.get(f"/statements/{ab2['batch_id']}/attributions").json()
        types2 = [x["change_type"] for x in aattr2["records"]]
        check("legacy only once", types2 == ["removed", "added"], str(types2))
        rm = [x for x in aattr2["records"] if x["change_type"] == "removed"][0]
        ad = [x for x in aattr2["records"] if x["change_type"] == "added"][0]
        check("removed/add tails",
              rm["amount"] == -30 and rm["tail_import_id"] == "L2"
              and ad["amount"] == 60 and ad["tail_import_id"] == "L3"
              and rm["root_import_id"] == ad["root_import_id"] == "L1",
              str(aattr2["records"]))

        # 全部单据累计（行）== 最新归因链累计 == 当前应付
        r = c.get("/statements", params=feb)
        cum = {}
        for b in r.json():
            for l in b["lines"]:
                cum[l["holder_code"]] = cum.get(l["holder_code"], 0) + l["amount"]
        check("feb cumulative 100? ", cum == {"A": 100, "B": 48, "C": 12}, str(cum))

        # ---- 错误场景：不存在单据 / 非法分页 / 不存在权利人 ----
        check("missing batch 404",
              c.get("/statements/999999/attributions").status_code == 404)
        check("page zero 422",
              c.get(f"/statements/{b0['batch_id']}/attributions",
                    params={"page": 0}).status_code == 422)
        check("page_size huge 422",
              c.get(f"/statements/{b0['batch_id']}/attributions",
                    params={"page_size": 501}).status_code == 422)
        check("missing holder 404",
              c.get(f"/statements/{b0['batch_id']}/attributions",
                    params={"holder_code": "NOBODY"}).status_code == 404)
        # 已登记但本单无记录：合法空结果
        c.post("/rightsholders", json={"code": "Z", "name": "无关方"})
        rz = c.get(f"/statements/{b0['batch_id']}/attributions",
                   params={"holder_code": "Z"})
        check("unrelated holder empty",
              rz.status_code == 200 and rz.json()["records"] == []
              and rz.json()["holder_totals"] == [], rz.text)
        # 期间非法
        check("bad period 422",
              c.post("/statements/issue", json={
                  "start": "2026-03-01T00:00:00+08:00",
                  "end": "2026-02-01T00:00:00+08:00"}).status_code == 422)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL ATTRIBUTION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
