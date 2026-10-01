"""付款额度预留指令（财务"先预留、后确认付款"两阶段）。

财务可凭权利人编号 holder_code、调用方唯一指令号 instruction_ref 与
正整数分额 amount 预留付款额度：

- 创建预留：仅当"当前余额 − 全部待确认预留（可用额）"仍足额才创建
  pending 指令，返回指令状态与可用额；同号同内容重试返回原指令，
  同号异内容（权利人或金额不同）拒绝（409 reservation_ref_conflict）。
- 取消待确认指令：pending -> cancelled，原子释放预留额度；
  已取消指令重复取消幂等返回原指令；已确认指令不可取消（409）。
- 确认待确认指令：提交外部付款流水号 external_ref，原子登记一笔现有
  payments 付款并把 pending -> confirmed（指令与付款双向绑定），
  返回付款记录及余额。同流水号确认重试幂等（status=duplicate）；
  异流水号或该流水号已被其他付款占用拒绝（409）。
- 负额追补 FIFO 保护：确认时可用额可能已被跨期负额追补冲减，
  必须留足"更早创建（id 更小）的待确认指令 + 本指令"金额，
  即 balance >= earlier_pending + amount，否则 409
  payment_exceeds_balance 拒绝且指令不变（晚于本指令的预留不被保护，
  其确认时再各自校验）。

并发协议（统一加锁次序，无死锁）：
- 创建预留：指令号事务咨询锁 → 权利人行锁（FOR UPDATE）；
- 取消：指令号事务咨询锁 → 指令行锁（FOR UPDATE）；
- 确认：外部付款流水号事务咨询锁 → 指令行锁（FOR UPDATE）
  → 权利人行锁（FOR UPDATE）→ 原子插入付款并结束预留。
确认不取指令号咨询锁：同指令的并发确认在"流水号咨询锁 → 指令行锁"
上串行（先到者把状态改为 confirmed，后到者看到终态走幂等/冲突分支）。
"""
from .errors import APIError
from .payments_service import (
    _PAYMENT_REF_NS,
    get_balance,
    get_reserved_total,
    take_ref_advisory_lock,
)

# 指令号事务咨询锁命名空间
_INSTRUCTION_NS = 0x52535631  # "RSV1"

_INSTRUCTION_COLS = (
    "id, holder_code, instruction_ref, amount, status, external_ref, "
    "payment_id, created_at, confirmed_at, cancelled_at"
)


def _instruction_payload(row, conn, *, request_status: str | None = None) -> dict:
    """组装指令响应：始终附带当前余额、待确认预留合计与可用额。

    request_status 仅用于区分"新登记 created"与"幂等重试 duplicate"的
    请求级状态；指令自身状态始终取数据库行的 status。
    """
    balance = get_balance(conn, row["holder_code"])
    reserved = get_reserved_total(conn, row["holder_code"])
    return {
        "instruction_id": row["id"],
        "holder_code": row["holder_code"],
        "instruction_ref": row["instruction_ref"],
        "amount": int(row["amount"]),
        "status": request_status or row["status"],
        "instruction_status": row["status"],
        "external_ref": row["external_ref"],
        "payment_id": row["payment_id"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "cancelled_at": row["cancelled_at"],
        "balance": balance,
        "reserved_total": reserved,
        "available_balance": balance - reserved,
    }


def get_instruction(conn, instruction_ref: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions "
        "WHERE instruction_ref = %s",
        (instruction_ref,),
    ).fetchone()
    if row is None:
        return None
    return _instruction_payload(row, conn)


def create_reservation(conn, holder_code: str, instruction_ref: str, amount: int) -> dict:
    """预留付款额度。

    - 权利人不存在：404 holder_not_found，不留指令；
    - 同号同内容重试：返回原指令（status=duplicate，HTTP 200），
      附带当前余额/待确认预留/可用额；
    - 同号异内容（权利人或金额不同）：409 reservation_ref_conflict；
    - 当前余额扣除全部待确认预留后不足（可用额 < amount，
      或余额本身不为正/不足）：409 payment_exceeds_balance，拒绝且不留指令。
    """
    with conn.transaction():
        # 1) 指令号咨询锁：同号并发请求串行化，幂等/冲突判定确定。
        take_ref_advisory_lock(conn, _INSTRUCTION_NS, instruction_ref)

        existing = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions "
            "WHERE instruction_ref = %s",
            (instruction_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _instruction_conflict(instruction_ref, existing)
            return _instruction_payload(existing, conn, request_status="duplicate")

        # 2) 锁定权利人行：与付款/退回/出单/确认持有的权利人行锁互斥，
        #    保证"余额 + 待确认预留"校验所见即所入账；权利人不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        reserved = get_reserved_total(conn, holder_code)
        available = balance - reserved
        # 只有当前余额扣除全部待确认预留后仍足额才创建：
        # 余额不为正、余额不足或可用额不足，一律拒绝（不留指令）。
        if balance <= 0 or amount > balance or amount > available:
            raise _exceeds_balance(holder_code, amount, balance, reserved, available)

        row = conn.execute(
            """
            INSERT INTO payment_instructions(
                holder_code, instruction_ref, amount, status
            ) VALUES (%s, %s, %s, 'pending')
            RETURNING id, created_at
            """,
            (holder_code, instruction_ref, amount),
        ).fetchone()
        created = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _instruction_payload(created, conn, request_status="created")


def cancel_reservation(conn, instruction_ref: str) -> dict:
    """取消待确认指令并释放额度。

    - 指令不存在：404 reservation_not_found；
    - pending -> cancelled：原子更新（cancelled_at 落戳），返回指令与
      当前可用额（额度已释放）；
    - 已取消指令重复取消：幂等返回原指令（status=duplicate，HTTP 200）；
    - 已确认指令不可取消：409 reservation_not_pending，指令不变。
    取消无需锁权利人行：只减少待确认预留，不改变余额/付款；
    与创建、确认的判定经指令行锁互斥即足够。
    """
    with conn.transaction():
        # 1) 指令号咨询锁：同号并发取消/创建串行化。
        take_ref_advisory_lock(conn, _INSTRUCTION_NS, instruction_ref)

        # 2) 锁指令行：与确认（持指令行锁）互斥，
        #    不会出现"取消与确认同时成功"。
        row = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions "
            "WHERE instruction_ref = %s FOR UPDATE",
            (instruction_ref,),
        ).fetchone()
        if row is None:
            raise _reservation_not_found(instruction_ref)

        if row["status"] == "confirmed":
            raise _not_pending(instruction_ref, row["status"])
        if row["status"] == "cancelled":
            return _instruction_payload(row, conn, request_status="duplicate")

        updated = conn.execute(
            """
            UPDATE payment_instructions
            SET status = 'cancelled', cancelled_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, created_at
            """,
            (row["id"],),
        ).fetchone()
        # 行锁下状态只可能仍是 pending；防御性兜底
        if updated is None:
            refreshed = conn.execute(
                f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions "
                "WHERE instruction_ref = %s",
                (instruction_ref,),
            ).fetchone()
            raise _not_pending(instruction_ref, refreshed["status"])

        cancelled = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions WHERE id = %s",
            (row["id"],),
        ).fetchone()
        return _instruction_payload(cancelled, conn, request_status="cancelled")


def confirm_reservation(conn, instruction_ref: str, external_ref: str) -> dict:
    """确认待确认指令：提交外部付款流水号，原子登记付款并结束预留。

    - 指令不存在：404 reservation_not_found；
    - 指令已取消：409 reservation_not_pending，不变；
    - 指令已确认：同流水号重试幂等返回原付款（status=duplicate，HTTP 200）；
      异流水号拒绝 409 reservation_already_confirmed；
    - 流水号已被其他付款占用（或已绑定另一条确认指令）：
      409 payment_ref_conflict；
    - FIFO 余额校验：当前余额不足"更早创建的待确认指令 + 本指令"金额
      （负额追补可能冲减余额）：409 payment_exceeds_balance，
      拒绝且指令保持 pending 不变；
    - 通过后在同一事务内插入 payments（holder/金额取指令内容）并把
      指令置为 confirmed、双向绑定，返回付款记录及余额。
    """
    with conn.transaction():
        # 1) 外部付款流水号咨询锁：与直接付款、其他指令确认同流水号串行，
        #    "流水号是否已被占用 / 同流水号重试"判定确定，不依赖唯一约束竞态。
        take_ref_advisory_lock(conn, _PAYMENT_REF_NS, external_ref)

        # 2) 锁指令行：同指令的并发确认在此串行（先提交者改终态），
        #    也与取消互斥。
        instr = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions "
            "WHERE instruction_ref = %s FOR UPDATE",
            (instruction_ref,),
        ).fetchone()
        if instr is None:
            raise _reservation_not_found(instruction_ref)

        holder_code = instr["holder_code"]
        amount = int(instr["amount"])

        if instr["status"] == "confirmed":
            if instr["external_ref"] != external_ref:
                raise _already_confirmed(instruction_ref, instr)
            return _confirm_payload(instr, conn, request_status="duplicate")
        if instr["status"] == "cancelled":
            raise _not_pending(instruction_ref, "cancelled")

        # pending：先查流水号是否已被一笔现有付款占用。
        existing_payment = conn.execute(
            "SELECT id, holder_code, amount, external_ref, created_at, "
            "instruction_id FROM payments WHERE external_ref = %s",
            (external_ref,),
        ).fetchone()
        if existing_payment is not None:
            # 仅当该付款正是本指令已绑定的付款才算重试；pending 指令尚未
            # 绑定付款，故任何命中都属于"被其他付款占用"。
            raise _payment_ref_occupied(external_ref, existing_payment)

        # 3) 锁权利人行：与直接付款/退回/出单/创建预留互斥，
        #    FIFO 余额校验与付款插入原子完成。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:  # 外键已建档，理论不可达
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        # 更早创建（id 更小）的待确认指令合计：确认后这些预留仍须留足。
        earlier = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS earlier "
                "FROM payment_instructions "
                "WHERE holder_code = %s AND status = 'pending' AND id < %s",
                (holder_code, instr["id"]),
            ).fetchone()["earlier"]
        )
        # 本指令确认后：余额须覆盖"更早待确认预留 + 本笔付款"。
        if balance < earlier + amount:
            raise _fifo_exceeds(
                holder_code, amount, balance, earlier, instr["id"]
            )

        # 4) 原子登记现有付款并结束预留（双向绑定，同一事务提交）。
        payment_row = conn.execute(
            """
            INSERT INTO payments(
                holder_code, external_ref, amount, instruction_id
            ) VALUES (%s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount, instr["id"]),
        ).fetchone()
        conn.execute(
            """
            UPDATE payment_instructions
            SET status = 'confirmed', external_ref = %s,
                payment_id = %s, confirmed_at = now()
            WHERE id = %s AND status = 'pending'
            """,
            (external_ref, payment_row["id"], instr["id"]),
        )

        confirmed = conn.execute(
            f"SELECT {_INSTRUCTION_COLS} FROM payment_instructions WHERE id = %s",
            (instr["id"],),
        ).fetchone()
        payload = _confirm_payload(
            confirmed, conn, request_status="created",
            payment_id=payment_row["id"], registered_at=payment_row["created_at"],
            balance_after=balance - amount,
        )
        return payload


def _confirm_payload(
    row, conn, *, request_status: str,
    payment_id=None, registered_at=None, balance_after=None,
) -> dict:
    """组装确认响应：付款记录（兼容付款登记字段）+ 余额 + 指令状态。"""
    pid = payment_id or row["payment_id"]
    if registered_at is None or balance_after is None:
        payment = conn.execute(
            "SELECT created_at FROM payments WHERE id = %s", (pid,),
        ).fetchone()
        registered_at = payment["created_at"]
        # 幂等重试时 balance_after 为当前余额（与 POST /payments 重试约定一致）
        balance_after = get_balance(conn, row["holder_code"])
    return {
        # 付款记录及余额（与 POST /payments 响应兼容）
        "payment_id": pid,
        "holder_code": row["holder_code"],
        "external_ref": row["external_ref"],
        "amount": int(row["amount"]),
        "registered_at": registered_at,
        "balance_after": balance_after,
        # 预留指令信息
        "status": request_status,       # created | duplicate
        "instruction_ref": row["instruction_ref"],
        "instruction_id": row["id"],
        "instruction_status": row["status"],
        "confirmed_at": row["confirmed_at"],
    }


def _holder_not_found(holder_code: str):
    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _reservation_not_found(instruction_ref: str):
    return APIError(
        404,
        "reservation_not_found",
        f"payment instruction {instruction_ref} not found",
        detail={"instruction_ref": instruction_ref},
    )


def _instruction_conflict(instruction_ref: str, existing):
    return APIError(
        409,
        "reservation_ref_conflict",
        f"instruction ref {instruction_ref} already reserved with different content",
        detail={
            "instruction_ref": instruction_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
            "existing_status": existing["status"],
        },
    )


def _not_pending(instruction_ref: str, status: str):
    return APIError(
        409,
        "reservation_not_pending",
        f"payment instruction {instruction_ref} is {status}, not pending; "
        "operation rejected and the instruction is unchanged",
        detail={"instruction_ref": instruction_ref, "instruction_status": status},
    )


def _already_confirmed(instruction_ref: str, existing):
    return APIError(
        409,
        "reservation_already_confirmed",
        f"payment instruction {instruction_ref} already confirmed with a different "
        f"external ref; retry with the original ref to stay idempotent",
        detail={
            "instruction_ref": instruction_ref,
            "existing_external_ref": existing["external_ref"],
            "payment_id": existing["payment_id"],
        },
    )


def _payment_ref_occupied(external_ref: str, existing_payment):
    return APIError(
        409,
        "payment_ref_conflict",
        f"external payment ref {external_ref} is already used by another payment; "
        "confirm rejected and the instruction stays pending",
        detail={
            "external_ref": external_ref,
            "existing_payment_id": existing_payment["id"],
            "existing_holder_code": existing_payment["holder_code"],
            "existing_amount": int(existing_payment["amount"]),
            "existing_instruction_id": existing_payment["instruction_id"],
        },
    )


def _fifo_exceeds(holder_code: str, amount: int, balance: int, earlier: int, instr_id: int):
    return APIError(
        409,
        "payment_exceeds_balance",
        f"confirming instruction {instr_id} for {amount} would leave balance "
        f"{balance} short of earlier pending reservations {earlier}; "
        "confirm rejected and the instruction stays pending",
        detail={
            "holder_code": holder_code,
            "requested_amount": amount,
            "balance": balance,
            "earlier_pending_total": earlier,
            "required": earlier + amount,
        },
    )


def _exceeds_balance(holder_code: str, amount: int, balance: int, reserved: int, available: int):
    # 创建预留复用付款超额错误码，保持"超额拒绝约定"一致
    return APIError(
        409,
        "payment_exceeds_balance",
        f"reservation of {amount} exceeds available balance {available} "
        f"(balance {balance} minus pending reservations {reserved}) "
        f"of rightsholder {holder_code}; reservation rejected",
        detail={
            "holder_code": holder_code,
            "requested_amount": amount,
            "balance": balance,
            "reserved_total": reserved,
            "available": available,
        },
    )
