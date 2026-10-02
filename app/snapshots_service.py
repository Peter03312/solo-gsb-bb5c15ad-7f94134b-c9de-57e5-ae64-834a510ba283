"""权利人余额核对快照（财务冻结底稿）。

财务凭唯一快照号 snapshot_no 与权利人编号集合（空集合 = 全体）冻结一次
余额核对底稿：基于同一已提交数据库视图，固化每位权利人的

- 已出具单据净额 billed_total（全部期间初次单与正负追补单的代数和）、
- 付款总额 paid_total、退回总额 refunded_total、
- 待确认预留额 reserved_total（pending 预留指令合计）、
- 余额 balance = billed_total - paid_total + refunded_total、
- 可用额 available_balance = balance - reserved_total，

金额均为整数分（最小货币单位），口径与权利人对账单完全一致。

规则：
- 请求集合内编号重复（422 duplicate_holder_code）或权利人不存在
  （404 holder_not_found）：整次拒绝，不留任何快照；
- 同号同集合重试：幂等返回原快照（status=duplicate，HTTP 200）；
  同号异集合（含"全体"与"显式列全"之别）：409 snapshot_conflict；
- 冻结读取用单条 INSERT ... SELECT：出单、付款、退票、预留确认/取消与
  权利人建档并发时，整张快照的全部数字来自同一已提交数据库视图
  （语句级快照），不混合前后状态；
- 同号并发创建经"快照号事务咨询锁"串行化：恰一份快照，其余幂等返回；
- 快照行一经写入不可修改/删除（触发器强制）：后续流水与权利人建档
  不改历史；空库快照（无任何权利人）返回零总计；
- 按快照号分页读取冻结明细与各项总计；未知快照号 404 snapshot_not_found。
"""
import hashlib
import json

from .errors import APIError
from .payments_service import take_ref_advisory_lock

# 快照号事务咨询锁命名空间
_SNAPSHOT_NS = 0x534E4150  # "SNAP"

_LINE_COLS = (
    "holder_code, billed_total, paid_total, refunded_total, "
    "reserved_total, balance, available_balance"
)

# 单条 INSERT ... SELECT：同一语句内全部权利人的四项累计来自同一已提交
# 数据库视图（READ COMMITTED 下语句级快照），并发流水只可能整体落在
# 快照之前或之后，绝不混合前后状态。
_INSERT_LINES_SQL = """
INSERT INTO balance_snapshot_lines
    (snapshot_id, holder_code, billed_total, paid_total, refunded_total,
     reserved_total, balance, available_balance)
SELECT
    %s,
    h.code,
    COALESCE(b.billed, 0),
    COALESCE(p.paid, 0),
    COALESCE(rf.refunded, 0),
    COALESCE(rs.reserved, 0),
    COALESCE(b.billed, 0) - COALESCE(p.paid, 0) + COALESCE(rf.refunded, 0),
    COALESCE(b.billed, 0) - COALESCE(p.paid, 0) + COALESCE(rf.refunded, 0)
        - COALESCE(rs.reserved, 0)
FROM rightsholders h
LEFT JOIN (
    SELECT holder_code, SUM(amount) AS billed
    FROM statement_lines
    GROUP BY holder_code
) b ON b.holder_code = h.code
LEFT JOIN (
    SELECT holder_code, SUM(amount) AS paid
    FROM payments
    GROUP BY holder_code
) p ON p.holder_code = h.code
LEFT JOIN (
    SELECT p2.holder_code, SUM(r.amount) AS refunded
    FROM payment_refunds r
    JOIN payments p2 ON p2.id = r.payment_id
    GROUP BY p2.holder_code
) rf ON rf.holder_code = h.code
LEFT JOIN (
    SELECT holder_code, SUM(amount) AS reserved
    FROM payment_instructions
    WHERE status = 'pending'
    GROUP BY holder_code
) rs ON rs.holder_code = h.code
WHERE (%s OR h.code = ANY(%s))
ORDER BY h.code
"""

# 各项总计（跨全部冻结行，与分页无关）；空快照经 COALESCE 得零总计。
_TOTALS_SQL = """
SELECT COUNT(*) AS holder_count,
       COALESCE(SUM(billed_total), 0) AS billed_total,
       COALESCE(SUM(paid_total), 0) AS paid_total,
       COALESCE(SUM(refunded_total), 0) AS refunded_total,
       COALESCE(SUM(reserved_total), 0) AS reserved_total,
       COALESCE(SUM(balance), 0) AS balance,
       COALESCE(SUM(available_balance), 0) AS available_balance
FROM balance_snapshot_lines
WHERE snapshot_id = %s
"""


def _content_hash(scope_all: bool, codes: list[str]) -> str:
    """请求集合的规范化内容哈希：与编号次序无关，同一集合即同内容。"""
    canonical = {"all": scope_all, "codes": sorted(codes)}
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _line_rows(conn, snapshot_id: int, limit: int | None = None,
               offset: int = 0) -> list[dict]:
    sql = (
        f"SELECT {_LINE_COLS} FROM balance_snapshot_lines "
        "WHERE snapshot_id = %s ORDER BY holder_code"
    )
    params: list = [snapshot_id]
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "holder_code": r["holder_code"],
            "billed_total": int(r["billed_total"]),
            "paid_total": int(r["paid_total"]),
            "refunded_total": int(r["refunded_total"]),
            "reserved_total": int(r["reserved_total"]),
            "balance": int(r["balance"]),
            "available_balance": int(r["available_balance"]),
        }
        for r in rows
    ]


def _totals(conn, snapshot_id: int) -> dict:
    row = conn.execute(_TOTALS_SQL, (snapshot_id,)).fetchone()
    return {k: int(v) for k, v in row.items()}


def _snapshot_payload(conn, snapshot_id: int, status: str) -> dict:
    """整份快照（创建/幂等重试的响应）：元信息 + 各项总计 + 全部冻结行。"""
    snap = conn.execute(
        """
        SELECT snapshot_no, scope_all, holder_codes, created_at
        FROM balance_snapshots
        WHERE id = %s
        """,
        (snapshot_id,),
    ).fetchone()
    return {
        "snapshot_no": snap["snapshot_no"],
        "status": status,
        "scope_all": snap["scope_all"],
        "holder_codes": list(snap["holder_codes"]),
        "created_at": snap["created_at"],
        "totals": _totals(conn, snapshot_id),
        "lines": _line_rows(conn, snapshot_id),
    }


def create_snapshot(conn, snapshot_no: str, holder_codes: list[str]) -> dict:
    """冻结一次权利人余额核对底稿。

    - 请求集合内编号重复：422 duplicate_holder_code，整次拒绝不留快照；
    - 同号同集合重试：返回原快照（status=duplicate，HTTP 200）；
    - 同号异集合：409 snapshot_conflict（"空集合=全体"与"显式列全"
      属不同集合，同样拒绝）；
    - 权利人不存在：404 holder_not_found，整次拒绝不留快照；
    - 通过后基于同一已提交视图冻结全部明细（单条 INSERT ... SELECT）。
    """
    # 集合语义：请求内编号重复 => 整次拒绝（422），不留任何快照
    if len(set(holder_codes)) != len(holder_codes):
        raise APIError(
            422,
            "duplicate_holder_code",
            "duplicate holder_code in request; whole snapshot rejected",
            detail={"snapshot_no": snapshot_no},
        )
    scope_all = len(holder_codes) == 0
    canonical_codes = sorted(holder_codes)
    content_hash = _content_hash(scope_all, canonical_codes)

    with conn.transaction():
        # 快照号咨询锁：同号并发创建在此串行，恰一份快照；
        # 幂等/冲突判定确定，不依赖唯一约束竞态。
        take_ref_advisory_lock(conn, _SNAPSHOT_NS, snapshot_no)

        existing = conn.execute(
            "SELECT id, content_hash FROM balance_snapshots WHERE snapshot_no = %s",
            (snapshot_no,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise APIError(
                    409,
                    "snapshot_conflict",
                    f"balance snapshot {snapshot_no} already created with a "
                    "different holder set",
                    detail={"snapshot_no": snapshot_no},
                )
            # 同号同集合重试：幂等返回原快照（冻结内容不随后续流水变化）
            return _snapshot_payload(conn, existing["id"], status="duplicate")

        if not scope_all:
            # 权利人不存在 => 整次拒绝（404），不留任何快照
            rows = conn.execute(
                "SELECT code FROM rightsholders WHERE code = ANY(%s)",
                (canonical_codes,),
            ).fetchall()
            found = {r["code"] for r in rows}
            missing = [code for code in canonical_codes if code not in found]
            if missing:
                raise APIError(
                    404,
                    "holder_not_found",
                    f"rightsholder(s) not found: {', '.join(missing)}; "
                    "whole snapshot rejected",
                    detail={"missing_holder_codes": missing},
                )

        snap = conn.execute(
            """
            INSERT INTO balance_snapshots
                (snapshot_no, scope_all, holder_codes, content_hash)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (snapshot_no, scope_all, canonical_codes, content_hash),
        ).fetchone()

        # 单语句快照：全部权利人的四项累计来自同一已提交视图
        conn.execute(
            _INSERT_LINES_SQL,
            (snap["id"], scope_all, canonical_codes),
        )
        return _snapshot_payload(conn, snap["id"], status="created")


def get_snapshot_page(conn, snapshot_no: str, page: int, page_size: int):
    """按快照号分页读取冻结明细与各项总计；快照不存在返回 None。"""
    snap = conn.execute(
        """
        SELECT id, scope_all, holder_codes, created_at
        FROM balance_snapshots
        WHERE snapshot_no = %s
        """,
        (snapshot_no,),
    ).fetchone()
    if snap is None:
        return None
    totals = _totals(conn, snap["id"])
    lines = _line_rows(
        conn, snap["id"], limit=page_size, offset=(page - 1) * page_size
    )
    return {
        "snapshot_no": snapshot_no,
        "scope_all": snap["scope_all"],
        "holder_codes": list(snap["holder_codes"]),
        "created_at": snap["created_at"],
        "page": page,
        "page_size": page_size,
        "total_lines": totals["holder_count"],
        # 各项总计跨全部冻结行（与分页无关）；空快照为零总计
        "totals": totals,
        "lines": lines,
    }
