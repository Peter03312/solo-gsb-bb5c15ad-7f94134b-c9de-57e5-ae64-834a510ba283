import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from .allocation import allocate
from .payments_service import lock_holders_for_billing

# 咨询锁：使用单 bigint 键，高 32 位为服务命名空间、低 32 位为期间键
ADVISORY_NS = 0x524F5941  # "ROYA"

# 证据性质
ADDED = "added"
REMOVED = "removed"
LEGACY = "legacy"


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


# 当期"有效播放"：仅更正链链尾参与核算；同时沿 corrects_id 回溯出链的原始编号。
# 递归从链尾出发向"更原始"方向走，depth 最大处 walk_id 即原始编号（root）。
# LEFT JOIN 按发生时间匹配协议（协议区间不重叠，最多命中一条）。
EFFECTIVE_PLAYS_SQL = """
WITH RECURSIVE chain AS (
    SELECT import_id AS tail_id, import_id AS walk_id, 0 AS depth
    FROM plays p
    WHERE p.occurred_at >= %(start)s
      AND p.occurred_at <  %(end)s
      AND NOT EXISTS (SELECT 1 FROM plays c WHERE c.corrects_id = p.import_id)
    UNION ALL
    SELECT ch.tail_id, p.corrects_id, ch.depth + 1
    FROM chain ch
    JOIN plays p ON p.import_id = ch.walk_id
    WHERE p.corrects_id IS NOT NULL AND ch.depth < 10000
)
SELECT
    ch.tail_id AS import_id,
    ch.root_id,
    p.work_code,
    p.occurred_at,
    p.amount,
    a.id AS agreement_id
FROM (
    SELECT DISTINCT ON (tail_id) tail_id, walk_id AS root_id
    FROM chain
    ORDER BY tail_id, depth DESC
) ch
JOIN plays p ON p.import_id = ch.tail_id
LEFT JOIN agreements a
    ON a.work_code = p.work_code
   AND tstzrange(a.valid_from, a.valid_to, '[)') @> p.occurred_at
ORDER BY p.occurred_at, ch.tail_id
"""


def canonical_period(start: datetime, end: datetime) -> tuple[datetime, datetime, str]:
    """统一按 UTC 存储/比较期间，返回 (start_utc, end_utc, key)。"""
    s = start.astimezone(timezone.utc)
    e = end.astimezone(timezone.utc)
    return s, e, f"{s.isoformat()}|{e.isoformat()}"


def _period_lock_key(key: str) -> int:
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    # 转成 int8 有符号数
    return n - (1 << 64) if n >= (1 << 63) else n


def _pending_payload(rows) -> list[dict]:
    return [
        {
            "import_id": r["import_id"],
            "work_code": r["work_code"],
            "occurred_at": r["occurred_at"],
            "amount": r["amount"],
            "reason": r["reason"],
        }
        for r in rows
    ]


def compute_current(conn, start: datetime, end: datetime):
    """按当前最新数据（后补协议、更正后的链尾）重算期间。

    返回 (payable, pending, chains)：
      payable: holder_code -> 当前应付合计
      pending: 无匹配协议的链尾播放（待处理，不计入应付）
      chains:  每条参与核算的链的冻结上下文与逐笔分摊，形如
        {
          "root_id", "tail_id", "work_code", "occurred_at", "amount",
          "agreement_id", "shares": {holder: percent},
          "parts":  {holder: 逐笔分摊整数},
        }
    """
    rows = conn.execute(
        EFFECTIVE_PLAYS_SQL, {"start": start, "end": end}
    ).fetchall()

    agreement_ids = {r["agreement_id"] for r in rows if r["agreement_id"] is not None}
    shares_map: dict[int, dict[str, Decimal]] = {}
    if agreement_ids:
        share_rows = conn.execute(
            """
            SELECT agreement_id, holder_code, percent
            FROM agreement_shares
            WHERE agreement_id = ANY(%s)
            ORDER BY holder_code
            """,
            (list(agreement_ids),),
        ).fetchall()
        for sr in share_rows:
            shares_map.setdefault(sr["agreement_id"], {})[sr["holder_code"]] = sr["percent"]

    payable: dict[str, int] = {}
    pending: list[dict] = []
    chains: list[dict] = []
    for r in rows:
        if r["agreement_id"] is None:
            pending.append(
                {
                    "import_id": r["import_id"],
                    "work_code": r["work_code"],
                    "occurred_at": r["occurred_at"],
                    "amount": r["amount"],
                    "reason": "no matching agreement at play occurrence time",
                }
            )
            continue
        shares = shares_map[r["agreement_id"]]
        # 逐笔按比例分到最小货币单位，余数按规则补足
        parts = allocate(r["amount"], shares)
        for holder, amt in parts.items():
            if amt:
                payable[holder] = payable.get(holder, 0) + amt
        chains.append(
            {
                "root_id": r["root_id"],
                "tail_id": r["import_id"],
                "work_code": r["work_code"],
                "occurred_at": r["occurred_at"],
                "amount": r["amount"],
                "agreement_id": r["agreement_id"],
                "shares": shares,
                "parts": parts,
            }
        )

    pending.sort(key=lambda x: (x["occurred_at"], x["import_id"]))
    return payable, pending, chains


def _load_pending_snapshot(conn, start: datetime, end: datetime):
    rows = conn.execute(
        """
        SELECT import_id, work_code, occurred_at, amount, reason
        FROM statement_pending
        WHERE period_start = %s AND period_end = %s
        ORDER BY occurred_at, import_id
        """,
        (start, end),
    ).fetchall()
    return _pending_payload(rows)


def _load_prior_payable(conn, start: datetime, end: datetime) -> dict[str, int]:
    """此前全部单据明细行的累计额（= 已入账应付）。"""
    rows = conn.execute(
        """
        SELECT l.holder_code, COALESCE(SUM(l.amount), 0) AS total
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
        GROUP BY l.holder_code
        """,
        (start, end),
    ).fetchall()
    return {r["holder_code"]: r["total"] for r in rows}


def _load_prior_traced(conn, start: datetime, end: datetime) -> dict[tuple[str, str], int]:
    """此前全部单据中可逐笔追溯的证据累计额，键为 (root_import_id, holder)。

    removed（负）与 added（正）自然相抵，得到每条链当前仍在账的逐笔分摊。
    """
    rows = conn.execute(
        """
        SELECT a.root_import_id, a.holder_code, SUM(a.amount) AS total
        FROM statement_attributions a
        JOIN statement_batches b ON b.id = a.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
          AND a.change_type <> %s
        GROUP BY a.root_import_id, a.holder_code
        """,
        (start, end, LEGACY),
    ).fetchall()
    return {(r["root_import_id"], r["holder_code"]): r["total"] for r in rows}


def _load_latest_evidence_ctx(conn, start, end):
    """每条 (链, 权利人) 最近一次证据的冻结上下文。

    用于追补单撤销旧账时回填旧链尾/旧协议等信息；取 batch 最新、
    同批内 added 优先于 removed（added 携带当前上下文）。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (a.root_import_id, a.holder_code)
               a.root_import_id, a.holder_code,
               a.tail_import_id, a.work_code, a.occurred_at,
               a.agreement_id, a.tail_amount, a.share_percent, a.amount
        FROM statement_attributions a
        JOIN statement_batches b ON b.id = a.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
          AND a.change_type <> %s
        ORDER BY a.root_import_id, a.holder_code,
                 b.id DESC,
                 CASE a.change_type WHEN %s THEN 0 ELSE 1 END, a.id
        """,
        (start, end, LEGACY, ADDED),
    ).fetchall()
    return {(r["root_import_id"], r["holder_code"]): r for r in rows}


def _insert_pending_snapshot(conn, start: datetime, end: datetime, pending: list[dict]):
    conn.execute(
        "DELETE FROM statement_pending WHERE period_start = %s AND period_end = %s",
        (start, end),
    )
    if pending:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO statement_pending
                    (period_start, period_end, import_id, work_code, occurred_at, amount, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (start, end, p["import_id"], p["work_code"], p["occurred_at"],
                     p["amount"], p["reason"])
                    for p in pending
                ],
            )


def _latest_batch(conn, start: datetime, end: datetime):
    return conn.execute(
        """
        SELECT id, kind, supersedes_batch_id, created_at, evidence_status
        FROM statement_batches
        WHERE period_start = %s AND period_end = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (start, end),
    ).fetchone()


def _insert_batch(conn, start, end, kind, supersedes, lines: dict[str, int]):
    row = conn.execute(
        """
        INSERT INTO statement_batches
            (period_start, period_end, kind, supersedes_batch_id, evidence_status)
        VALUES (%s, %s, %s, %s, 'traceable')
        RETURNING id, created_at
        """,
        (start, end, kind, supersedes),
    ).fetchone()
    if lines:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO statement_lines(batch_id, holder_code, amount) VALUES (%s, %s, %s)",
                [(row["id"], holder, amt) for holder, amt in lines.items()],
            )
    return row["id"], row["created_at"]


# ---- 证据行构造 ----

def _play_evidence(ch: dict, holder: str, amount: int, change_type: str) -> dict:
    return {
        "holder_code": holder,
        "change_type": change_type,
        "root_import_id": ch["root_id"],
        "tail_import_id": ch["tail_id"],
        "work_code": ch["work_code"],
        "occurred_at": ch["occurred_at"],
        "agreement_id": ch["agreement_id"],
        "tail_amount": ch["amount"],
        "share_percent": ch["shares"][holder],
        "amount": amount,
    }


def _initial_evidence(chains: list[dict]) -> list[dict]:
    """初次单：每条链的逐笔分摊全部作为 added（0 额不入证据）。"""
    rows = []
    for ch in chains:
        for holder, amt in ch["parts"].items():
            if amt:
                rows.append(_play_evidence(ch, holder, amt, ADDED))
    return rows


def _supplement_evidence(
    chains: list[dict],
    prior_traced: dict[tuple[str, str], int],
    latest_ctx: dict[tuple[str, str], dict],
    prior_payable: dict[str, int],
) -> list[dict]:
    """追补单：相对此前已入账结果，逐链给出撤销(removed)/新增(added)。

    无法用当前链解释的旧账差额（存量不可追溯单据）单列为 legacy，
    绝不以当前播放冒充历史。
    """
    rows: list[dict] = []
    current_map: dict[tuple[str, str], int] = {}
    current_ctx: dict[str, dict] = {}
    for ch in chains:
        current_ctx[ch["root_id"]] = ch
        for holder, amt in ch["parts"].items():
            if amt:
                current_map[(ch["root_id"], holder)] = amt

    keys = set(current_map) | set(prior_traced)
    # 同链先撤销后新增，保证跨期更正/协议后补的撤销与新增均可辨
    ordered_keys = sorted(keys, key=lambda k: (k[0], k[1]))

    for root, holder in ordered_keys:
        old = prior_traced.get((root, holder), 0)
        new = current_map.get((root, holder), 0)
        if old == new:
            continue
        # 撤销旧账：负额逐笔保留，不做轧差
        if old > 0:
            ctx = latest_ctx.get((root, holder))
            rows.append(
                {
                    "holder_code": holder,
                    "change_type": REMOVED,
                    "root_import_id": root,
                    "tail_import_id": ctx["tail_import_id"] if ctx else None,
                    "work_code": ctx["work_code"] if ctx else None,
                    "occurred_at": ctx["occurred_at"] if ctx else None,
                    "agreement_id": ctx["agreement_id"] if ctx else None,
                    "tail_amount": ctx["tail_amount"] if ctx else None,
                    "share_percent": ctx["share_percent"] if ctx else None,
                    "amount": -old,
                }
            )
        # 新增当前账（逐笔分摊可能为 0 则不入账）
        if new > 0:
            rows.append(_play_evidence(current_ctx[root], holder, new, ADDED))

    # 旧账差额（撤销）= 可逐笔追溯累计 − 已入账累计。
    # 存量不可追溯单据的金额没有任何当前播放可解释，只能单列，不能挂到当前链上；
    # 符号随账（旧账为正即撤销为负），随后可与新增相抵。
    traced_total: dict[str, int] = {}
    for (_root, holder), amt in prior_traced.items():
        traced_total[holder] = traced_total.get(holder, 0) + amt
    legacy_holders = set(prior_payable) | set(traced_total)
    legacy_rows = []
    for holder in sorted(legacy_holders):
        legacy_amt = traced_total.get(holder, 0) - prior_payable.get(holder, 0)
        if legacy_amt:
            legacy_rows.append(
                {
                    "holder_code": holder,
                    "change_type": LEGACY,
                    "root_import_id": None,
                    "tail_import_id": None,
                    "work_code": None,
                    "occurred_at": None,
                    "agreement_id": None,
                    "tail_amount": None,
                    "share_percent": None,
                    "amount": legacy_amt,
                }
            )

    rows.sort(
        key=lambda r: (
            r["root_import_id"] if r["root_import_id"] is not None else "￿",
            r["holder_code"],
            {REMOVED: 0, ADDED: 1, LEGACY: 2}[r["change_type"]],
        )
    )
    rows.extend(legacy_rows)
    return rows


def _insert_evidence(conn, batch_id: int, rows: list[dict]):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO statement_attributions
                (batch_id, holder_code, root_import_id, change_type,
                 tail_import_id, work_code, occurred_at, agreement_id,
                 tail_amount, share_percent, amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    batch_id, r["holder_code"], r["root_import_id"], r["change_type"],
                    r["tail_import_id"], r["work_code"], r["occurred_at"],
                    r["agreement_id"], r["tail_amount"], r["share_percent"], r["amount"],
                )
                for r in rows
            ],
        )


def _build_response(batch, start, end, payable, prior, pending, note=None):
    if batch["kind"] == "initial":
        lines = [
            {"holder_code": h, "amount": payable.get(h, 0), "delta": None}
            for h in sorted(payable)
            if payable.get(h, 0) != 0
        ]
    else:
        # 追补单列出本期全部权利人当前应付；差额为 0 也保留行，delta 置空
        holders = set(payable) | set(prior)
        lines = []
        for h in sorted(holders):
            delta = payable.get(h, 0) - prior.get(h, 0)
            lines.append(
                {
                    "holder_code": h,
                    "amount": payable.get(h, 0),
                    "delta": delta if delta != 0 else None,
                }
            )
    return {
        "batch_id": batch["id"],
        "kind": batch["kind"],
        "supersedes_batch_id": batch["supersedes_batch_id"],
        "period_start": start,
        "period_end": end,
        "issued_at": batch["created_at"],
        "note": note,
        "traceable": batch.get("evidence_status", "traceable") == "traceable",
        "payable": lines,
        "pending": pending,
    }


def issue_statement(conn, start_in: datetime, end_in: datetime):
    start, end, key = canonical_period(start_in, end_in)
    with conn.transaction():
        # 同一期间串行化出具，杜绝并发重复单据与重复证据
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(ADVISORY_NS, _period_lock_key(key)),),
        )

        payable, pending, chains = compute_current(conn, start, end)
        _insert_pending_snapshot(conn, start, end, pending)

        existing_initial = conn.execute(
            "SELECT id FROM statement_batches WHERE period_start = %s AND period_end = %s AND kind = 'initial'",
            (start, end),
        ).fetchone()

        if existing_initial is None:
            lines = {h: amt for h, amt in payable.items() if amt != 0}
            # 写明细前锁定全部受影响权利人：与付款登记互斥，
            # 保证"付款校验"与"出单入账"不会并发交错而超付。
            lock_holders_for_billing(conn, lines.keys())
            batch_id, created_at = _insert_batch(
                conn, start, end, "initial", None, lines,
            )
            # 冻结逐笔归因证据（按更正链原始编号归集）
            _insert_evidence(conn, batch_id, _initial_evidence(chains))
            batch = {
                "id": batch_id,
                "kind": "initial",
                "supersedes_batch_id": None,
                "created_at": created_at,
                "evidence_status": "traceable",
            }
            return _build_response(batch, start, end, payable, {}, pending)

        # 已有单据：不可覆盖，按新应付额与此前累计额之差出具追补单（差额可正可负）
        prior = _load_prior_payable(conn, start, end)
        holders = set(payable) | set(prior)
        deltas = {
            h: payable.get(h, 0) - prior.get(h, 0)
            for h in holders
            if payable.get(h, 0) - prior.get(h, 0) != 0
        }

        latest = _latest_batch(conn, start, end)
        if not deltas:
            # 应付无变化（可能只是待处理清单变化）：不出新单、也不生成任何证据
            return _build_response(
                latest, start, end, payable, prior, pending,
                note="no payable change since previous statement; no new batch created",
            )

        # 追补差额可正可负：写明细/证据前锁定受影响权利人，与付款登记互斥。
        # 负额追补不撤销历史付款，只改变已出具单据累计（余额随之变负）。
        lock_holders_for_billing(conn, deltas.keys())

        prior_traced = _load_prior_traced(conn, start, end)
        latest_ctx = _load_latest_evidence_ctx(conn, start, end)
        evidence_rows = _supplement_evidence(
            chains, prior_traced, latest_ctx, prior
        )

        batch_id, created_at = _insert_batch(
            conn, start, end, "supplement", latest["id"], deltas
        )
        _insert_evidence(conn, batch_id, evidence_rows)
        batch = {
            "id": batch_id,
            "kind": "supplement",
            "supersedes_batch_id": latest["id"],
            "created_at": created_at,
            "evidence_status": "traceable",
        }
        return _build_response(batch, start, end, payable, prior, pending)


def preview_period(conn, start_in: datetime, end_in: datetime):
    """不出具，仅按当前数据试算。"""
    start, end, _ = canonical_period(start_in, end_in)
    payable, pending, _chains = compute_current(conn, start, end)
    prior = _load_prior_payable(conn, start, end)
    latest = _latest_batch(conn, start, end)
    return {
        "period_start": start,
        "period_end": end,
        "payable": [
            {
                "holder_code": h,
                "amount": payable.get(h, 0),
                "delta": (payable.get(h, 0) - prior.get(h, 0))
                if latest is not None else None,
            }
            for h in sorted(set(payable) | set(prior))
        ],
        "pending": pending,
        "statement_exists": latest is not None,
    }


def list_batches(conn, start_in: datetime, end_in: datetime):
    start, end, _ = canonical_period(start_in, end_in)
    batches = conn.execute(
        """
        SELECT id, kind, supersedes_batch_id, created_at, evidence_status
        FROM statement_batches
        WHERE period_start = %s AND period_end = %s
        ORDER BY id
        """,
        (start, end),
    ).fetchall()
    result = []
    last_idx = len(batches) - 1
    for idx, b in enumerate(batches):
        lines = conn.execute(
            "SELECT holder_code, amount FROM statement_lines WHERE batch_id = %s ORDER BY holder_code",
            (b["id"],),
        ).fetchall()
        pending = _load_pending_snapshot(conn, start, end) if idx == last_idx else []
        result.append(
            {
                "batch_id": b["id"],
                "kind": b["kind"],
                "supersedes_batch_id": b["supersedes_batch_id"],
                "created_at": b["created_at"],
                "traceable": b["evidence_status"] == "traceable",
                "lines": [
                    {"holder_code": l["holder_code"], "amount": l["amount"], "delta": None}
                    for l in lines
                ],
                "pending": pending,
            }
        )
    return result


# ---- 归因记录查询 ----

_ATTRIBUTION_ORDER = (
    "ORDER BY a.holder_code, "
    # 旧账差额排在最后；真实链按原始编号、性质（撤销先于新增）、插入次序
    "CASE WHEN a.root_import_id IS NULL THEN 1 ELSE 0 END, "
    "a.root_import_id, "
    "CASE a.change_type WHEN 'removed' THEN 0 WHEN 'added' THEN 1 ELSE 2 END, "
    "a.id"
)


def _row_to_attribution(r) -> dict:
    return {
        "holder_code": r["holder_code"],
        "change_type": r["change_type"],
        "root_import_id": r["root_import_id"],
        "tail_import_id": r["tail_import_id"],
        "work_code": r["work_code"],
        "occurred_at": r["occurred_at"],
        "agreement_id": r["agreement_id"],
        "tail_amount": r["tail_amount"],
        "share_percent": str(r["share_percent"]) if r["share_percent"] is not None else None,
        "amount": r["amount"],
    }


def get_attributions(
    conn, batch_id: int, holder_code: str | None, page: int, page_size: int
):
    """按单据（可再按权利人）分页返回归因记录与逐权利人合计。

    - 不存在的单据：404（由路由层据 None 抛出）；
    - 不可追溯的存量单据：traceable=false、records 为空，
      holder_totals 仅回显原明细行且 reconciles=false；
    - 汇总恒等于该单据原有明细行金额。
    """
    batch = conn.execute(
        "SELECT id, evidence_status FROM statement_batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None

    traceable = batch["evidence_status"] == "traceable"

    line_rows = conn.execute(
        "SELECT holder_code, amount FROM statement_lines WHERE batch_id = %s",
        (batch_id,),
    ).fetchall()
    line_amounts = {r["holder_code"]: r["amount"] for r in line_rows}

    totals_rows = conn.execute(
        """
        SELECT a.holder_code, SUM(a.amount) AS attributed_total
        FROM statement_attributions a
        WHERE a.batch_id = %s
          AND (%s::text IS NULL OR a.holder_code = %s)
        GROUP BY a.holder_code
        ORDER BY a.holder_code
        """,
        (batch_id, holder_code, holder_code),
    ).fetchall() if traceable else []

    holder_totals = []
    summed_holders = {r["holder_code"] for r in totals_rows}
    for r in totals_rows:
        line_amt = line_amounts.get(r["holder_code"], 0)
        holder_totals.append(
            {
                "holder_code": r["holder_code"],
                "attributed_total": int(r["attributed_total"]),
                "statement_line_amount": line_amt,
                "reconciles": int(r["attributed_total"]) == line_amt,
            }
        )
    # 明细行存在但没有任何证据（理论上只可能出现在不可追溯单据）
    if not traceable:
        for h, amt in sorted(line_amounts.items()):
            if holder_code is not None and h != holder_code:
                continue
            holder_totals.append(
                {
                    "holder_code": h,
                    "attributed_total": 0,
                    "statement_line_amount": amt,
                    "reconciles": False,
                }
            )
    else:
        for h, amt in sorted(line_amounts.items()):
            if h not in summed_holders and (holder_code is None or h == holder_code):
                holder_totals.append(
                    {
                        "holder_code": h,
                        "attributed_total": 0,
                        "statement_line_amount": amt,
                        "reconciles": amt == 0,
                    }
                )
    holder_totals.sort(key=lambda t: t["holder_code"])

    total_records = 0
    records = []
    if traceable:
        total_row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM statement_attributions a
            WHERE a.batch_id = %s
              AND (%s::text IS NULL OR a.holder_code = %s)
            """,
            (batch_id, holder_code, holder_code),
        ).fetchone()
        total_records = total_row["n"]

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""
            SELECT a.holder_code, a.change_type, a.root_import_id,
                   a.tail_import_id, a.work_code, a.occurred_at, a.agreement_id,
                   a.tail_amount, a.share_percent, a.amount
            FROM statement_attributions a
            WHERE a.batch_id = %s
              AND (%s::text IS NULL OR a.holder_code = %s)
            {_ATTRIBUTION_ORDER}
            LIMIT %s OFFSET %s
            """,
            (batch_id, holder_code, holder_code, page_size, offset),
        ).fetchall()
        records = [_row_to_attribution(r) for r in rows]

    return {
        "batch_id": batch_id,
        "traceable": traceable,
        "holder_code": holder_code,
        "page": page,
        "page_size": page_size,
        "total_records": total_records,
        "holder_totals": holder_totals,
        "records": records,
    }
