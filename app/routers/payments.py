from fastapi import APIRouter, Depends

from .. import payments_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import HolderLedger, PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import payments_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import HolderLedger, PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


@router.post("/payments")
def register_payment(body: PaymentIn, conn=Depends(get_conn)):
    """登记一笔已完成付款。

    - 余额口径：全部期间已出具单据明细（初次单与正负追补单）累计额
      减去已登记付款；试算不出具、待处理播放不入账；
    - 仅当付款金额不超过当时的**正余额**时入账，返回付款记录及登记后余额；
    - 同一外部流水号同内容重试幂等返回原记录（status=duplicate，HTTP 200）；
      同流水号异内容（权利人或金额不同）返回 409 payment_ref_conflict；
    - 权利人不存在 404；金额非正整数 422；余额不为正或超额 409
      payment_exceeds_balance；拒绝时不留任何付款；
    - 跨期负额追补后历史付款不撤销，负余额留待后续正额抵扣，余额非正禁止付款；
    - 与出单或其他付款并发时，行锁保证"校验 + 入账"一致，绝不超付。
    """
    result = svc.register_payment(conn, body.holder_code, body.external_ref, body.amount)
    # 新登记 201；同流水号同内容重试 200 且返回原记录
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(status_code=status, content=PaymentOut(**result).model_dump(mode="json"))


@router.get("/rightsholders/{holder_code}/ledger", response_model=HolderLedger)
def get_holder_ledger(holder_code: str, conn=Depends(get_conn)):
    """按权利人核对：单据增减、付款流水、逐笔退回与当前余额。

    - statements[] 为该权利人全部期间已出具单据的明细行（追补差额保留正负号），
      按单据出具次序排列；payments[] 为已登记付款流水；
      refunds[] 为逐笔银行退回（携带原付款流水号）；
    - billed_total=单据正负累计、paid_total=付款累计、refunded_total=退回累计、
      balance=billed_total-paid_total+refunded_total（可为负，即待后续正额抵扣）；
    - 待处理播放与试算不在内；权利人不存在返回 404 holder_not_found。
    """
    result = svc.get_holder_ledger(conn, holder_code)
    if result is None:
        raise APIError(
            404,
            "holder_not_found",
            f"rightsholder {holder_code} not found",
        )
    return result
