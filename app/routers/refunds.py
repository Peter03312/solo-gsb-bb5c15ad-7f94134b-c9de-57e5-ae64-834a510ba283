from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import refunds_service as svc
from ..db import get_conn
from ..schemas import RefundIn, RefundOut

router = APIRouter(tags=["refunds"])


@router.post("/refunds")
def register_refund(body: RefundIn, conn=Depends(get_conn)):
    """登记一笔银行退票（原付款的部分/全部退回）。

    - 凭原付款流水号 payment_ref、退回流水号 refund_ref 与正整数分额登记，
      返回退回记录及登记后余额；原付款与单据保持不可变；
    - 同一退回流水号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
      同退回流水号异内容（原付款流水号或金额不同）409 refund_ref_conflict；
    - 原付款不存在 404 payment_not_found；金额非正整数 422；
      累计退回超过原付款金额 409 refund_exceeds_payment，拒绝且不留记录；
    - 退回额加回权利人余额（余额 = 单据累计 - 付款累计 + 退回累计，可为负）；
      后续付款仍只允许在正余额内登记；
    - 并发退回、付款与出单经"退回流水号咨询锁 + 原付款行锁 + 权利人行锁"
      保持余额校验一致，同一原付款的多个退回绝不合计超额。
    """
    result = svc.register_refund(
        conn, body.payment_ref, body.refund_ref, body.amount
    )
    # 新登记 201；同退回流水号同内容重试 200 且返回原记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=RefundOut(**result).model_dump(mode="json"),
    )
