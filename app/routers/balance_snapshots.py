from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from .. import balance_snapshots_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import BalanceSnapshotCreated, BalanceSnapshotIn, BalanceSnapshotPage

router = APIRouter(tags=["balance-snapshots"])


@router.post("/balance-snapshots")
def create_balance_snapshot(body: BalanceSnapshotIn, conn=Depends(get_conn)):
    """冻结一次权利人余额核对底稿。

    - 接收唯一快照号与权利人编号集合（空集合/省略表示全体已建档权利人）；
      每人固化"已出具单据净额、付款总额、退回总额、待确认预留额、
      余额（=单据净额−付款+退回）与可用额（=余额−待确认预留）"，整数分；
    - 固化取自同一已提交数据库视图，与出单、付款、退票、预留确认/取消
      并发时不混合前后状态；
    - 编号重复 422 duplicate_holder_code；任一权利人不存在
      404 holder_not_found；同号同集合重试 200 status=duplicate 返回原快照；
      同号异集合（全体/指定模式不同或集合不同）409 snapshot_ref_conflict；
      拒绝时不留任何快照；
    - 空库（全体模式无权利人）快照合法：零行明细、各项总计 0。
    """
    result = svc.create_snapshot(conn, body.snapshot_no, body.holder_codes)
    # 新冻结 201；同号同集合重试 200 且返回原快照
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=BalanceSnapshotCreated(**result).model_dump(mode="json"),
    )


@router.get(
    "/balance-snapshots/{snapshot_no}",
    response_model=BalanceSnapshotPage,
)
def get_balance_snapshot(
    snapshot_no: str,
    conn=Depends(get_conn),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=50, ge=1, le=500, description="每页条数，1-500"),
):
    """按快照号分页读取冻结明细及各项总计。

    - records[] 按权利人编号升序；totals 为全快照口径（不随分页变化），
      空快照各项总计为 0；页超出范围时 records 为空（不报 404）；
    - 明细与总计均为冻结值：后续流水（出单/付款/退票/预留）及新建
      权利人不改历史；
    - 未知快照号返回 404 balance_snapshot_not_found。
    """
    result = svc.get_snapshot_page(conn, snapshot_no, page, page_size)
    if result is None:
        raise APIError(
            404,
            "balance_snapshot_not_found",
            f"balance reconciliation snapshot {snapshot_no} not found",
        )
    return result
