from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import receipts_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    ReceiptBatchDetail,
    ReceiptBatchIn,
    ReceiptBatchOut,
    ReceiptReverifyOut,
    ReceiptVerificationOut,
    ReceiptVersionList,
)

router = APIRouter(tags=["receipts"])


@router.post("/receipts/import")
def import_receipts(body: ReceiptBatchIn, conn=Depends(get_conn)):
    """按批次导入银行回单，并冻结第 1 版逐行核验结论。

    - 回单行：回单编号、外部付款流水号、权利人编号、正整数分额；
      无效字段（空编号、非正整数金额等）整批拒绝（422），不留任何回单；
    - 批内重复流水号（或重复回单编号）整批拒绝（422）；
    - 批次同内容重试幂等返回原批次（status=duplicate，HTTP 200）；
    - 批次号异内容复用 409 receipt_batch_conflict；
      回单编号异内容复用 409 receipt_no_conflict，均整批拒绝；
    - 跨批次同一流水号仅首次成功导入的回单参与核验，其余标为重复回单；
    - 有效回单按流水号对照已登记付款：matched / payment_not_registered /
      mismatched（holder_mismatch、amount_mismatch，两项均不符同时列出）。
    """
    result = svc.import_batch(conn, body)
    # 新导入 201；同批次同内容重试 200 且返回原批次
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReceiptBatchOut(**result).model_dump(mode="json"),
    )


@router.post("/receipts/{batch_no}/reverify")
def reverify_receipts(batch_no: str, conn=Depends(get_conn)):
    """按当前已登记付款复核某批次（付款迟到后使用）。

    - 结论变化才生成新版本（status=created，HTTP 201）；
      结论不变不新增版本（status=unchanged，HTTP 200，返回现有最新版）；
    - 付款与复核并发时，一版内全部结论来自同一已提交状态（单语句快照）；
    - 复核只新增核验版本，绝不改写付款、余额或单据；
    - 批次不存在返回 404 receipt_batch_not_found。
    """
    result = svc.reverify(conn, batch_no)
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReceiptReverifyOut(**result).model_dump(mode="json"),
    )


@router.get("/receipts/{batch_no}", response_model=ReceiptBatchDetail)
def get_receipt_batch(batch_no: str, conn=Depends(get_conn)):
    """查询批次原始回单行（始终可查）与最新核验版本号。"""
    result = svc.get_batch(conn, batch_no)
    if result is None:
        raise APIError(
            404, "receipt_batch_not_found",
            f"receipt batch {batch_no} not found",
        )
    return result


@router.get("/receipts/{batch_no}/versions", response_model=ReceiptVersionList)
def list_receipt_versions(batch_no: str, conn=Depends(get_conn)):
    """列出某批次的全部核验版本（旧版本始终可查）及各版状态计数。"""
    result = svc.list_versions(conn, batch_no)
    if result is None:
        raise APIError(
            404, "receipt_batch_not_found",
            f"receipt batch {batch_no} not found",
        )
    return result


@router.get(
    "/receipts/{batch_no}/versions/{version}",
    response_model=ReceiptVerificationOut,
)
def get_receipt_version(batch_no: str, version: int, conn=Depends(get_conn)):
    """查询指定核验版本：状态计数及差异行（含付款快照，结论已冻结）。"""
    batch, result = svc.get_version(conn, batch_no, version)
    if batch is None:
        raise APIError(
            404, "receipt_batch_not_found",
            f"receipt batch {batch_no} not found",
        )
    if result is None:
        raise APIError(
            404, "receipt_version_not_found",
            f"receipt batch {batch_no} has no verification version {version}",
        )
    return result
