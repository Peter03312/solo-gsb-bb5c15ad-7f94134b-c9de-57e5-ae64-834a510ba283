from decimal import Decimal

import psycopg
from fastapi import APIRouter, Depends

from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    AgreementOut,
    AgreementShareOut,
    HolderIn,
    HolderOut,
    WorkIn,
    WorkOut,
    AgreementIn,
)

router = APIRouter()

# 协议登记咨询锁：高 32 位命名空间（任意常数），低 32 位为作品键
_AGREEMENT_LOCK_NS = 0x41475231  # "AGR1"


def _combined_lock_key(namespace: int, work_code: str) -> int:
    import hashlib

    n = int.from_bytes(
        hashlib.blake2b(work_code.encode(), digest_size=4).digest(), "big"
    )
    combined = (namespace << 32) | (n & 0xFFFFFFFF)
    return combined - (1 << 64) if combined >= (1 << 63) else combined


@router.post("/works", response_model=WorkOut, tags=["catalog"])
def register_work(body: WorkIn, conn=Depends(get_conn)):
    """登记作品。同编号幂等；同编号标题不同则拒绝。"""
    with conn.transaction():
        row = conn.execute(
            "SELECT title FROM works WHERE code = %s", (body.code,)
        ).fetchone()
        if row is not None:
            if row["title"] != body.title:
                raise APIError(
                    409, "work_conflict",
                    f"work {body.code} already exists with a different title",
                )
            return WorkOut(code=body.code, title=row["title"], created=False)
        conn.execute(
            "INSERT INTO works(code, title) VALUES (%s, %s)",
            (body.code, body.title),
        )
    return WorkOut(code=body.code, title=body.title, created=True)


@router.get("/works", tags=["catalog"])
def list_works(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT code, title FROM works ORDER BY code"
    ).fetchall()
    return {"works": [{"code": r["code"], "title": r["title"]} for r in rows]}


@router.post("/rightsholders", response_model=HolderOut, tags=["catalog"])
def register_holder(body: HolderIn, conn=Depends(get_conn)):
    """登记权利人（登记协议时也会自动建档，此接口可选）。"""
    with conn.transaction():
        row = conn.execute(
            "SELECT name FROM rightsholders WHERE code = %s", (body.code,)
        ).fetchone()
        if row is not None:
            if body.name is not None and row["name"] is not None and row["name"] != body.name:
                raise APIError(
                    409, "holder_conflict",
                    f"rightsholder {body.code} already exists with a different name",
                )
            if row["name"] is None and body.name is not None:
                conn.execute(
                    "UPDATE rightsholders SET name = %s WHERE code = %s",
                    (body.name, body.code),
                )
            return HolderOut(code=body.code, name=body.name or row["name"], created=False)
        conn.execute(
            "INSERT INTO rightsholders(code, name) VALUES (%s, %s)",
            (body.code, body.name),
        )
    return HolderOut(code=body.code, name=body.name, created=True)


@router.get("/rightsholders", tags=["catalog"])
def list_holders(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT code, name FROM rightsholders ORDER BY code"
    ).fetchall()
    return {"rightsholders": [{"code": r["code"], "name": r["name"]} for r in rows]}


@router.post("/agreements", response_model=AgreementOut, status_code=201, tags=["catalog"])
def create_agreement(body: AgreementIn, conn=Depends(get_conn)):
    """登记分成协议：比例合计须 100%，同作品区间不得重叠。"""
    try:
        with conn.transaction():
            work = conn.execute(
                "SELECT 1 FROM works WHERE code = %s", (body.work_code,)
            ).fetchone()
            if work is None:
                raise APIError(
                    422, "work_not_found",
                    f"work {body.work_code} is not registered",
                )

            # 同一作品的协议登记串行化：事务咨询锁在本事务提交/回滚前持有。
            # 配合下面的 OVERLAPS 检测，可在无 btree_gist 扩展的情况下杜绝重叠。
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (_combined_lock_key(_AGREEMENT_LOCK_NS, body.work_code),),
            )
            # 半开区间重叠判定；valid_to 为 NULL 视为开放上界，相接不视为重叠
            overlap = conn.execute(
                """
                SELECT id FROM agreements
                WHERE work_code = %s
                  AND (valid_from, COALESCE(valid_to, 'infinity'::timestamptz))
                      OVERLAPS
                      (%s::timestamptz, COALESCE(%s::timestamptz, 'infinity'::timestamptz))
                LIMIT 1
                """,
                (body.work_code, body.valid_from, body.valid_to),
            ).fetchone()
            if overlap is not None:
                raise APIError(
                    409, "agreement_overlap",
                    f"agreement range overlaps existing agreement {overlap['id']} "
                    f"of work {body.work_code}",
                )

            # 自动建档权利人
            for s in body.shares:
                conn.execute(
                    """
                    INSERT INTO rightsholders(code) VALUES (%s)
                    ON CONFLICT (code) DO NOTHING
                    """,
                    (s.holder_code,),
                )
            row = conn.execute(
                """
                INSERT INTO agreements(work_code, valid_from, valid_to, note)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (body.work_code, body.valid_from, body.valid_to, body.note),
            ).fetchone()
            agreement_id = row["id"]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO agreement_shares(agreement_id, holder_code, percent)
                    VALUES (%s, %s, %s)
                    """,
                    [
                        (agreement_id, s.holder_code, Decimal(str(s.percent)))
                        for s in body.shares
                    ],
                )
    except psycopg.errors.CheckViolation as exc:
        # 延迟触发器触发：比例合计不为 100%（双重保险）
        raise APIError(422, "shares_total_invalid", str(exc.diag.message_primary))

    return _fetch_agreement(conn, agreement_id)


@router.get("/agreements", response_model=list[AgreementOut], tags=["catalog"])
def list_agreements(work_code: str | None = None, conn=Depends(get_conn)):
    if work_code is not None:
        rows = conn.execute(
            "SELECT id FROM agreements WHERE work_code = %s ORDER BY valid_from",
            (work_code,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM agreements ORDER BY id").fetchall()
    return [_fetch_agreement(conn, r["id"]) for r in rows]


@router.get("/agreements/{agreement_id}", response_model=AgreementOut, tags=["catalog"])
def get_agreement(agreement_id: int, conn=Depends(get_conn)):
    return _fetch_agreement(conn, agreement_id)


def _fetch_agreement(conn, agreement_id: int) -> AgreementOut:
    row = conn.execute(
        """
        SELECT id, work_code, valid_from, valid_to, note
        FROM agreements WHERE id = %s
        """,
        (agreement_id,),
    ).fetchone()
    if row is None:
        raise APIError(404, "agreement_not_found", f"agreement {agreement_id} not found")
    share_rows = conn.execute(
        """
        SELECT holder_code, percent
        FROM agreement_shares WHERE agreement_id = %s
        ORDER BY holder_code
        """,
        (agreement_id,),
    ).fetchall()
    return AgreementOut(
        id=row["id"],
        work_code=row["work_code"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        note=row["note"],
        shares=[
            AgreementShareOut(holder_code=s["holder_code"], percent=str(s["percent"]))
            for s in share_rows
        ],
    )
