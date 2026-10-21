-- IM-Agent-Memory schema v1
-- 设计原则：
--   1. observation 不可变，是唯一真相（agent 自己写入）
--   2. message 是去重后的原子事件，带 anchor_hash 防重
--   3. conversation.key / entity.key 一律 app-agnostic，加新 IM 不改表
--   4. 每张表都允许"我不知道"——NULL 是合法状态，不是错误

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 观察
CREATE TABLE IF NOT EXISTS observation (
    id            INTEGER PRIMARY KEY,
    app           TEXT    NOT NULL,
    window_title  TEXT,
    captured_at   TEXT    NOT NULL,          -- ISO8601
    captured_epoch INTEGER NOT NULL,
    image_ref     TEXT,                      -- 截图路径 / 哈希
    note          TEXT,
    created_at    TEXT    NOT NULL
);

-- ---------------------------------------------------------------- 会话（scope）
CREATE TABLE IF NOT EXISTS conversation (
    key               TEXT PRIMARY KEY,      -- 'wechat:12345@chatroom' 之类
    app               TEXT    NOT NULL,
    app_chat_key      TEXT,                  -- app 内部标识，可空
    title             TEXT    NOT NULL,
    kind              TEXT    NOT NULL DEFAULT 'unknown'
                              CHECK (kind IN ('group','dm','channel','unknown')),
    member_count      INTEGER,
    first_seen_at     TEXT,
    last_seen_at      TEXT,
    last_deep_read_at TEXT,                  -- 上次深度抓取（滚动）
    note              TEXT
);
CREATE INDEX IF NOT EXISTS ix_conv_app ON conversation(app);

-- ---------------------------------------------------------------- 实体（人/群/机器人/我）
CREATE TABLE IF NOT EXISTS entity (
    key          TEXT PRIMARY KEY,           -- 'person:老王' 或 'self:wechat'
    kind         TEXT NOT NULL DEFAULT 'person'
                      CHECK (kind IN ('person','group','bot','self','unknown')),
    display_name TEXT NOT NULL,
    confirmed    INTEGER NOT NULL DEFAULT 0, -- 1 = 人工确认过
    created_at   TEXT NOT NULL
);

-- 别名：群昵称是 per-scope 的，同一个人的不同群昵称必须分开映射
CREATE TABLE IF NOT EXISTS entity_alias (
    id         INTEGER PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES entity(key) ON DELETE CASCADE,
    alias      TEXT NOT NULL,                -- 界面原文
    alias_norm TEXT NOT NULL,                -- 归一化后用于匹配
    app        TEXT NOT NULL DEFAULT '',     -- '' = 不限 app
    scope_key  TEXT NOT NULL DEFAULT '',     -- '' = 全局（私聊/通讯录）
    source     TEXT NOT NULL DEFAULT 'inferred'
                    CHECK (source IN ('manual','inferred','imported')),
    created_at TEXT NOT NULL,
    UNIQUE (alias_norm, scope_key, app)
);
CREATE INDEX IF NOT EXISTS ix_alias_entity ON entity_alias(entity_key);

-- ---------------------------------------------------------------- 消息
CREATE TABLE IF NOT EXISTS message (
    id                    INTEGER PRIMARY KEY,
    conversation_key      TEXT    NOT NULL REFERENCES conversation(key) ON DELETE CASCADE,
    sender_entity_key     TEXT    REFERENCES entity(key),  -- NULL = 还没消歧
    sender_label          TEXT    NOT NULL,                -- 界面昵称原文，永远保留
    sender_norm           TEXT    NOT NULL,
    text                  TEXT    NOT NULL,
    ts_est                TEXT    NOT NULL,                -- ISO8601（估算）
    ts_epoch              INTEGER NOT NULL,
    ts_conf               TEXT    NOT NULL DEFAULT 'minute'
                                  CHECK (ts_conf IN ('exact','minute','inferred')),
    msg_type              TEXT    NOT NULL DEFAULT 'text'
                                  CHECK (msg_type IN ('text','image','file','system',
                                                      'sticker','quote','redpacket','other')),
    reply_to_id           INTEGER REFERENCES message(id),
    -- 去重锚：同一会话内 (anchor_hash, ordinal) 唯一
    anchor_hash           TEXT    NOT NULL,
    ordinal               INTEGER NOT NULL DEFAULT 0,
    client_ref            TEXT,                            -- agent 提供的幂等键（推荐）
    state                 TEXT    NOT NULL DEFAULT 'observed'
                                  CHECK (state IN ('observed','confirmed','vanished')),
    -- 检索用的派生列（bigram / unigram）
    fts_bi                TEXT    NOT NULL DEFAULT '',
    fts_uni               TEXT    NOT NULL DEFAULT '',
    source_observation_id INTEGER REFERENCES observation(id),
    created_at            TEXT    NOT NULL,
    UNIQUE (conversation_key, anchor_hash, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_msg_conv_ts ON message(conversation_key, ts_epoch);
CREATE INDEX IF NOT EXISTS ix_msg_sender  ON message(sender_entity_key);
CREATE INDEX IF NOT EXISTS ix_msg_state   ON message(state);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_client_ref
    ON message(conversation_key, client_ref) WHERE client_ref IS NOT NULL;

-- ---------------------------------------------------------------- 全文索引
-- 双列：bi=bigram（≥2 字查询），uni=unigram（单字查询）
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    fts_bi, fts_uni,
    content='message', content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_msg_ai AFTER INSERT ON message BEGIN
    INSERT INTO message_fts(rowid, fts_bi, fts_uni)
    VALUES (new.id, new.fts_bi, new.fts_uni);
END;
CREATE TRIGGER IF NOT EXISTS trg_msg_ad AFTER DELETE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, fts_bi, fts_uni)
    VALUES ('delete', old.id, old.fts_bi, old.fts_uni);
END;
CREATE TRIGGER IF NOT EXISTS trg_msg_au AFTER UPDATE OF fts_bi, fts_uni ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, fts_bi, fts_uni)
    VALUES ('delete', old.id, old.fts_bi, old.fts_uni);
    INSERT INTO message_fts(rowid, fts_bi, fts_uni)
    VALUES (new.id, new.fts_bi, new.fts_uni);
END;

-- ---------------------------------------------------------------- 向量（可选）
CREATE TABLE IF NOT EXISTS message_vec (
    message_id INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
    dim        INTEGER NOT NULL,
    vec        BLOB    NOT NULL,   -- float32 小端数组
    model      TEXT
);

-- ---------------------------------------------------------------- 元信息
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
