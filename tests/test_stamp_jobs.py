from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import Config
from app.db import connect
from app.services.jobs import close_stamp_job, create_interrupted_job


def _pending_ids(items):
    return [b["id"] for b in items if b["status"] == "待盖印"]


@pytest.fixture()
def job_pair(authed):
    async def _make():
        items = (await (await authed.get("/api/batches")).json())["items"]
        ids = _pending_ids(items)
        assert len(ids) >= 2
        r = await authed.post(
            "/api/stamp-jobs",
            json={"batch_ids": ids[:2], "watermark_value": "WM-HALF-01"},
        )
        assert r.status == 200
        body = await r.json()
        return ids, body["job"]

    return _make


@pytest.mark.asyncio
async def test_create_interrupted_job_half_state(authed, job_pair):
    ids, job = await job_pair()
    assert job["status"] == "半截"
    assert job["total_count"] == 2
    assert job["stamped_count"] == 1
    assert job["strategy"] is None
    assert job["progress_text"] == "1/2"
    statuses = {b["run_code"]: b["status"] for b in job["batches"]}
    assert set(statuses.values()) == {"待盖印", "已盖印"}

    detail = await authed.get(f"/api/stamp-jobs/{job['id']}")
    assert detail.status == 200
    assert (await detail.json())["job"]["status"] == "半截"


@pytest.mark.asyncio
async def test_rollback_strategy_clears_all_marks(authed, job_pair):
    _, job = await job_pair()
    # 先复跑对账，让收口前核对有基准
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})

    r = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close",
        json={"strategy": "rollback"},
    )
    assert r.status == 200
    closed = (await r.json())["job"]
    assert closed["status"] == "已收口"
    assert closed["strategy"] == "rollback"
    assert closed["strategy_label"] == "整笔回滚"
    assert all(b["status"] == "待盖印" for b in closed["batches"])
    assert all(b["watermark_value"] is None for b in closed["batches"])


@pytest.mark.asyncio
async def test_retain_strategy_fills_remaining_with_same_watermark(authed, job_pair):
    _, job = await job_pair()
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})

    r = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close",
        json={"strategy": "retain"},
    )
    assert r.status == 200
    closed = (await r.json())["job"]
    assert closed["strategy"] == "retain"
    assert closed["strategy_label"] == "部分留存"
    assert closed["stamped_count"] == 2
    assert all(b["status"] == "已盖印" for b in closed["batches"])
    assert all(b["watermark_value"] == "WM-HALF-01" for b in closed["batches"])


@pytest.mark.asyncio
async def test_closed_job_cannot_close_again(authed, job_pair):
    _, job = await job_pair()
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    first = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "rollback"}
    )
    assert first.status == 200
    again = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "retain"}
    )
    assert again.status == 409
    assert "收口" in (await again.json())["error"]


@pytest.mark.asyncio
async def test_invalid_strategy_rejected_in_chinese(authed, job_pair):
    _, job = await job_pair()
    r = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "nuke"}
    )
    assert r.status == 400
    err = (await r.json())["error"]
    assert "回滚" in err and "留存" in err


@pytest.mark.asyncio
async def test_consistency_drift_blocks_close(authed, job_pair):
    _, job = await job_pair()
    # 复跑对账落一条基准
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    # 外部篡改对账结果，制造批次态与缺口数不齐
    with connect(Config.database()) as conn:
        conn.execute("UPDATE gap_reports SET gap_count = gap_count + 9")
        conn.commit()

    r = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "retain"}
    )
    assert r.status == 400
    assert "核对失败" in (await r.json())["error"]

    # 停手后作业仍是半截，已落印段没被动过
    detail = await authed.get(f"/api/stamp-jobs/{job['id']}")
    fresh = (await detail.json())["job"]
    assert fresh["status"] == "半截"
    assert sum(1 for b in fresh["batches"] if b["status"] == "已盖印") == 1


@pytest.mark.asyncio
async def test_gap_rerun_reflects_then_closes_each_path_once(authed, job_pair):
    ids, job = await job_pair()
    before = await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )
    before_body = (await before.json())["report"]
    assert any(j["id"] == job["id"] for j in before_body["open_jobs"])
    assert before_body["consistent"] is True
    gap_half = before_body["gap_count"]

    # 留存补盖：缺口应下降一个（待盖印被补齐）
    await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "retain"}
    )
    after = await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16"}
    )
    after_body = (await after.json())["report"]
    assert after_body["gap_count"] == gap_half - 1
    assert all(j["id"] != job["id"] for j in after_body["open_jobs"])


@pytest.mark.asyncio
async def test_rollback_keeps_gap_count_stable(authed):
    items = (await (await authed.get("/api/batches")).json())["items"]
    ids = _pending_ids(items)[:2]
    made = await authed.post(
        "/api/stamp-jobs",
        json={"batch_ids": ids, "watermark_value": "WM-HALF-02"},
    )
    job = (await made.json())["job"]
    report = (
        await (
            await authed.post(
                "/api/gap-reports", json={"as_of_date": "2026-11-16"}
            )
        ).json()
    )["report"]
    gap_when_half = report["gap_count"]

    await authed.post(
        f"/api/stamp-jobs/{job['id']}/close", json={"strategy": "回滚"}
    )
    rerun = (
        await (
            await authed.post(
                "/api/gap-reports", json={"as_of_date": "2026-11-16"}
            )
        ).json()
    )["report"]
    # 回滚把已落印的那一个也清回待盖印，缺口回到作业创建前
    assert rerun["gap_count"] == gap_when_half + 1


def test_concurrent_close_only_one_wins(tmp_path):
    db = str(tmp_path / "concurrent.db")
    from app.db import init_db

    init_db(db)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT id FROM run_batches WHERE status='待盖印' ORDER BY id LIMIT 2"
        ).fetchall()
        ids = [r["id"] for r in rows]
    job = create_interrupted_job(ids, "WM-CONC-01", db_path=db)

    outcomes = []

    def worker(strategy):
        try:
            close_stamp_job(job["id"], strategy, db_path=db)
            return ("ok", strategy)
        except Exception as exc:  # noqa: BLE001
            return ("err", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, "retain"),
            pool.submit(worker, "rollback"),
        ]
        outcomes = [f.result() for f in futures]

    assert sorted(outcomes, key=lambda x: x[0])[0][0] == "err"
    winners = [o for o in outcomes if o[0] == "ok"]
    assert len(winners) == 1

    with connect(db) as conn:
        row = conn.execute(
            "SELECT * FROM stamp_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        assert row["status"] == "已收口"
        # 库里只允许一种命运
        assert row["strategy"] == winners[0][1]
        marks = conn.execute(
            "SELECT DISTINCT watermark_value FROM run_batches WHERE job_id=?",
            (job["id"],),
        ).fetchall()
        if row["strategy"] == "retain":
            assert len(marks) == 1 and marks[0]["watermark_value"] == "WM-CONC-01"
        else:
            assert all(m["watermark_value"] is None for m in marks)


def test_job_marker_persisted_and_queryable(tmp_path):
    db = str(tmp_path / "marker.db")
    from app.db import init_db

    init_db(db)
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT id FROM run_batches WHERE status='待盖印' ORDER BY id LIMIT 3"
        ).fetchall()
        ids = [r["id"] for r in rows]
    job = create_interrupted_job(ids, "WM-MARK-01", db_path=db)
    close_stamp_job(job["id"], "retain", db_path=db)

    with connect(db) as conn:
        row = conn.execute(
            "SELECT status, strategy, strategy_label, closed_at, stamped_count "
            "FROM stamp_jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
        assert row["status"] == "已收口"
        assert row["strategy"] == "retain"
        assert row["strategy_label"] == "部分留存"
        assert row["closed_at"]
        assert row["stamped_count"] == 3
        linked = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE job_id=? AND status='已盖印'",
            (job["id"],),
        ).fetchone()["c"]
        assert linked == 3


@pytest.mark.asyncio
async def test_create_job_requires_two_pending_batches(authed):
    items = (await (await authed.get("/api/batches")).json())["items"]
    one = _pending_ids(items)[:1]
    r = await authed.post(
        "/api/stamp-jobs",
        json={"batch_ids": one, "watermark_value": "WM-X"},
    )
    assert r.status == 400
    assert "两个" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_retain_rejects_mismatched_watermark(authed, job_pair):
    _, job = await job_pair()
    await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    r = await authed.post(
        f"/api/stamp-jobs/{job['id']}/close",
        json={"strategy": "retain", "watermark_value": "WM-OTHER-999"},
    )
    assert r.status == 400
    assert "水印" in (await r.json())["error"]
