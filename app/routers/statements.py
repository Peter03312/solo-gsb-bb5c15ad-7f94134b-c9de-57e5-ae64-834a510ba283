from fastapi import APIRouter, Depends, Query

from .. import statements_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    AttributionPage,
    PeriodParams,
    StatementOut,
    StatementSummary,
)

router = APIRouter(prefix="/statements", tags=["statements"])


@router.post("/issue", response_model=StatementOut)
def issue(period: PeriodParams, conn=Depends(get_conn)):
    """按期间出具单据。

    - 首次：出具初次单（已出单据不可覆盖），并冻结逐笔归因证据；
    - 再次：按最新应付额与此期间全部单据累计额之差出具追补单，差额可正可负；
      逐笔归因相对上次结果给出 added/removed（撤销为负额，不轧差），
      不可追溯旧账差额单列为 legacy；
    - 应付无变化时不产生新单据、也不生成归因证据；
    - 同一期间并发出具由咨询锁串行化，不会产生重复单据或重复证据。
    """
    return svc.issue_statement(conn, period.start, period.end)


@router.get("/preview")
def preview(period: PeriodParams = Depends(), conn=Depends(get_conn)):
    """不出具，仅试算当前应付额、待处理清单及若追补将产生的差额。"""
    return svc.preview_period(conn, period.start, period.end)


@router.get("", response_model=list[StatementSummary])
def list_statements(period: PeriodParams = Depends(), conn=Depends(get_conn)):
    """列出某期间的全部单据（初次单 + 历次追补单，按出具顺序）。"""
    return svc.list_batches(conn, period.start, period.end)


@router.get("/{batch_id}/attributions", response_model=AttributionPage)
def list_attributions(
    batch_id: int,
    conn=Depends(get_conn),
    holder_code: str | None = Query(
        default=None, min_length=1, max_length=200,
        description="只看某位权利人的归因记录（可选）",
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=50, ge=1, le=500, description="每页条数，1-500"),
):
    """按单据（可再按权利人）分页查询逐笔归因记录与逐权利人合计。

    - 归因按更正链原始编号归集，记录出具时冻结的链尾编号、作品、发生时间、
      匹配协议与逐笔分摊；追补单含 added/removed/legacy 三类，负额保留；
    - holder_totals 中 attributed_total 恒等于该单据原明细行金额
      （reconciles=true）；不可追溯的存量单据返回 traceable=false、
      空记录，且 reconciles=false，明确标明无法逐笔核对；
    - 不存在的单据 404；页码/每页条数非法 422；查询本身不改变任何历史证据。
    """
    if holder_code is not None:
        holder = conn.execute(
            "SELECT 1 FROM rightsholders WHERE code = %s", (holder_code,)
        ).fetchone()
        if holder is None:
            raise APIError(
                404, "holder_not_found",
                f"rightsholder {holder_code} not found",
            )
    result = svc.get_attributions(conn, batch_id, holder_code, page, page_size)
    if result is None:
        raise APIError(
            404, "statement_not_found",
            f"statement batch {batch_id} not found",
        )
    return result
