from fastapi import APIRouter, Depends

from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    PlayChainItem,
    PlayChainOut,
    PlayIn,
    PlayBulkIn,
    PlayResult,
)

router = APIRouter()


def _same_content(a: PlayIn, row) -> bool:
    """同编号幂等：作品、发生时间、金额、更正引用全部一致才算同内容。"""
    return (
        a.work_code == row["work_code"]
        and a.occurred_at == row["occurred_at"]
        and a.amount == row["amount"]
        and a.corrects_id == row["corrects_id"]
    )


def _import_one(conn, p: PlayIn) -> PlayResult:
    # 引用的原记录必须存在
    parent = None
    if p.corrects_id is not None:
        parent = conn.execute(
            "SELECT import_id FROM plays WHERE import_id = %s FOR UPDATE",
            (p.corrects_id,),
        ).fetchone()
        if parent is None:
            raise APIError(
                422, "original_not_found",
                f"original record {p.corrects_id} referenced by corrects_id does not exist",
            )
        # 原记录已被他人更正 => 再更正会形成分叉，拒绝
        successor = conn.execute(
            "SELECT import_id FROM plays WHERE corrects_id = %s",
            (p.corrects_id,),
        ).fetchone()
        if successor is not None:
            raise APIError(
                409, "correction_fork",
                f"record {p.corrects_id} already has correction {successor['import_id']}; "
                "forking is not allowed",
            )

    work = conn.execute(
        "SELECT 1 FROM works WHERE code = %s FOR SHARE", (p.work_code,)
    ).fetchone()
    if work is None:
        raise APIError(
            422, "work_not_found", f"work {p.work_code} is not registered"
        )

    existing = conn.execute(
        "SELECT work_code, occurred_at, amount, corrects_id FROM plays WHERE import_id = %s",
        (p.import_id,),
    ).fetchone()
    if existing is not None:
        if not _same_content(p, existing):
            raise APIError(
                409, "import_id_conflict",
                f"import_id {p.import_id} already exists with different content",
            )
        tail = conn.execute(
            "SELECT 1 FROM plays WHERE corrects_id = %s", (p.import_id,)
        ).fetchone() is None
        return PlayResult(
            import_id=p.import_id,
            status="duplicate",
            is_correction=p.corrects_id is not None,
            corrects_id=p.corrects_id,
            chain_tail=tail,
        )

    conn.execute(
        """
        INSERT INTO plays(import_id, work_code, occurred_at, amount, corrects_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (p.import_id, p.work_code, p.occurred_at, p.amount, p.corrects_id),
    )
    return PlayResult(
        import_id=p.import_id,
        status="created",
        is_correction=p.corrects_id is not None,
        corrects_id=p.corrects_id,
        chain_tail=True,
    )


@router.post("/plays/import", response_model=PlayResult, status_code=201, tags=["plays"])
def import_play(body: PlayIn, conn=Depends(get_conn)):
    """导入单条播放记录；同编号同内容幂等，异内容 409。"""
    with conn.transaction():
        return _import_one(conn, body)


@router.post("/plays/bulk-import", status_code=201, tags=["plays"])
def import_plays_bulk(body: PlayBulkIn, conn=Depends(get_conn)):
    """批量导入：整批在一个事务内，任一条冲突/非法则整批拒绝。"""
    # 批次内同一编号不得重复
    ids = [p.import_id for p in body.plays]
    if len(set(ids)) != len(ids):
        raise APIError(422, "duplicate_in_batch", "duplicate import_id within batch")
    results = []
    with conn.transaction():
        for p in body.plays:
            results.append(_import_one(conn, p))
    return {"results": [r.model_dump() for r in results]}


@router.get("/plays/{import_id}/chain", response_model=PlayChainOut, tags=["plays"])
def get_play_chain(import_id: str, conn=Depends(get_conn)):
    """查询某编号所在的更正链（从原始记录到链尾）。"""
    row = conn.execute(
        "SELECT import_id, corrects_id FROM plays WHERE import_id = %s",
        (import_id,),
    ).fetchone()
    if row is None:
        raise APIError(404, "play_not_found", f"play {import_id} not found")

    # 沿 corrects_id 回溯到原始记录
    root_id = import_id
    guard = 0
    while True:
        cur = conn.execute(
            "SELECT import_id, corrects_id FROM plays WHERE import_id = %s",
            (root_id,),
        ).fetchone()
        if cur is None or cur["corrects_id"] is None:
            break
        root_id = cur["corrects_id"]
        guard += 1
        if guard > 100000:
            raise APIError(500, "chain_too_long", "correction chain too long")

    # 从原始记录向后取链（corrects_id 唯一，链必然线性）
    chain_rows = []
    current = root_id
    guard = 0
    while current is not None:
        r = conn.execute(
            """
            SELECT import_id, work_code, occurred_at, amount, corrects_id
            FROM plays WHERE import_id = %s
            """,
            (current,),
        ).fetchone()
        if r is None:
            break
        chain_rows.append(r)
        nxt = conn.execute(
            "SELECT import_id FROM plays WHERE corrects_id = %s", (current,)
        ).fetchone()
        current = nxt["import_id"] if nxt else None
        guard += 1
        if guard > 100000:
            raise APIError(500, "chain_too_long", "correction chain too long")

    tail_id = chain_rows[-1]["import_id"]
    return PlayChainOut(
        import_id=import_id,
        chain=[
            PlayChainItem(
                import_id=r["import_id"],
                work_code=r["work_code"],
                occurred_at=r["occurred_at"],
                amount=r["amount"],
                corrects_id=r["corrects_id"],
                chain_tail=(r["import_id"] == tail_id),
            )
            for r in chain_rows
        ],
    )
