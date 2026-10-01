"""银行退票（付款退回）登记。

财务收到银行退票时，凭"原付款流水号（payment_ref）+ 退回流水号（refund_ref）
+ 正整数分额（amount）"登记退回：

- 原付款与单据保持不可变，退回只追加 payment_refunds 一条记录，
  并把退回额加回权利人余额（余额 = 单据累计 - 付款累计 + 退回累计，可为负）；
- 同一退回流水号同内容重试：幂等返回原记录（status=duplicate，HTTP 200）；
  同退回流水号异内容（原付款流水号或金额不同）：409 refund_ref_conflict；
- 原付款不存在：404 payment_not_found；金额非正整数 422（Pydantic）；
  累计退回超过原付款金额：409 refund_exceeds_payment，拒绝且不留记录；
- 并发协议（在同一事务内按固定次序加锁）：
  1) 退回流水号事务咨询锁 —— 同一退回流水号的并发重试在此串行化，
     幂等/冲突判定确定，不依赖唯一约束竞态；
  2) 锁定原付款行（FOR UPDATE）—— 同一原付款的多笔并发退回串行，
     "累计退回 + 本笔 > 原付款金额"的校验与插入原子完成，多个退回绝不合计超额；
  3) 锁定权利人行 —— 与付款登记、出单持有的权利人行锁互斥，
     退回、付款与出单并发时余额校验所见即所入账；后续付款仍只允许在正余额内登记。
  加锁次序全局一致（咨询锁 → 付款行 → 权利人行），无死锁。
"""
import hashlib

from .errors import APIError

# 退回流水号咨询锁命名空间
_REFUND_REF_NS = 0x52464E31  # "RFN1"


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _ref_lock_key(refund_ref: str) -> int:
    digest = hashlib.blake2b(refund_ref.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


def _refund_payload(row, conn, status: str) -> dict:
    """组装退回记录响应；balance_after 取登记/重试时的当前余额。"""
    from .payments_service import get_balance

    return {
        "refund_id": row["id"],
        "payment_id": row["payment_id"],
        "payment_ref": row["payment_ref"],
        "refund_ref": row["refund_ref"],
        "amount": int(row["amount"]),
        "registered_at": row["created_at"],
        "status": status,
        "balance_after": get_balance(conn, row["holder_code"]),
    }


def register_refund(conn, payment_ref: str, refund_ref: str, amount: int) -> dict:
    """登记一笔银行退票（付款退回）。

    - 原付款不存在：404 payment_not_found，不留退回；
    - 同一退回流水号同内容重试：返回原退回记录与当前余额（幂等）；
    - 同一退回流水号异内容（原付款流水号或金额不同）：409 refund_ref_conflict；
    - 累计退回超过原付款金额：409 refund_exceeds_payment，拒绝且不留退回。
    """
    with conn.transaction():
        # 1) 按退回流水号加锁：并发的同流水号请求在此串行化
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_REFUND_REF_NS, _ref_lock_key(refund_ref)),),
        )

        existing = conn.execute(
            """
            SELECT r.id, r.payment_id, r.refund_ref, r.amount, r.created_at,
                   p.external_ref AS payment_ref, p.holder_code
            FROM payment_refunds r
            JOIN payments p ON p.id = r.payment_id
            WHERE r.refund_ref = %s
            """,
            (refund_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["payment_ref"] != payment_ref
                or int(existing["amount"]) != amount
            ):
                raise _ref_conflict(refund_ref, existing)
            return _refund_payload(existing, conn, status="duplicate")

        # 2) 锁定原付款行：同一原付款的并发退回在此串行，累计退回校验确定；
        #    原付款不存在 => 404（不留任何退回记录）。
        payment = conn.execute(
            """
            SELECT id, holder_code, amount
            FROM payments
            WHERE external_ref = %s
            FOR UPDATE
            """,
            (payment_ref,),
        ).fetchone()
        if payment is None:
            raise _payment_not_found(payment_ref)
        payment_id = payment["id"]
        holder_code = payment["holder_code"]
        paid_amount = int(payment["amount"])

        refunded = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS refunded "
                "FROM payment_refunds WHERE payment_id = %s",
                (payment_id,),
            ).fetchone()["refunded"]
        )
        if refunded + amount > paid_amount:
            raise _exceeds_payment(
                payment_ref, amount, paid_amount, refunded
            )

        # 3) 锁定权利人行：与付款登记/出单互斥，余额口径并发一致。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:  # 付款外键已建档，理论不可达
            raise _payment_not_found(payment_ref)

        from .payments_service import get_balance

        balance_before = get_balance(conn, holder_code)

        row = conn.execute(
            """
            INSERT INTO payment_refunds(payment_id, refund_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (payment_id, refund_ref, amount),
        ).fetchone()

        return {
            "refund_id": row["id"],
            "payment_id": payment_id,
            "payment_ref": payment_ref,
            "refund_ref": refund_ref,
            "amount": amount,
            "registered_at": row["created_at"],
            "status": "created",
            "balance_after": balance_before + amount,
        }


def _payment_not_found(payment_ref: str):
    return APIError(
        404,
        "payment_not_found",
        f"original payment {payment_ref} not found; refund rejected",
        detail={"payment_ref": payment_ref},
    )


def _ref_conflict(refund_ref: str, existing) -> "object":
    return APIError(
        409,
        "refund_ref_conflict",
        f"refund ref {refund_ref} already registered with different content",
        detail={
            "refund_ref": refund_ref,
            "existing_payment_ref": existing["payment_ref"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _exceeds_payment(payment_ref: str, amount: int, paid_amount: int, refunded: int):
    return APIError(
        409,
        "refund_exceeds_payment",
        f"refunds of payment {payment_ref} would total {refunded + amount}, "
        f"exceeding the original payment amount {paid_amount}; refund rejected",
        detail={
            "payment_ref": payment_ref,
            "requested_amount": amount,
            "payment_amount": paid_amount,
            "already_refunded": refunded,
        },
    )
