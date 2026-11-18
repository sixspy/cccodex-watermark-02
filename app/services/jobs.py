import sqlite3
import threading

from app.db import get_db, immediate_tx
from app.services.errors import ServiceError

# 同一进程内两名仓管同时点“继续盖印”时的进程级排他锁
_job_lock = threading.Lock()

STRATEGY_LABELS = {"rollback": "整笔回滚", "retain": "部分留存"}
_STRATEGY_ALIASES = {
    "rollback": "rollback",
    "回滚": "rollback",
    "整笔回滚": "rollback",
    "retain": "retain",
    "留存": "retain",
    "部分留存": "retain",
}


def _normalize_strategy(strategy: str) -> str:
    key = (strategy or "").strip()
    normalized = _STRATEGY_ALIASES.get(key)
    if not normalized:
        raise ServiceError("收口策略只支持整笔回滚(rollback)或部分留存(retain)")
    return normalized


def _load_job(conn, job_id: int) -> sqlite3.Row:
    job = conn.execute("SELECT * FROM stamp_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ServiceError("盖印作业不存在", 404)
    return job


def _job_batches(conn, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM run_batches WHERE job_id=? ORDER BY id", (job_id,)
    ).fetchall()


def _serialize_job(conn, job: sqlite3.Row, with_batches: bool = True) -> dict:
    data = dict(job)
    data["strategy_label"] = STRATEGY_LABELS.get(job["strategy"]) if job["strategy"] else None
    batches = _job_batches(conn, job["id"]) if with_batches else []
    stamped = sum(1 for b in batches if b["status"] == "已盖印")
    pending = sum(1 for b in batches if b["status"] == "待盖印")
    data["stamped_count_live"] = stamped
    data["pending_count_live"] = pending
    data["progress_text"] = f"{stamped}/{job['total_count']}"
    if with_batches:
        data["batches"] = [dict(b) for b in batches]
    return data


def list_jobs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        jobs = conn.execute("SELECT * FROM stamp_jobs ORDER BY id").fetchall()
        return [_serialize_job(conn, j) for j in jobs]


def get_job(job_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        job = conn.execute("SELECT * FROM stamp_jobs WHERE id=?", (job_id,)).fetchone()
        return _serialize_job(conn, job) if job else None


def create_interrupted_job(
    batch_ids: list[int],
    watermark_value: str,
    interrupt_after: int | None = None,
    db_path: str | None = None,
) -> dict:
    """模拟断流：创建一个盖印作业，只给前 N 个运行码落印，其余保持待盖印。"""
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    ids = list(dict.fromkeys(int(i) for i in (batch_ids or [])))
    if len(ids) < 2:
        raise ServiceError("至少选择两个运行码才能模拟半截盖印")

    with immediate_tx(db_path) as conn:
        batches = []
        for bid in ids:
            row = conn.execute(
                "SELECT * FROM run_batches WHERE id=?", (bid,)
            ).fetchone()
            if not row:
                raise ServiceError(f"批次 {bid} 不存在", 404)
            if row["status"] != "待盖印":
                raise ServiceError(f"运行码 {row['run_code']} 不是待盖印状态", 409)
            if row["job_id"] is not None:
                raise ServiceError(f"运行码 {row['run_code']} 已挂在其他盖印作业上", 409)
            tpl = conn.execute(
                "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
                (row["table_name"],),
            ).fetchone()
            if not tpl:
                raise ServiceError(f"运行码 {row['run_code']} 无现行水印模板", 409)
            batches.append(row)

        cut = len(batches) // 2 if interrupt_after is None else int(interrupt_after)
        if cut < 1 or cut >= len(batches):
            raise ServiceError("断流点必须落在首批与末批之间，至少留一个待盖印码")

        cur = conn.execute(
            "INSERT INTO stamp_jobs(watermark_value, status, total_count, stamped_count) "
            "VALUES (?,'半截',?,?)",
            (value, len(batches), cut),
        )
        job_id = cur.lastrowid
        for index, row in enumerate(batches):
            if index < cut:
                conn.execute(
                    "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                    "stamped_at=datetime('now'), job_id=? WHERE id=? AND status='待盖印'",
                    (value, job_id, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE run_batches SET job_id=? WHERE id=? AND status='待盖印'",
                    (job_id, row["id"]),
                )
        job = _load_job(conn, job_id)
        return _serialize_job(conn, job)


def _live_totals(conn) -> tuple[int, int, int]:
    expected = conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches WHERE status!='已作废'"
    ).fetchone()["c"]
    stamped = conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches WHERE status='已盖印'"
    ).fetchone()["c"]
    return expected, stamped, expected - stamped


def _assert_matches_latest_report(conn, stage: str) -> None:
    """收口前后核对：批次态算出的缺口数必须与最近一次对账一致，偏了就停。"""
    report = conn.execute(
        "SELECT * FROM gap_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not report:
        return
    expected, stamped, gap = _live_totals(conn)
    if expected != report["expected_count"]:
        raise ServiceError(
            f"{stage}核对失败：在库应盖印 {expected} 与最近对账 {report['expected_count']} 不一致，已停手"
        )
    if stamped != report["stamped_count"]:
        raise ServiceError(
            f"{stage}核对失败：在库已盖印 {stamped} 与最近对账 {report['stamped_count']} 不一致，已停手"
        )
    if gap != report["gap_count"]:
        raise ServiceError(
            f"{stage}核对失败：在库缺口 {gap} 与最近对账 {report['gap_count']} 不一致，已停手"
        )


def close_stamp_job(
    job_id: int,
    strategy: str,
    watermark_value: str | None = None,
    db_path: str | None = None,
) -> dict:
    """半截收口：按唯一取舍一次做完。rollback 整笔回滚，retain 留存并补盖。"""
    normalized = _normalize_strategy(strategy)

    if not _job_lock.acquire(blocking=False):
        raise ServiceError("已有仓管正在收口该批盖印，请勿重复点击", 409)
    try:
        try:
            with immediate_tx(db_path) as conn:
                job = _load_job(conn, job_id)
                if job["status"] != "半截":
                    raise ServiceError(
                        f"作业已按「{STRATEGY_LABELS.get(job['strategy'], job['strategy'])}」收口，不能重复处理",
                        409,
                    )
                # 收口前核对：批次态与缺口数要和最近一次对账核齐
                _assert_matches_latest_report(conn, "收口前")

                batches = _job_batches(conn, job_id)
                if normalized == "rollback":
                    # 整笔回滚：已落印段全部清回待盖印，不留半截命运
                    conn.execute(
                        "UPDATE run_batches SET status='待盖印', watermark_value=NULL, "
                        "stamped_at=NULL WHERE job_id=? AND status='已盖印'",
                        (job_id,),
                    )
                    final_stamped = 0
                else:
                    # 部分留存：已落印段保留，对剩余待盖印码补齐同一水印
                    override = (watermark_value or "").strip()
                    value = override or job["watermark_value"]
                    if override and override != job["watermark_value"]:
                        raise ServiceError("补盖水印值必须与作业原水印一致")
                    cur = conn.execute(
                        "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                        "stamped_at=datetime('now') WHERE job_id=? AND status='待盖印'",
                        (value, job_id),
                    )
                    if cur.rowcount != sum(1 for b in batches if b["status"] == "待盖印"):
                        raise ServiceError("补盖行数与待盖印数不符，已整笔回滚")
                    final_stamped = len(batches)

                conn.execute(
                    "UPDATE stamp_jobs SET status='已收口', strategy=?, "
                    "strategy_label=?, stamped_count=?, closed_at=datetime('now') WHERE id=?",
                    (
                        normalized,
                        STRATEGY_LABELS[normalized],
                        final_stamped,
                        job_id,
                    ),
                )

                # 收口后核对：作业内计数、批次态、缺口数三者核齐
                refreshed = _job_batches(conn, job_id)
                live_stamped = sum(1 for b in refreshed if b["status"] == "已盖印")
                live_pending = sum(1 for b in refreshed if b["status"] == "待盖印")
                if live_stamped != final_stamped or (
                    normalized == "retain" and live_pending != 0
                ) or (normalized == "rollback" and live_stamped != 0):
                    raise ServiceError("收口后批次态与作业计数核不齐，已整笔回滚")
                expected, stamped, gap = _live_totals(conn)
                if expected != stamped + gap:
                    raise ServiceError("收口后缺口数与批次态核不齐，已整笔回滚")

                job = _load_job(conn, job_id)
                return _serialize_job(conn, job)
        except sqlite3.OperationalError as exc:  # 跨进程撞锁兜底
            if "locked" in str(exc).lower():
                raise ServiceError("该盖印作业正被其他仓管占用，请稍后再试", 409)
            raise
    finally:
        _job_lock.release()
