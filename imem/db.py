"""连接与初始化（含轻量迁移）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 每次改表结构就 +1，并在 _migrate() 里补上对应的 ALTER。
SCHEMA_VERSION = 2


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：只补缺失的列，不动已有数据。

    注：schema.sql 里的 CHECK 约束只对新库生效；老库补列时由代码层校验。
    """
    cols = _columns(conn, "message")
    if "sent_by" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN sent_by TEXT NOT NULL DEFAULT 'unknown'")
    if "outbox_id" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN outbox_id INTEGER")

    conn.execute(
        "INSERT INTO meta(k, v) VALUES('schema_version', ?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),))


def schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    return int(row["v"]) if row else None
