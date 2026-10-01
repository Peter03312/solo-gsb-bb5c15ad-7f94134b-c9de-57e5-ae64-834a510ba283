from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import reservations_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    ReservationConfirmIn,
    ReservationConfirmOut,
    ReservationIn,
    ReservationOut,
)

router = APIRouter(tags=["payment-reservations"])


@router.post("/payment-reservations")
def create_reservation(body: ReservationIn, conn=Depends(get_conn)):
    """预留付款额度（财务两阶段付款：先预留、后确认）。

    - 凭权利人编号 holder_code、唯一指令号 instruction_ref 与正整数分额
      amount 预留；仅当"当前余额 − 全部待确认预留（可用额）"仍足额才创建
      pending 指令，返回指令状态与可用额；
    - 同号同内容重试幂等返回原指令（status=duplicate，HTTP 200）；
      同号异内容（权利人或金额不同）409 reservation_ref_conflict；
    - 权利人不存在 404 holder_not_found；金额非正整数 422；
      余额不为正/不足，或可用额被待确认预留占用 409 payment_exceeds_balance
      （detail 含 balance、reserved_total、available），拒绝且不留指令。
    """
    result = svc.create_reservation(
        conn, body.holder_code, body.instruction_ref, body.amount
    )
    # 新预留 201；同号同内容重试 200 且返回原指令
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReservationOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-reservations/{instruction_ref}/cancel")
def cancel_reservation(instruction_ref: str, conn=Depends(get_conn)):
    """取消待确认指令并释放额度。

    - pending -> cancelled，返回指令与当前可用额（额度已释放）；
      已取消指令重复取消幂等返回原指令（status=duplicate，HTTP 200）；
    - 已确认指令不可取消：409 reservation_not_pending，指令不变；
      指令不存在 404 reservation_not_found。
    """
    result = svc.cancel_reservation(conn, instruction_ref)
    # 本次取消 200（无新建资源）；重复取消幂等也 200
    return JSONResponse(
        status_code=200,
        content=ReservationOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-reservations/{instruction_ref}/confirm")
def confirm_reservation(
    instruction_ref: str, body: ReservationConfirmIn, conn=Depends(get_conn)
):
    """确认待确认指令：提交外部付款流水号，原子登记付款并结束预留。

    - 同一事务内原子登记一笔现有付款（权利人/金额取自指令）并把指令置为
      confirmed、双向绑定，返回付款记录（兼容 POST /payments 字段）及余额；
    - 同流水号确认重试幂等返回原付款（status=duplicate，HTTP 200）；
      异流水号或该流水号已被其他付款占用 409（reservation_already_confirmed /
      payment_ref_conflict）；
    - 负额追补 FIFO 保护：确认时当前余额须留足"更早创建的待确认指令 +
      本指令"金额，否则 409 payment_exceeds_balance，拒绝且指令保持 pending；
    - 指令不存在 404 reservation_not_found；已取消/非待确认
      409 reservation_not_pending。
    """
    result = svc.confirm_reservation(conn, instruction_ref, body.external_ref)
    # 本次确认原子登记付款 201；同流水号确认重试 200 返回原付款
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReservationConfirmOut(**result).model_dump(mode="json"),
    )


@router.get("/payment-reservations/{instruction_ref}", response_model=ReservationOut)
def get_reservation(instruction_ref: str, conn=Depends(get_conn)):
    """查询单条预留指令：状态（pending/cancelled/confirmed）、
    绑定的付款流水号，以及当前余额、待确认预留合计与可用额。

    指令不存在返回 404 reservation_not_found。
    """
    result = svc.get_instruction(conn, instruction_ref)
    if result is None:
        raise APIError(
            404,
            "reservation_not_found",
            f"payment instruction {instruction_ref} not found",
        )
    return result
