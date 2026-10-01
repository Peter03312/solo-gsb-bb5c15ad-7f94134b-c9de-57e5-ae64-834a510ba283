"""银行回单批次导入与付款核验。

- 财务按批次导入回单（批次编号 + 回单行：回单编号 / 外部付款流水号 /
  权利人编号 / 正整数分额）。批次同内容重试幂等返回原批次；
  批次号或回单编号异内容复用、批内重复流水号（或重复回单编号）、
  无效字段均整批拒绝，不留任何回单。
- 跨批次同一付款流水号：仅首次成功导入的回单参与核验，其余标为重复回单。
- 有效回单按流水号对照已登记付款：匹配 / 付款未登记 / 权利人不符 / 金额不符，
  两项均不符同时列出；导入时冻结逐行结论（第 1 版）。
- 付款迟到后可复核：结论变化才生成新版本，结论不变不新增版本；
  旧版本与原始回单始终可查。
- 退票联动：有效回单仍按原付款字段（流水号、权利人、金额）判定匹配与不符，
  退回不改变结论；导入与复核时另冻结该付款当时的累计退回额，
  仅当"原核验结论或冻结退回额变化"才产生新核验版本，旧版结论不改写；
  重复回单仍不参与核验。
- 并发协议：
  * 导入经"全局导入咨询锁"串行化 —— "首次成功导入"的判定确定、无竞态，
    批次号/回单编号的内容检查与插入原子完成；
  * 复核经"每批次咨询锁"串行化 —— 同一批次并发复核不会产生重复版本；
  * 核验读取付款与累计退回使用单条 SQL（单语句快照）：付款/退回与复核并发时，
    一版内全部逐行结论（含冻结退回额）来自同一已提交状态；
  * 核验只写回单/结论表，绝不改写付款、退回、余额或单据。
"""
import hashlib
import json

from .errors import APIError

# 咨询锁命名空间（高 32 位）与键
_IMPORT_NS = 0x52435054  # "RCPT"：全局导入锁
_BATCH_NS = 0x52435042   # "RCPB"：每批次复核锁

# 核验结论状态
MATCHED = "matched"
NOT_REGISTERED = "payment_not_registered"
MISMATCHED = "mismatched"
DUPLICATE = "duplicate"
_STATUSES = (MATCHED, NOT_REGISTERED, MISMATCHED, DUPLICATE)


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _hash_key(text: str) -> int:
    digest = hashlib.blake2b(text.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


def _content_hash(lines) -> str:
    """批次内容规范化哈希：与回单行次序无关，同一组回单行即同内容。"""
    canonical = sorted(
        (l.receipt_no, l.external_ref, l.holder_code, l.amount) for l in lines
    )
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- 核验计算 ----

def _compute_lines(conn, batch_id: int) -> list[dict]:
    """按当前已登记付款计算某批次全部回单的逐行结论。

    单条 SQL 左连接付款表，并以关联子查询一并取出该付款当前累计退回额：
    即使付款/退回登记与本查询并发，全部行结论（含冻结退回额）也来自同一
    已提交快照（语句级一致性，单事务内不发生重算）。
    """
    rows = conn.execute(
        """
        SELECT r.id AS receipt_id, r.receipt_no, r.external_ref,
               r.holder_code, r.amount, r.is_duplicate,
               p.holder_code AS payment_holder_code,
               p.amount AS payment_amount,
               COALESCE((
                   SELECT SUM(rf.amount)
                   FROM payment_refunds rf
                   WHERE rf.payment_id = p.id
               ), 0) AS payment_refunded_total
        FROM receipts r
        LEFT JOIN payments p ON p.external_ref = r.external_ref
        WHERE r.batch_id = %s
        ORDER BY r.id
        """,
        (batch_id,),
    ).fetchall()

    lines = []
    for r in rows:
        payment_holder = r["payment_holder_code"]
        payment_amount = (
            int(r["payment_amount"]) if r["payment_amount"] is not None else None
        )
        refunded_total = (
            int(r["payment_refunded_total"])
            if payment_holder is not None
            else None
        )
        if r["is_duplicate"]:
            # 重复回单不参与核验（流水号首次导入的回单才有结论）
            status, holder_mm, amount_mm = DUPLICATE, False, False
            payment_holder, payment_amount, refunded_total = None, None, None
        elif payment_holder is None:
            status, holder_mm, amount_mm = NOT_REGISTERED, False, False
        else:
            holder_mm = payment_holder != r["holder_code"]
            amount_mm = payment_amount != int(r["amount"])
            status = MISMATCHED if (holder_mm or amount_mm) else MATCHED
        lines.append(
            {
                "receipt_id": r["receipt_id"],
                "status": status,
                "holder_mismatch": holder_mm,
                "amount_mismatch": amount_mm,
                "payment_holder_code": payment_holder,
                "payment_amount": payment_amount,
                "payment_refunded_total": refunded_total,
            }
        )
    return lines


def _conclusion_tuple(line: dict) -> tuple:
    """一行的"结论"：状态 + 不符项 + 付款快照（含累计退回额），用于版本间比较。"""
    return (
        line["status"],
        bool(line["holder_mismatch"]),
        bool(line["amount_mismatch"]),
        line["payment_holder_code"],
        line["payment_amount"],
        line["payment_refunded_total"],
    )


def _insert_version(conn, batch_id: int, version: int, lines: list[dict]):
    v = conn.execute(
        """
        INSERT INTO receipt_verifications(batch_id, version)
        VALUES (%s, %s)
        RETURNING id, created_at
        """,
        (batch_id, version),
    ).fetchone()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO receipt_verification_lines
                (verification_id, receipt_id, status,
                 holder_mismatch, amount_mismatch,
                 payment_holder_code, payment_amount, payment_refunded_total)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    v["id"], l["receipt_id"], l["status"],
                    l["holder_mismatch"], l["amount_mismatch"],
                    l["payment_holder_code"], l["payment_amount"],
                    l["payment_refunded_total"] or 0,
                )
                for l in lines
            ],
        )
    return v["id"], v["created_at"]


# ---- 输出装配 ----

def _version_payload(conn, batch_id: int, batch_no: str, version: int) -> dict:
    v = conn.execute(
        """
        SELECT id, created_at FROM receipt_verifications
        WHERE batch_id = %s AND version = %s
        """,
        (batch_id, version),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT l.status, l.holder_mismatch, l.amount_mismatch,
               l.payment_holder_code, l.payment_amount,
               l.payment_refunded_total,
               r.receipt_no, r.external_ref, r.holder_code, r.amount
        FROM receipt_verification_lines l
        JOIN receipts r ON r.id = l.receipt_id
        WHERE l.verification_id = %s
        ORDER BY r.receipt_no
        """,
        (v["id"],),
    ).fetchall()

    counts = {s: 0 for s in _STATUSES}
    all_lines = []
    differences = []
    for r in rows:
        counts[r["status"]] += 1
        refunded_total = (
            int(r["payment_refunded_total"])
            if r["payment_holder_code"] is not None
            else None
        )
        item = {
            "receipt_no": r["receipt_no"],
            "external_ref": r["external_ref"],
            "status": r["status"],
            "differences": [],
            "receipt_holder_code": r["holder_code"],
            "receipt_amount": int(r["amount"]),
            "payment_holder_code": r["payment_holder_code"],
            "payment_amount": (
                int(r["payment_amount"])
                if r["payment_amount"] is not None
                else None
            ),
            "payment_refunded_total": refunded_total,
        }
        all_lines.append(item)
        if r["status"] == MATCHED:
            continue
        diffs = []
        if r["holder_mismatch"]:
            diffs.append("holder_mismatch")
        if r["amount_mismatch"]:
            diffs.append("amount_mismatch")
        item["differences"] = diffs
        differences.append(item)
    counts["total"] = len(rows)
    return {
        "batch_no": batch_no,
        "version": version,
        "created_at": v["created_at"],
        "counts": counts,
        "differences": differences,
        # 含 matched 行的全部逐行结论（matched 行带冻结累计退回额）
        "lines": all_lines,
    }


def _receipt_rows(conn, batch_id: int):
    return conn.execute(
        """
        SELECT receipt_no, external_ref, holder_code, amount, is_duplicate
        FROM receipts
        WHERE batch_id = %s
        ORDER BY id
        """,
        (batch_id,),
    ).fetchall()


def _batch_payload(conn, batch_id: int, status: str) -> dict:
    batch = conn.execute(
        "SELECT batch_no, created_at FROM receipt_batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    receipts = [
        {
            "receipt_no": r["receipt_no"],
            "external_ref": r["external_ref"],
            "holder_code": r["holder_code"],
            "amount": int(r["amount"]),
            "is_duplicate": bool(r["is_duplicate"]),
        }
        for r in _receipt_rows(conn, batch_id)
    ]
    return {
        "batch_no": batch["batch_no"],
        "status": status,
        "created_at": batch["created_at"],
        "receipts": receipts,
        # 导入时冻结的第 1 版结论（重试时同样返回原批次的第 1 版）
        "verification": _version_payload(conn, batch_id, batch["batch_no"], 1),
    }


# ---- 导入 ----

def import_batch(conn, body) -> dict:
    lines = body.receipts

    # 批内重复：重复回单编号 / 重复流水号均整批拒绝（无效批次）
    receipt_nos = [l.receipt_no for l in lines]
    if len(set(receipt_nos)) != len(receipt_nos):
        raise APIError(
            422, "duplicate_receipt_no_in_batch",
            "duplicate receipt_no within batch; whole batch rejected",
        )
    refs = [l.external_ref for l in lines]
    if len(set(refs)) != len(refs):
        raise APIError(
            422, "duplicate_external_ref_in_batch",
            "duplicate external_ref within batch; whole batch rejected",
        )

    content_hash = _content_hash(lines)

    with conn.transaction():
        # 全局导入锁：串行化全部批次导入，使"首次成功导入"判定确定、
        # 批次号/回单编号的内容检查与插入原子完成，无竞态。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_IMPORT_NS, 1),),
        )

        existing = conn.execute(
            "SELECT id, content_hash FROM receipt_batches WHERE batch_no = %s",
            (body.batch_no,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise APIError(
                    409, "receipt_batch_conflict",
                    f"receipt batch {body.batch_no} already imported "
                    "with different content",
                    detail={"batch_no": body.batch_no},
                )
            # 同批次同内容重试：幂等返回原批次（含导入时冻结的第 1 版）
            return _batch_payload(conn, existing["id"], status="duplicate")

        # 回单编号全局内容绑定：异内容复用 => 整批拒绝
        conflict_rows = conn.execute(
            """
            SELECT receipt_no, external_ref, holder_code, amount
            FROM receipts
            WHERE receipt_no = ANY(%s)
            """,
            (receipt_nos,),
        ).fetchall()
        by_no = {r["receipt_no"]: r for r in conflict_rows}
        for l in lines:
            e = by_no.get(l.receipt_no)
            if e is not None and (
                e["external_ref"] != l.external_ref
                or e["holder_code"] != l.holder_code
                or int(e["amount"]) != l.amount
            ):
                raise APIError(
                    409, "receipt_no_conflict",
                    f"receipt {l.receipt_no} already imported "
                    "with different content; whole batch rejected",
                    detail={
                        "receipt_no": l.receipt_no,
                        "existing_external_ref": e["external_ref"],
                        "existing_holder_code": e["holder_code"],
                        "existing_amount": int(e["amount"]),
                    },
                )

        # 已成功导入的流水号：跨批次同号 => 后导入的回单标为重复回单，
        # 仅首次成功导入的回单参与核验。同内容复用回单编号时其流水号
        # 必然已存在，同样落入重复回单。
        ref_rows = conn.execute(
            "SELECT DISTINCT external_ref FROM receipts WHERE external_ref = ANY(%s)",
            (refs,),
        ).fetchall()
        seen_refs = {r["external_ref"] for r in ref_rows}

        batch = conn.execute(
            """
            INSERT INTO receipt_batches(batch_no, content_hash, line_count)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (body.batch_no, content_hash, len(lines)),
        ).fetchone()
        batch_id = batch["id"]

        with conn.cursor() as cur:
            for l in lines:
                is_dup = l.external_ref in seen_refs
                seen_refs.add(l.external_ref)
                cur.execute(
                    """
                    INSERT INTO receipts
                        (batch_id, receipt_no, external_ref, holder_code,
                         amount, is_duplicate)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (batch_id, l.receipt_no, l.external_ref, l.holder_code,
                     l.amount, is_dup),
                )

        # 导入时冻结逐行结论（第 1 版）
        _insert_version(conn, batch_id, 1, _compute_lines(conn, batch_id))
        return _batch_payload(conn, batch_id, status="created")


# ---- 复核 ----

def reverify(conn, batch_no: str) -> dict:
    """按当前已登记付款复核某批次：结论变化才生成新版本。"""
    with conn.transaction():
        batch = conn.execute(
            "SELECT id FROM receipt_batches WHERE batch_no = %s",
            (batch_no,),
        ).fetchone()
        if batch is None:
            raise APIError(
                404, "receipt_batch_not_found",
                f"receipt batch {batch_no} not found",
            )
        batch_id = batch["id"]

        # 每批次复核锁：同一批次并发复核串行化，不会产生重复版本
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_BATCH_NS, _hash_key(batch_no) & 0xFFFFFFFF),),
        )

        latest = conn.execute(
            """
            SELECT id, version FROM receipt_verifications
            WHERE batch_id = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (batch_id,),
        ).fetchone()

        # 单条 SQL 快照：付款与复核并发时，本版全部结论来自同一已提交状态
        new_lines = _compute_lines(conn, batch_id)
        old_rows = conn.execute(
            """
            SELECT receipt_id, status, holder_mismatch, amount_mismatch,
                   payment_holder_code, payment_amount, payment_refunded_total
            FROM receipt_verification_lines
            WHERE verification_id = %s
            """,
            (latest["id"],),
        ).fetchall()
        old_map = {}
        for r in old_rows:
            # 未登记付款/重复回单行的累计退回额在库中为列默认值 0，
            # 与新算结论的 None 归一化后再比较，避免凭空判定为结论变化。
            old_refunded = (
                int(r["payment_refunded_total"])
                if r["payment_holder_code"] is not None
                else None
            )
            old_map[r["receipt_id"]] = (
                r["status"],
                bool(r["holder_mismatch"]),
                bool(r["amount_mismatch"]),
                r["payment_holder_code"],
                r["payment_amount"],
                old_refunded,
            )
        new_map = {l["receipt_id"]: _conclusion_tuple(l) for l in new_lines}

        if old_map == new_map:
            # 结论不变：不新增版本，返回现有最新版
            return {
                "batch_no": batch_no,
                "status": "unchanged",
                "verification": _version_payload(
                    conn, batch_id, batch_no, latest["version"]
                ),
            }

        _insert_version(conn, batch_id, latest["version"] + 1, new_lines)
        return {
            "batch_no": batch_no,
            "status": "created",
            "verification": _version_payload(
                conn, batch_id, batch_no, latest["version"] + 1
            ),
        }


# ---- 查询 ----

def get_batch(conn, batch_no: str):
    """原始回单（始终可查）与最新版本号。"""
    batch = conn.execute(
        """
        SELECT id, created_at, line_count,
               (SELECT MAX(v.version) FROM receipt_verifications v
                WHERE v.batch_id = b.id) AS latest_version
        FROM receipt_batches b
        WHERE batch_no = %s
        """,
        (batch_no,),
    ).fetchone()
    if batch is None:
        return None
    receipts = [
        {
            "receipt_no": r["receipt_no"],
            "external_ref": r["external_ref"],
            "holder_code": r["holder_code"],
            "amount": int(r["amount"]),
            "is_duplicate": bool(r["is_duplicate"]),
        }
        for r in _receipt_rows(conn, batch["id"])
    ]
    return {
        "batch_no": batch_no,
        "created_at": batch["created_at"],
        "line_count": batch["line_count"],
        "latest_version": batch["latest_version"],
        "receipts": receipts,
    }


def list_versions(conn, batch_no: str):
    """列出一批次的全部核验版本（旧版本始终可查）。"""
    batch = conn.execute(
        "SELECT id FROM receipt_batches WHERE batch_no = %s",
        (batch_no,),
    ).fetchone()
    if batch is None:
        return None
    rows = conn.execute(
        """
        SELECT v.version, v.created_at, l.status, COUNT(*) AS n
        FROM receipt_verifications v
        JOIN receipt_verification_lines l ON l.verification_id = v.id
        WHERE v.batch_id = %s
        GROUP BY v.version, v.created_at, l.status
        ORDER BY v.version
        """,
        (batch["id"],),
    ).fetchall()
    versions: dict[int, dict] = {}
    for r in rows:
        entry = versions.setdefault(
            r["version"],
            {
                "version": r["version"],
                "created_at": r["created_at"],
                "counts": {s: 0 for s in _STATUSES},
            },
        )
        entry["counts"][r["status"]] = int(r["n"])
    result = []
    for version in sorted(versions):
        entry = versions[version]
        entry["counts"]["total"] = sum(entry["counts"][s] for s in _STATUSES)
        result.append(entry)
    return {"batch_no": batch_no, "versions": result}


def get_version(conn, batch_no: str, version: int):
    """查询某一批次的指定核验版本（状态计数 + 差异行）。"""
    batch = conn.execute(
        "SELECT id FROM receipt_batches WHERE batch_no = %s",
        (batch_no,),
    ).fetchone()
    if batch is None:
        return None, None
    exists = conn.execute(
        """
        SELECT 1 FROM receipt_verifications
        WHERE batch_id = %s AND version = %s
        """,
        (batch["id"], version),
    ).fetchone()
    if exists is None:
        return batch, None
    return batch, _version_payload(conn, batch["id"], batch_no, version)
