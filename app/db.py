import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发收口时让后来者等待而不是立刻抛锁
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_tx(db_path: str | None = None):
    """立即取得写锁的事务：收口全流程在同一事务内一次做完。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stamp_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watermark_value TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('半截','已收口')),
  total_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL DEFAULT 0,
  strategy TEXT CHECK (strategy IS NULL OR strategy IN ('rollback','retain')),
  strategy_label TEXT,
  closed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  job_id INTEGER REFERENCES stamp_jobs(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_batches)").fetchall()}
        if "job_id" not in cols:
            conn.execute("ALTER TABLE run_batches ADD COLUMN job_id INTEGER")
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for table, code, biz, status, wm in SEED_BATCHES:
            conn.execute(
                "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    table,
                    code,
                    biz,
                    status,
                    wm,
                    "2026-11-13 18:00:00" if status == "已盖印" else None,
                ),
            )
        for up, down in SEED_EDGES:
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                (up, down),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
