from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from .. import snapshots_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import BalanceSnapshotIn, BalanceSnapshotOut, BalanceSnapshotPage

router = APIRouter(tags=["balance-snapshots"])


@router.post("/balance-snapshots")
def create_balance_snapshot(body: BalanceSnapshotIn, conn=Depends(get_conn)):
    """冻结一次权利人余额核对底稿。

    - 凭唯一快照号 snapshot_no 与权利人编号集合（空集合 = 全体）创建；
      基于同一已提交数据库视图固化每人的已出具单据净额、付款总额、
      退回总额、待确认预留额、余额（=单据净额-付款+退回）与可用额
      （=余额-待确认预留），金额均为整数分；
    - 集合内编号重复 422 duplicate_holder_code、权利人不存在 404
      holder_not_found：整次拒绝且不留快照；
    - 同号同集合重试幂等返回原快照（status=duplicate，HTTP 200）；
      同号异集合 409 snapshot_conflict；
    - 与出单、付款、退票、预留确认/取消并发时，整张快照来自同一
      已提交视图（单语句快照），不混合前后状态；
    - 快照一经冻结不可修改：后续流水与权利人建档不改历史。
    """
    result = svc.create_snapshot(conn, body.snapshot_no, body.holder_codes)
    # 新快照 201；同号同集合重试 200 且返回原快照
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=BalanceSnapshotOut(**result).model_dump(mode="json"),
    )


@router.get("/balance-snapshots/{snapshot_no}", response_model=BalanceSnapshotPage)
def get_balance_snapshot(
    snapshot_no: str,
    conn=Depends(get_conn),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=50, ge=1, le=500, description="每页条数，1-500"),
):
    """按快照号分页读取冻结明细及各项总计。

    - lines 按权利人编号升序分页；totals 为跨全部冻结行的各项总计
      （与分页无关），空库快照返回零总计；
    - 快照一经冻结不改写：后续出单、付款、退票、预留确认/取消与
      权利人建档均不影响本结果；
    - 未知快照号 404 snapshot_not_found；页码/每页条数非法 422。
    """
    result = svc.get_snapshot_page(conn, snapshot_no, page, page_size)
    if result is None:
        raise APIError(
            404,
            "snapshot_not_found",
            f"balance snapshot {snapshot_no} not found",
        )
    return result
