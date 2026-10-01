"""付款登记、付款额度预留、银行退回与权利人对账。

余额口径：全部期间已出具单据明细行（初次单与正负追补单）金额的代数和，
减去全部已登记付款，再加上全部已登记银行退回。试算（preview）不出具、
待处理播放不计入应付，因此二者天然不进入余额。

可用额口径：当前余额 − 该权利人全部待确认（pending）预留指令金额合计。
预留创建、直接付款与预留确认都不得占用待确认预留。

并发协议（与出单互斥，杜绝超付/超退）：
- 登记付款先取"外部流水号事务咨询锁"（序列化同一流水号的并发重试），
  再对该权利人行取行锁；余额/可用额校验与付款插入在同一事务内原子完成。
- 预留指令先取"指令号事务咨询锁"（同号并发重试串行化），再锁权利人行；
  确认预留先取"外部付款流水号咨询锁"（同流水号确认串行化、占用判定确定），
  再锁指令行与权利人行；取消只取"指令号咨询锁"后锁指令行。
- 登记银行退回先取"退回流水号事务咨询锁"，再锁定原付款行
  （同一原付款的多笔并发退回在此串行，累计退回绝不合计超额），
  随后锁定权利人行（与付款/预留/出单的余额校验互斥）。
- 出单（statements_service）在写任何明细行前，按权利人编号排序锁定
  本次受影响的全部权利人行。加锁次序统一为"业务流水号咨询锁 →
  付款行（仅退回）→ 指令行（仅预留确认/取消）→ 按编号升序的权利人行"，
  无死锁。
"""
import hashlib

# 付款流水号咨询锁命名空间
_PAYMENT_REF_NS = 0x50415931  # "PAY1"


def combined_lock_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def text_lock_key(value: str) -> int:
    digest = hashlib.blake2b(value.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


def take_ref_advisory_lock(conn, namespace: int, value: str) -> None:
    """取"业务流水号"事务级咨询锁：同号并发请求在该锁上串行化。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (combined_lock_key(namespace, text_lock_key(value)),),
    )


# 旧名称保留为模块内别名（register_payment 等内部使用）
_combined_key = combined_lock_key
_ref_lock_key = text_lock_key


# 全部期间已出具单据的正负金额累计；追补差额为有符号值，直接代数相加。
# 待处理播放不入 statement_lines，故不影响余额；preview 根本不出单。
_BILLED_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS billed
FROM statement_lines
WHERE holder_code = %s
"""

_PAID_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS paid
FROM payments
WHERE holder_code = %s
"""

# 银行退回累计：payment_refunds 经 payment_id 关联付款，退回加回余额。
_REFUNDED_TOTAL_SQL = """
SELECT COALESCE(SUM(r.amount), 0) AS refunded
FROM payment_refunds r
JOIN payments p ON p.id = r.payment_id
WHERE p.holder_code = %s
"""

# 待确认预留累计：payment_instructions 中 status='pending' 的预留金额合计。
_RESERVED_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS reserved
FROM payment_instructions
WHERE holder_code = %s AND status = 'pending'
"""

# 比指定指令"更早创建"的待确认指令金额合计（创建次序以 id 升序为准）。
# 预留确认的 FIFO 规则：负额追补可能冲减余额，确认本指令时仍须留足
# 更早创建的待确认指令与本指令金额，否则拒绝且指令不变。
_EARLIER_PENDING_SQL = """
SELECT COALESCE(SUM(amount), 0) AS earlier_reserved
FROM payment_instructions
WHERE holder_code = %s AND status = 'pending' AND id < %s
"""


def _billed_total(conn, holder_code: str) -> int:
    return int(conn.execute(_BILLED_TOTAL_SQL, (holder_code,)).fetchone()["billed"])


def _paid_total(conn, holder_code: str) -> int:
    return int(conn.execute(_PAID_TOTAL_SQL, (holder_code,)).fetchone()["paid"])


def _refunded_total(conn, holder_code: str) -> int:
    return int(
        conn.execute(_REFUNDED_TOTAL_SQL, (holder_code,)).fetchone()["refunded"]
    )


def get_reserved_total(conn, holder_code: str) -> int:
    """全部待确认（pending）预留指令金额合计。"""
    return int(
        conn.execute(_RESERVED_TOTAL_SQL, (holder_code,)).fetchone()["reserved"]
    )


def get_balance(conn, holder_code: str) -> int:
    """当前余额 = 已出具单据累计 - 已登记付款 + 已退回累计（可为负）。"""
    return (
        _billed_total(conn, holder_code)
        - _paid_total(conn, holder_code)
        + _refunded_total(conn, holder_code)
    )


def get_available(conn, holder_code: str) -> int:
    """可用额 = 当前余额 - 全部待确认预留（可为负）。"""
    return get_balance(conn, holder_code) - get_reserved_total(conn, holder_code)


def lock_holders_for_billing(conn, holder_codes) -> None:
    """出单侧调用：按编号升序逐个锁定本次受影响的权利人，再写单据明细。

    与付款登记持有的权利人行锁互斥；逐个、升序加锁，使多权利人事务间
    加锁次序全局一致，避免死锁（不依赖数据库执行计划的取行顺序）。
    """
    codes = sorted({c for c in holder_codes if c})
    for code in codes:
        conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (code,),
        )


def register_payment(conn, holder_code: str, external_ref: str, amount: int) -> dict:
    """登记一笔已完成付款（预留确认之外的直接登记，行为向后兼容）。

    - 权利人不存在：404 holder_not_found，不留付款；
    - 同一流水号同内容重试：返回原付款记录与登记后余额（幂等）；
    - 同一流水号异内容（权利人或金额不同）：409 payment_ref_conflict；
    - 当前正余额不足，或余额虽足但"扣除全部待确认预留后"可用额不足
      （余额 <= 0、amount > 余额、amount > 可用额任一成立）：
      409 payment_exceeds_balance，拒绝且不留付款。
      即新付款必须避开待确认预留；入参、响应与超额拒绝约定保持兼容。
    """
    with conn.transaction():
        # 先按流水号加锁：并发的同流水号请求在此串行化，
        # 避免唯一约束竞态下误报，也保证"同内容/异内容"判定确定。
        take_ref_advisory_lock(conn, _PAYMENT_REF_NS, external_ref)

        existing = conn.execute(
            """
            SELECT id, holder_code, amount, created_at, instruction_id
            FROM payments WHERE external_ref = %s
            """,
            (external_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _conflict(external_ref, existing)
            return {
                "payment_id": existing["id"],
                "holder_code": existing["holder_code"],
                "external_ref": external_ref,
                "amount": int(existing["amount"]),
                "registered_at": existing["created_at"],
                "status": "duplicate",
                "balance_after": get_balance(conn, existing["holder_code"]),
            }

        # 锁定权利人行：与出单侧行锁互斥；权利人不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        reserved = get_reserved_total(conn, holder_code)
        available = balance - reserved
        # 余额不为正，或余额不足，或余额须让位于待确认预留（新付款避开预留）
        if balance <= 0 or amount > balance or amount > available:
            raise _exceeds_balance(
                holder_code, amount, balance, reserved, available
            )

        row = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()
        return {
            "payment_id": row["id"],
            "holder_code": holder_code,
            "external_ref": external_ref,
            "amount": amount,
            "registered_at": row["created_at"],
            "status": "created",
            "balance_after": balance - amount,
        }


def _holder_not_found(holder_code: str):
    from .errors import APIError

    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _conflict(external_ref: str, existing) -> "object":
    from .errors import APIError

    return APIError(
        409,
        "payment_ref_conflict",
        f"external payment ref {external_ref} already registered with different content",
        detail={
            "external_ref": external_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _exceeds_balance(
    holder_code: str,
    amount: int,
    balance: int,
    reserved: int | None = None,
    available: int | None = None,
):
    from .errors import APIError

    detail = {
        "holder_code": holder_code,
        "requested_amount": amount,
        "balance": balance,
    }
    if reserved is not None:
        detail["reserved_total"] = reserved
        detail["available"] = available
    return APIError(
        409,
        "payment_exceeds_balance",
        f"payment amount {amount} exceeds current positive balance {balance} "
        f"of rightsholder {holder_code}; payment rejected",
        detail=detail,
    )


def get_holder_ledger(conn, holder_code: str):
    """按权利人查询：基本信息、单据增减、付款流水与当前余额。"""
    holder = conn.execute(
        "SELECT code, name FROM rightsholders WHERE code = %s",
        (holder_code,),
    ).fetchone()
    if holder is None:
        return None

    statement_rows = conn.execute(
        """
        SELECT b.id AS batch_id, b.kind, b.period_start, b.period_end,
               b.created_at AS issued_at, l.amount
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE l.holder_code = %s
        ORDER BY b.id
        """,
        (holder_code,),
    ).fetchall()

    payment_rows = conn.execute(
        """
        SELECT id, external_ref, amount, created_at
        FROM payments
        WHERE holder_code = %s
        ORDER BY id
        """,
        (holder_code,),
    ).fetchall()

    refund_rows = conn.execute(
        """
        SELECT r.id AS refund_id, r.refund_ref, r.amount, r.created_at,
               r.payment_id, p.external_ref AS payment_ref
        FROM payment_refunds r
        JOIN payments p ON p.id = r.payment_id
        WHERE p.holder_code = %s
        ORDER BY r.id
        """,
        (holder_code,),
    ).fetchall()

    instruction_rows = conn.execute(
        """
        SELECT i.id, i.instruction_ref, i.amount, i.status,
               i.external_ref, i.payment_id,
               i.created_at, i.confirmed_at, i.cancelled_at
        FROM payment_instructions i
        WHERE i.holder_code = %s
        ORDER BY i.id
        """,
        (holder_code,),
    ).fetchall()

    billed_total = sum(int(r["amount"]) for r in statement_rows)
    paid_total = sum(int(r["amount"]) for r in payment_rows)
    refunded_total = sum(int(r["amount"]) for r in refund_rows)
    reserved_total = sum(
        int(r["amount"]) for r in instruction_rows if r["status"] == "pending"
    )
    balance = billed_total - paid_total + refunded_total

    return {
        "holder_code": holder["code"],
        "name": holder["name"],
        "billed_total": billed_total,
        "paid_total": paid_total,
        "refunded_total": refunded_total,
        # 待确认预留合计与可用额（可用额 = 余额 - 待确认预留，可为负）
        "reserved_total": reserved_total,
        "balance": balance,
        "available_balance": balance - reserved_total,
        "statements": [
            {
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "amount": int(r["amount"]),
                "issued_at": r["issued_at"],
            }
            for r in statement_rows
        ],
        "payments": [
            {
                "payment_id": r["id"],
                "external_ref": r["external_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in payment_rows
        ],
        "refunds": [
            {
                "refund_id": r["refund_id"],
                "refund_ref": r["refund_ref"],
                "payment_id": r["payment_id"],
                "payment_ref": r["payment_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in refund_rows
        ],
        # 预留指令（含终态）：财务对账可见待确认预留、可用额与指令状态。
        # 历史单据与付款不可改写；已取消/已确认指令同样只追加、不改写。
        "instructions": [
            {
                "instruction_id": r["id"],
                "instruction_ref": r["instruction_ref"],
                "amount": int(r["amount"]),
                "status": r["status"],
                "external_ref": r["external_ref"],
                "payment_id": r["payment_id"],
                "created_at": r["created_at"],
                "confirmed_at": r["confirmed_at"],
                "cancelled_at": r["cancelled_at"],
            }
            for r in instruction_rows
        ],
    }
