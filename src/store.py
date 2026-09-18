"""SQLite 持久化层：schema、连接与公共工具。

数据库文件默认写入 .runtime/planner.db（见 README）。金额列一律为
微单位整数；时间列同时保留业务时间（如 occurred_on / value_date）与
接收时间（created_at / imported_at），对应 docs/domain.md 的约定。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_income_micros INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY',
    local_tz TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    bank_tz TEXT NOT NULL DEFAULT 'UTC',
    transfer_time TEXT NOT NULL DEFAULT '21:00',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixed_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    amount_micros INTEGER NOT NULL,
    day_of_month INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_type TEXT NOT NULL,
    name TEXT NOT NULL,
    target_micros INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    deadline TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'active',
    eta_date TEXT,
    eta_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deductions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    amount_micros INTEGER NOT NULL,
    scheduled_date TEXT NOT NULL,
    value_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    recovery_condition TEXT,
    created_version INTEGER,
    updated_version INTEGER,
    executed_amount_micros INTEGER,
    executed_at TEXT,
    bank_txn_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deductions_goal ON deductions(goal_id, status);
CREATE INDEX IF NOT EXISTS idx_deductions_value_date ON deductions(value_date);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    occurred_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    plan_version_id INTEGER
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id INTEGER PRIMARY KEY,
    event_id INTEGER,
    reason TEXT NOT NULL,
    changes TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    external_id TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    value_date TEXT NOT NULL,
    amount_micros INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    goal_id INTEGER,
    deduction_id INTEGER,
    memo TEXT,
    import_batch TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (account, external_id)
);

CREATE TABLE IF NOT EXISTS training_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    goal_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    """接收时间戳（UTC，秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT OR IGNORE INTO profile (id, updated_at) VALUES (1, ?)", (utcnow(),))
        conn.commit()
    finally:
        conn.close()
