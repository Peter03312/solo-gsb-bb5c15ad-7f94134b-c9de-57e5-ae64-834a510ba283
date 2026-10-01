from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import close_pool, init_pool
from .errors import APIError
from .routers import catalog, payments, plays, receipts, refunds, reservations, statements


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="演出录音播放版税结算服务",
    version="1.4.0",
    description=(
        "登记作品与分成协议、导入播放记录（支持幂等与更正链）、"
        "按播放发生时间匹配协议并逐笔按比例分摊，按期间出具应付/追补单据，"
        "按权利人核对此前已出具单据与实际付款（正余额付款、流水号幂等、负额抵扣），"
        "付款额度预留（凭权利人/唯一指令号/正整数分额预留，取消释放、"
        "提交外部流水号原子确认付款，负额追补下更早预留 FIFO 优先），"
        "登记银行退票（凭原付款流水号与退回流水号、累计不超额、退回加回余额），"
        "并按批次导入银行回单核验已登记付款（冻结逐行结论与累计退回额、"
        "迟到付款/退回复核版本化）。"
    ),
    lifespan=lifespan,
)

app.include_router(catalog.router)
app.include_router(plays.router)
app.include_router(statements.router)
app.include_router(payments.router)
app.include_router(reservations.router)
app.include_router(refunds.router)
app.include_router(receipts.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.exception_handler(APIError)
async def api_error_handler(request, exc: APIError):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.exception_handler(psycopg.Error)
async def psycopg_error_handler(request, exc: psycopg.Error):
    """把并发竞态下数据库直接抛出的约束错误映射为 409/422。"""
    if isinstance(exc, psycopg.errors.ExclusionViolation):
        code, message = "agreement_overlap", str(exc.diag.message_primary or exc)
        status = 409
    elif isinstance(exc, psycopg.errors.UniqueViolation):
        code, message = "duplicate", str(exc.diag.message_primary or exc)
        status = 409
    elif isinstance(exc, psycopg.errors.CheckViolation):
        code, message = "constraint_violation", str(exc.diag.message_primary or exc)
        status = 422
    elif isinstance(exc, psycopg.errors.ForeignKeyViolation):
        code, message = "foreign_key_violation", str(exc.diag.message_primary or exc)
        status = 422
    else:
        code, message = "database_error", str(exc)
        status = 500
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, "detail": None},
    )
