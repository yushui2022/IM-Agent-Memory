"""连接与初始化（含轻量迁移）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 每次改表结构就 +1，并在 _migrate() 里补上对应处理。
#   v2  message.sent_by / outbox_id（归因 + 回读）
#   v3  outbox.source_message_id（盖章幂等）
#   v4  outbox.escalated 状态 + escalate_reason / auto_approved（自治 + 升级人工）
SCHEMA_VERSION = 4

_OUTBOX_COLUMNS = (
    "id", "conversation_key", "text", "text_norm", "state", "risk", "block_reason",
    "escalate_reason", "auto_approved", "created_at", "approved_at", "sent_at",
    "verified_at", "attempts", "message_id", "source_message_id", "note",
)

_OUTBOX_DDL = """
CREATE TABLE {name} (
    id               INTEGER PRIMARY KEY,
    conversation_key TEXT    NOT NULL REFERENCES conversation(key) ON DELETE CASCADE,
    text             TEXT    NOT NULL,
    text_norm        TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'draft'
                             CHECK (state IN ('draft','approved','sent',
                                              'verified','failed','cancelled','escalated')),
    risk             TEXT    NOT NULL DEFAULT 'normal'
                             CHECK (risk IN ('normal','blocked')),
    block_reason     TEXT,
    escalate_reason  TEXT,
    auto_approved    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    approved_at      TEXT,
    sent_at          TEXT,
    verified_at      TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    message_id       INTEGER REFERENCES message(id),
    source_message_id INTEGER REFERENCES message(id),
    note             TEXT
)
"""


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
    """幂等迁移：只补缺失的列 / 必要时重建表，不动已有数据。"""
    # --- message：归因与回读链接
    cols = _columns(conn, "message")
    if "sent_by" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN sent_by TEXT NOT NULL DEFAULT 'unknown'")
    if "outbox_id" not in cols:
        conn.execute("ALTER TABLE message ADD COLUMN outbox_id INTEGER")

    # --- outbox：先看 CHECK 是否已允许 'escalated'（SQLite 改不了 CHECK，只能重建表）
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone()
    if row is not None and "escalated" not in (row["sql"] or ""):
        _rebuild_outbox(conn)

    ocols = _columns(conn, "outbox")
    if "source_message_id" not in ocols:
        conn.execute("ALTER TABLE outbox ADD COLUMN source_message_id INTEGER")
    if "escalate_reason" not in ocols:
        conn.execute("ALTER TABLE outbox ADD COLUMN escalate_reason TEXT")
    if "auto_approved" not in ocols:
        conn.execute("ALTER TABLE outbox ADD COLUMN auto_approved INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        "INSERT INTO meta(k, v) VALUES('schema_version', ?)"
        " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),))


def _rebuild_outbox(conn: sqlite3.Connection) -> None:
    """重建 outbox 以升级 CHECK 约束，保留全部数据。

    SQLite 不支持 ALTER CHECK，只能 建新表 → 拷数据 → 删旧表 → 改名。
    过程中临时关掉外键检查，避免 message.outbox_id 的引用把 DROP 卡住。
    """
    old_cols = _columns(conn, "outbox")
    keep = [c for c in _OUTBOX_COLUMNS if c in old_cols]
    select_list = ", ".join(keep)

    # PRAGMA foreign_keys 在事务内是无效操作，且 BEGIN 不能嵌套 → 先收尾再开
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(_OUTBOX_DDL.format(name="outbox_new"))
        conn.execute(f"INSERT INTO outbox_new({select_list}) SELECT {select_list} FROM outbox")
        conn.execute("DROP TABLE outbox")
        conn.execute("ALTER TABLE outbox_new RENAME TO outbox")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_outbox_conv_state"
                     " ON outbox(conversation_key, state)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_outbox_state ON outbox(state)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    return int(row["v"]) if row else None
