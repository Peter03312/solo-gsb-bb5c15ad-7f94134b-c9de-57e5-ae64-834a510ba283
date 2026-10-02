"""权利人余额核对底稿冻结（财务一次性快照）。

财务凭调用方唯一快照号 ``snapshot_no`` 与权利人编号集合 ``holder_codes``
（空集合表示全体已建档权利人）冻结一次核对底稿：每位权利人固化
"已出具单据净额、付款总额、退回总额、待确认预留额、余额及可用额"。

口径（金额均为整数分，与权利人对账单完全一致）：
  余额 balance   = 已出具单据净额 billed_total - 付款总额 paid_total
                   + 退回总额 refunded_total（可为负）
  可用额 available_balance = 余额 - 待确认预留 reserved_total（可为负）

语义约定：
- 编号重复或任一权利人不存在：整次拒绝（422/404），不留任何快照；
- 同号同集合重试：返回原快照（status=duplicate，HTTP 200）；
- 同号异集合（全体/指定模式不同，或集合内容不同）：409 snapshot_ref_conflict；
- 冻结明细一经写入不可修改/删除；后续出单、付款、退票、预留确认/取消
  与新建权利人都不改历史（明细独立固化，不依赖任何业务表现状）；
- 空库（全体模式下尚无任何权利人）快照合法：零行明细、各项总计为 0。

并发协议：
- 创建在"快照号事务咨询锁"下序列化同号请求（恰一笔创建，其余按同集合
  幂等或异集合冲突处理，无竞态、无序列化失败）；
- 固化只读取 statement_lines / payments / payment_refunds /
  payment_instructions（均为只追加表）并写入快照表，不锁业务行、
  不阻塞出单/付款/退票/预留；各项金额由**单条聚合 SQL** 一次取出 ——
  单语句在 READ COMMITTED 下同样使用一个一致性语句快照，与这些写操作
  并发时快照内各项全部来自同一已提交状态，绝不混合前后状态；
- 重试读原快照（只追加表上结果必然一致）。
"""
import hashlib

from .errors import APIError

# 快照号事务咨询锁命名空间（高 32 位）
_SNAPSHOT_NS = 0x42414C31  # "BAL1"

_HEADER_COLS = "id, snapshot_no, scope_all, holder_codes, holder_count, created_at"


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _snapshot_lock_key(snapshot_no: str) -> int:
    digest = hashlib.blake2b(snapshot_no.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


# 同一已提交数据库视图下的逐权利人聚合：单据净额 / 付款 / 退回 / 待确认预留
# 全部由相关子查询取出，单条语句保证来自同一语句快照；
# holder 列表由参数给出（指定模式为提交集合，全体模式另用 _AGGREGATE_ALL_SQL）。
# 内连接已建档权利人行；存在性由调用方在同一事务快照内先行校验。
_AGGREGATE_SQL = """
SELECT h.code AS holder_code,
       (SELECT COALESCE(SUM(l.amount), 0)
        FROM statement_lines l WHERE l.holder_code = h.code) AS billed_total,
       (SELECT COALESCE(SUM(p.amount), 0)
        FROM payments p WHERE p.holder_code = h.code) AS paid_total,
       (SELECT COALESCE(SUM(r.amount), 0)
        FROM payment_refunds r
        JOIN payments p2 ON p2.id = r.payment_id
        WHERE p2.holder_code = h.code) AS refunded_total,
       (SELECT COALESCE(SUM(i.amount), 0)
        FROM payment_instructions i
        WHERE i.holder_code = h.code AND i.status = 'pending') AS reserved_total
FROM unnest(%s::text[]) AS u(code)
JOIN rightsholders h ON h.code = u.code
ORDER BY h.code
"""

_AGGREGATE_ALL_SQL = """
SELECT h.code AS holder_code,
       COALESCE((SELECT SUM(l.amount)
                 FROM statement_lines l WHERE l.holder_code = h.code), 0) AS billed_total,
       COALESCE((SELECT SUM(p.amount)
                 FROM payments p WHERE p.holder_code = h.code), 0) AS paid_total,
       COALESCE((SELECT SUM(r.amount)
                 FROM payment_refunds r
                 JOIN payments p2 ON p2.id = r.payment_id
                 WHERE p2.holder_code = h.code), 0) AS refunded_total,
       COALESCE((SELECT SUM(i.amount)
                 FROM payment_instructions i
                 WHERE i.holder_code = h.code AND i.status = 'pending'), 0)
           AS reserved_total
FROM rightsholders h
ORDER BY h.code
"""


def _aggregate(conn, holder_codes: list[str]) -> list[dict]:
    """聚合指定权利人的各项金额（单条 SQL 的语句快照保证一致性）。

    holder_codes 须已去重、升序；全部编号假定已存在（存在性另行校验）。
    """
    rows = conn.execute(_AGGREGATE_SQL, (holder_codes,)).fetchall()
    lines = []
    for r in rows:
        billed = int(r["billed_total"])
        paid = int(r["paid_total"])
        refunded = int(r["refunded_total"])
        reserved = int(r["reserved_total"])
        balance = billed - paid + refunded
        lines.append(
            {
                "holder_code": r["holder_code"],
                "billed_total": billed,
                "paid_total": paid,
                "refunded_total": refunded,
                "reserved_total": reserved,
                "balance": balance,
                "available_balance": balance - reserved,
            }
        )
    return lines


def _aggregate_all(conn) -> list[dict]:
    """全体已建档权利人：单条聚合 SQL 取值（空库 => 空列表）。"""
    rows = conn.execute(_AGGREGATE_ALL_SQL).fetchall()
    lines = []
    for r in rows:
        billed = int(r["billed_total"])
        paid = int(r["paid_total"])
        refunded = int(r["refunded_total"])
        reserved = int(r["reserved_total"])
        balance = billed - paid + refunded
        lines.append(
            {
                "holder_code": r["holder_code"],
                "billed_total": billed,
                "paid_total": paid,
                "refunded_total": refunded,
                "reserved_total": reserved,
                "balance": balance,
                "available_balance": balance - reserved,
            }
        )
    return lines


def _totals(lines: list[dict]) -> dict:
    return {
        "billed_total": sum(l["billed_total"] for l in lines),
        "paid_total": sum(l["paid_total"] for l in lines),
        "refunded_total": sum(l["refunded_total"] for l in lines),
        "reserved_total": sum(l["reserved_total"] for l in lines),
        "balance": sum(l["balance"] for l in lines),
        "available_balance": sum(l["available_balance"] for l in lines),
    }


def _snapshot_payload(conn, row: dict, *, status: str, lines: list[dict]) -> dict:
    totals = _totals(lines)
    return {
        "snapshot_no": row["snapshot_no"],
        "status": status,
        "scope_all": bool(row["scope_all"]),
        # 指定模式为提交集合；全体模式（库存为归一化空数组）展开为冻结时点
        # 实际固化的权利人清单（此后新建权利人不进入历史）。
        "holder_codes": (
            [l["holder_code"] for l in lines]
            if row["scope_all"] else list(row["holder_codes"])
        ),
        "holder_count": int(row["holder_count"]),
        "created_at": row["created_at"],
        "lines": lines,
        "totals": totals,
    }


def _load_lines(conn, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT holder_code, billed_total, paid_total, refunded_total,
               reserved_total, balance, available_balance
        FROM balance_snapshot_lines
        WHERE snapshot_id = %s
        ORDER BY holder_code
        """,
        (snapshot_id,),
    ).fetchall()
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


def create_snapshot(conn, snapshot_no: str, holder_codes: list[str]) -> dict:
    """冻结一次权利人余额核对底稿。

    - holder_codes 为空列表表示全体已建档权利人（空库合法：零行、零总计）；
    - 编号重复 422 duplicate_holder_code；任一权利人不存在 404 holder_not_found；
    - 同号同集合重试幂等（status=duplicate，HTTP 200）；
      同号异集合 409 snapshot_ref_conflict；
      上述拒绝均不留任何快照。
    """
    scope_all = len(holder_codes) == 0
    # 去重 + 升序规范化：集合内容与提交次序、重复无关
    codes = sorted(set(holder_codes))
    if not scope_all and len(codes) != len(holder_codes):
        raise APIError(
            422,
            "duplicate_holder_code",
            "duplicate holder_code in snapshot request; whole request rejected",
        )

    # 各项金额由单条聚合 SQL 一次取出（语句级一致性快照）：出单/付款/退票/
    # 预留与本事务并发时，快照内全部字段来自同一已提交状态，不混合前后状态。
    with conn.transaction():
        # 快照号咨询锁：同号并发请求串行化，恰一笔创建，其余幂等/冲突。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_SNAPSHOT_NS, _snapshot_lock_key(snapshot_no)),),
        )

        existing = conn.execute(
            f"SELECT {_HEADER_COLS} FROM balance_snapshots "
            "WHERE snapshot_no = %s",
            (snapshot_no,),
        ).fetchone()
        if existing is not None:
            if (
                bool(existing["scope_all"]) != scope_all
                or list(existing["holder_codes"]) != codes
            ):
                raise APIError(
                    409,
                    "snapshot_ref_conflict",
                    f"snapshot {snapshot_no} already frozen with a different "
                    "holder set; request rejected",
                    detail={
                        "snapshot_no": snapshot_no,
                        "existing_scope_all": bool(existing["scope_all"]),
                        "existing_holder_codes": list(existing["holder_codes"]),
                        "request_scope_all": scope_all,
                        "request_holder_codes": codes,
                    },
                )
            # 同号同集合重试：返回原快照（明细已冻结，直接读取）
            return _snapshot_payload(
                conn,
                existing,
                status="duplicate",
                lines=_load_lines(conn, existing["id"]),
            )

        if scope_all:
            lines = _aggregate_all(conn)
        else:
            # 存在性校验：任一权利人不存在即整次拒绝
            found_rows = conn.execute(
                "SELECT code FROM rightsholders WHERE code = ANY(%s)",
                (codes,),
            ).fetchall()
            found = {r["code"] for r in found_rows}
            missing = [c for c in codes if c not in found]
            if missing:
                raise APIError(
                    404,
                    "holder_not_found",
                    f"rightsholder {missing[0]} not found; snapshot rejected",
                    detail={"holder_code": missing[0], "missing": missing},
                )
            lines = _aggregate(conn, codes)

        header = conn.execute(
            """
            INSERT INTO balance_snapshots
                (snapshot_no, scope_all, holder_codes, holder_count)
            VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (snapshot_no, scope_all, codes, len(lines)),
        ).fetchone()
        if lines:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO balance_snapshot_lines
                        (snapshot_id, holder_code, billed_total, paid_total,
                         refunded_total, reserved_total, balance,
                         available_balance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            header["id"], l["holder_code"], l["billed_total"],
                            l["paid_total"], l["refunded_total"],
                            l["reserved_total"], l["balance"],
                            l["available_balance"],
                        )
                        for l in lines
                    ],
                )

        row = {
            "id": header["id"],
            "snapshot_no": snapshot_no,
            "scope_all": scope_all,
            "holder_codes": codes,
            "holder_count": len(lines),
            "created_at": header["created_at"],
        }
        return _snapshot_payload(conn, row, status="created", lines=lines)


# ---- 分页读取冻结明细 ----

def _get_header(conn, snapshot_no: str):
    return conn.execute(
        f"SELECT {_HEADER_COLS} FROM balance_snapshots WHERE snapshot_no = %s",
        (snapshot_no,),
    ).fetchone()


def get_snapshot_page(
    conn, snapshot_no: str, page: int, page_size: int
) -> dict | None:
    """按快照号分页读取冻结明细及各项总计（总计为全快照口径，不随分页变化）。

    明细按权利人编号升序、行内固化值返回；页超出范围时 records 为空
    （总计、总数仍返回）。未知快照号返回 None（路由层 404）。
    """
    header = _get_header(conn, snapshot_no)
    if header is None:
        return None

    total_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM balance_snapshot_lines WHERE snapshot_id = %s",
        (header["id"],),
    ).fetchone()["n"]

    # 全体模式下展开冻结时点的完整权利人清单（单列，开销小）
    all_codes = [
        r["holder_code"]
        for r in conn.execute(
            "SELECT holder_code FROM balance_snapshot_lines "
            "WHERE snapshot_id = %s ORDER BY holder_code",
            (header["id"],),
        ).fetchall()
    ]

    offset = (page - 1) * page_size
    rows = conn.execute(
        """
        SELECT holder_code, billed_total, paid_total, refunded_total,
               reserved_total, balance, available_balance
        FROM balance_snapshot_lines
        WHERE snapshot_id = %s
        ORDER BY holder_code
        LIMIT %s OFFSET %s
        """,
        (header["id"], page_size, offset),
    ).fetchall()
    lines = [
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

    totals_row = conn.execute(
        """
        SELECT COALESCE(SUM(billed_total), 0) AS billed_total,
               COALESCE(SUM(paid_total), 0) AS paid_total,
               COALESCE(SUM(refunded_total), 0) AS refunded_total,
               COALESCE(SUM(reserved_total), 0) AS reserved_total,
               COALESCE(SUM(balance), 0) AS balance,
               COALESCE(SUM(available_balance), 0) AS available_balance
        FROM balance_snapshot_lines
        WHERE snapshot_id = %s
        """,
        (header["id"],),
    ).fetchone()

    return {
        "snapshot_no": header["snapshot_no"],
        "scope_all": bool(header["scope_all"]),
        # 全体模式（库存 holder_codes 为空）展开为冻结时点实际权利人清单
        "holder_codes": all_codes if header["scope_all"] else list(header["holder_codes"]),
        "holder_count": int(header["holder_count"]),
        "created_at": header["created_at"],
        "page": page,
        "page_size": page_size,
        "total_records": int(total_rows),
        "records": lines,
        "totals": {
            "billed_total": int(totals_row["billed_total"]),
            "paid_total": int(totals_row["paid_total"]),
            "refunded_total": int(totals_row["refunded_total"]),
            "reserved_total": int(totals_row["reserved_total"]),
            "balance": int(totals_row["balance"]),
            "available_balance": int(totals_row["available_balance"]),
        },
    }
