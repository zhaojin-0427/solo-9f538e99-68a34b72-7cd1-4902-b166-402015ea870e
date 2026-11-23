"""SQLite 持久化：批次、票款、分组合计、确认记录。"""
from __future__ import annotations

import json
import sqlite3
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    entry_count     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    group_type      TEXT NOT NULL CHECK (group_type IN ('window', 'date')),
    group_key       TEXT NOT NULL,
    total           INTEGER NOT NULL,
    UNIQUE (batch_id, group_type, group_key)
);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    position        INTEGER NOT NULL,
    ticket_no       TEXT NOT NULL,
    window_key      TEXT NOT NULL,
    sale_date       TEXT NOT NULL,
    candidates      TEXT NOT NULL,
    confirmed_amount INTEGER,
    feasible        TEXT NOT NULL,
    UNIQUE (batch_id, position)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    entry_id        INTEGER NOT NULL REFERENCES entries(id),
    amount          INTEGER NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('confirm', 'revoke')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # 写-写冲突时等待而非立即报 database is locked；
        # 配合 WAL，导出 CSV 的只读快照不会阻塞其他复核员提交确认/撤销
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    # WAL 模式：读快照（导出入仓 CSV）与写入（确认/撤销）互不阻塞。
    # 该设置持久化在库文件中，对已有数据卷同样生效，无需迁移数据。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()


def latest_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM batches").fetchone()
    return row["id"] if row and row["id"] is not None else None


def batch_state_stamp(conn: sqlite3.Connection, batch_id: int) -> int:
    """批次确认状态戳：本批 confirmations 审计表的最大行 id。

    审计行只增不删，任何一次确认/撤销成功提交都会插入新行使该值增大，
    即使之后把金额改回原值，状态戳也不会回到旧值。
    """
    row = conn.execute(
        "SELECT MAX(id) AS m FROM confirmations WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return row["m"] if row and row["m"] is not None else 0


def load_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()


def load_entries(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE batch_id = ? ORDER BY position", (batch_id,)
    ).fetchall()


def load_groups(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM groups WHERE batch_id = ? ORDER BY group_type, group_key",
        (batch_id,),
    ).fetchall()


def row_candidates(row: sqlite3.Row) -> tuple[int, ...]:
    return tuple(json.loads(row["candidates"]))


def row_feasible(row: sqlite3.Row) -> tuple[int, ...]:
    return tuple(json.loads(row["feasible"]))
