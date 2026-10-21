"""Store：agent 唯一的读写入口。

写入契约（agent 侧只需要产这四类东西）：
    store.add_observation(app, captured_at, window_title=None, image_ref=None)
    store.ensure_conversation(app, title, kind='group', app_chat_key=None)
    store.ingest(conversation_key, messages, observation_id=None, captured_at=None)
    store.bind_alias(entity_key, alias, scope_key=None)   # 消歧是渐进的人工回路

message 每条的字段（昵称/正文/时间戳是核心三项，其余可缺省）：
    sender        必填  界面上的昵称原文
    text          必填  正文（图片/表情也放占位文字）
    ts            建议  ISO/epoch；不给则退化为观测时刻并把置信度降为 inferred
    msg_type      可选  text|image|file|system|sticker|quote|redpacket|other
    reply_to_text 可选  被引用内容（用于后续关联）
    client_ref    可选  幂等键，强烈建议
    new           可选  True 表示 agent 确认这是一条新消息（绕过去重）
"""
from __future__ import annotations

import json
import math
import sqlite3
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import db as dbmod
from . import text as T


class Store:
    def __init__(self, db_path: str = ":memory:", *, embedder: Callable | None = None,
                 embed_model: str | None = None) -> None:
        self.conn = dbmod.connect(db_path)
        dbmod.init_schema(self.conn)
        self._embedder = embedder
        self._embed_model = embed_model

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ============================================================== 写入
    def add_observation(self, app: str, captured_at=None, window_title: str | None = None,
                        image_ref: str | None = None, note: str | None = None) -> int:
        iso, epoch, _ = T.parse_ts(captured_at)
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO observation(app, window_title, captured_at, captured_epoch,"
                " image_ref, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (app, window_title, iso, epoch, image_ref, note, T.now_iso()))
            return int(cur.lastrowid)

    def ensure_conversation(self, app: str, title: str, kind: str = "group",
                            app_chat_key: str | None = None,
                            member_count: int | None = None) -> str:
        key = f"{app}:{app_chat_key}" if app_chat_key else f"{app}:{T.normalize(title)}"
        now = T.now_iso()
        with self._tx():
            self.conn.execute(
                "INSERT INTO conversation(key, app, app_chat_key, title, kind, member_count,"
                " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "   title=excluded.title,"
                "   kind=excluded.kind,"
                "   member_count=COALESCE(excluded.member_count, conversation.member_count),"
                "   last_seen_at=excluded.last_seen_at",
                (key, app, app_chat_key, title, kind, member_count, now, now))
        return key

    def mark_deep_read(self, conversation_key: str, at=None) -> None:
        iso, _, _ = T.parse_ts(at)
        with self._tx():
            self.conn.execute(
                "UPDATE conversation SET last_deep_read_at=? WHERE key=?", (iso, conversation_key))

    def create_entity(self, display_name: str, kind: str = "person",
                      confirmed: bool = False) -> str:
        key = T.entity_key_for(display_name, kind)
        with self._tx():
            self.conn.execute(
                "INSERT INTO entity(key, kind, display_name, confirmed, created_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                (key, kind, display_name, 1 if confirmed else 0, T.now_iso()))
        return key

    def bind_alias(self, entity_key: str, alias: str, scope_key: str | None = None,
                   app: str | None = None, source: str = "manual",
                   confirmed: bool = True) -> int:
        """把一个界面昵称绑到实体。群昵称请务必传 scope_key。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT 1 FROM entity WHERE key=?", (entity_key,)).fetchone()
            if not row:
                name = entity_key.split(":", 1)[-1]
                self.conn.execute(
                    "INSERT INTO entity(key, kind, display_name, confirmed, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (entity_key, "person", name, 1 if confirmed else 0, T.now_iso()))
            if confirmed:
                self.conn.execute(
                    "UPDATE entity SET confirmed=1 WHERE key=?", (entity_key,))
            cur = self.conn.execute(
                "INSERT INTO entity_alias(entity_key, alias, alias_norm, app, scope_key,"
                " source, created_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(alias_norm, scope_key, app) DO UPDATE SET"
                "   entity_key=excluded.entity_key, source=excluded.source",
                (entity_key, alias, T.normalize(alias), app or "", scope_key or "",
                 source, T.now_iso()))
            self._relink(T.normalize(alias), scope_key or "")
            return int(cur.lastrowid or 0)

    def _relink(self, alias_norm: str, scope_key: str) -> int:
        """别名绑定后，回填此前未消歧的消息。"""
        if scope_key:
            cur = self.conn.execute(
                "UPDATE message SET sender_entity_key=("
                "  SELECT a.entity_key FROM entity_alias a"
                "  WHERE a.alias_norm=? AND a.scope_key=?"
                ") WHERE conversation_key=? AND sender_entity_key IS NULL AND sender_norm=?",
                (alias_norm, scope_key, scope_key, alias_norm))
        else:
            cur = self.conn.execute(
                "UPDATE message SET sender_entity_key=("
                "  SELECT a.entity_key FROM entity_alias a"
                "  WHERE a.alias_norm=? AND a.scope_key=''"
                ") WHERE sender_entity_key IS NULL AND sender_norm=?",
                (alias_norm, alias_norm))
        return cur.rowcount or 0

    # -------------------------------------------------------------- 核心写入
    def ingest(self, conversation_key: str, messages: Iterable[dict],
               observation_id: int | None = None, captured_at=None) -> dict:
        """批量写入一次观测到的消息窗口。

        去重策略（按优先级）：
          1. m['new'] is True        → 强制插入，ordinal 取该锚的下一个空位
          2. m['client_ref'] 已存在   → 跳过（见 client_ref 说明）
          3. (anchor, ordinal) 已存在 → 跳过（滚动重看同一批时走这条）
          4. 否则插入
        ordinal 在**同一批次内**按相同锚递增，所以"三条相同的『好的』"会存成三条。
        """
        cap_iso, cap_epoch, _ = T.parse_ts(captured_at)
        msgs = list(messages or [])
        stats = {"inserted": 0, "skipped": 0, "messages": [], "warnings": []}
        batch_ordinal: dict[str, int] = {}

        with self._tx():
            conv = self.conn.execute(
                "SELECT key FROM conversation WHERE key=?", (conversation_key,)).fetchone()
            if not conv:
                raise KeyError(f"未知会话: {conversation_key}，请先 ensure_conversation()")

            for m in msgs:
                sender = (m.get("sender") or "").strip()
                body = (m.get("text") or "").strip()
                if not sender and not body:
                    stats["warnings"].append("空消息已忽略")
                    continue
                sender = sender or "unknown"
                body = body or f"[{m.get('msg_type', 'other')}]"

                ts_iso, ts_epoch, ts_conf = T.parse_ts(m.get("ts"), cap_iso or None)
                ts_conf = m.get("ts_conf") or ts_conf
                ah = T.anchor_hash(conversation_key, sender, body, ts_epoch)
                client_ref = (m.get("client_ref") or "").strip() or None

                if client_ref and self._client_ref_exists(conversation_key, client_ref):
                    stats["skipped"] += 1
                    continue

                if m.get("new"):
                    ordinal = self._next_ordinal(conversation_key, ah)
                else:
                    ordinal = batch_ordinal.get(ah, 0)
                    batch_ordinal[ah] = ordinal + 1
                    if self._anchor_exists(conversation_key, ah, ordinal):
                        stats["skipped"] += 1
                        continue

                fts_bi, fts_uni = T.build_fts(body)
                entity_key = self._resolve_sender(conversation_key, sender)
                cur = self.conn.execute(
                    "INSERT INTO message(conversation_key, sender_entity_key, sender_label,"
                    " sender_norm, text, ts_est, ts_epoch, ts_conf, msg_type, anchor_hash,"
                    " ordinal, client_ref, state, fts_bi, fts_uni, source_observation_id,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (conversation_key, entity_key, sender, T.normalize(sender), body,
                     ts_iso, ts_epoch, ts_conf, m.get("msg_type") or "text", ah,
                     ordinal, client_ref, "observed", fts_bi, fts_uni,
                     observation_id, T.now_iso()))
                mid = int(cur.lastrowid)
                stats["inserted"] += 1
                stats["messages"].append(mid)

            self.conn.execute(
                "UPDATE conversation SET last_seen_at=? WHERE key=?",
                (T.now_iso(), conversation_key))

        if stats["skipped"] and not stats["inserted"]:
            stats["warnings"].append("整批都被去重跳过：可能是重复观测，或该用 m['new']=True")
        return stats

    def _client_ref_exists(self, conv: str, ref: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM message WHERE conversation_key=? AND client_ref=?",
            (conv, ref)).fetchone() is not None

    def _anchor_exists(self, conv: str, ah: str, ordinal: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM message WHERE conversation_key=? AND anchor_hash=? AND ordinal=?",
            (conv, ah, ordinal)).fetchone() is not None

    def _next_ordinal(self, conv: str, ah: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM message"
            " WHERE conversation_key=? AND anchor_hash=?", (conv, ah)).fetchone()
        return int(row[0])

    def _resolve_sender(self, conversation_key: str, label: str) -> str | None:
        r = self.who(label, scope_key=conversation_key)
        return r["entity"]["key"] if r["entity"] else None

    def mark_vanished(self, message_id: int) -> int:
        with self._tx():
            cur = self.conn.execute(
                "UPDATE message SET state='vanished' WHERE id=? AND state<>'vanished'",
                (message_id,))
            return cur.rowcount or 0

    def confirm(self, message_id: int) -> int:
        with self._tx():
            cur = self.conn.execute(
                "UPDATE message SET state='confirmed' WHERE id=?", (message_id,))
            return cur.rowcount or 0

    # ============================================================== 读取
    def who(self, name: str, scope_key: str | None = None,
            app: str | None = None) -> dict:
        """昵称 → 实体。分辨率从强到弱，返回置信度而不是抛异常。"""
        n = T.normalize(name)
        if not n:
            return {"entity": None, "confidence": 0.0, "candidates": [], "reason": "empty"}

        ladder = []
        if scope_key:
            ladder.append((n, scope_key, app, 1.0, "scope_exact"))
        ladder.append((n, "", app, 0.8, "global_exact"))
        ladder.append((n, "", None, 0.7, "global_any_app"))

        for alias_norm, scope, app_filter, conf, reason in ladder:
            sql = ("SELECT e.key, e.kind, e.display_name, e.confirmed, a.scope_key, a.app"
                   " FROM entity_alias a JOIN entity e ON e.key=a.entity_key"
                   " WHERE a.alias_norm=? AND a.scope_key=?")
            args: list[Any] = [alias_norm, scope]
            if app_filter:
                sql += " AND (a.app=? OR a.app='')"
                args.append(app_filter)
            rows = self.conn.execute(sql, args).fetchall()
            if len(rows) == 1:
                r = rows[0]
                return {"entity": {"key": r["key"], "kind": r["kind"],
                                   "display_name": r["display_name"],
                                   "confirmed": bool(r["confirmed"])},
                        "confidence": conf, "candidates": [], "reason": reason}
            if len(rows) > 1:
                return {"entity": None, "confidence": conf * 0.5,
                        "candidates": [dict(x) for x in rows], "reason": "ambiguous"}

        # 模糊：别名包含关系（低置信）
        rows = self.conn.execute(
            "SELECT e.key, e.kind, e.display_name, e.confirmed, a.alias_norm"
            " FROM entity_alias a JOIN entity e ON e.key=a.entity_key"
            " WHERE (a.alias_norm LIKE ? OR ? LIKE a.alias_norm) "
            "   AND a.scope_key IN (?, '')",
            (f"%{n}%", n, scope_key or "")).fetchall()
        if rows:
            return {"entity": None, "confidence": 0.3,
                    "candidates": [dict(x) for x in rows], "reason": "fuzzy"}
        return {"entity": None, "confidence": 0.0, "candidates": [], "reason": "unknown"}

    def search(self, query: str, scope: str | None = None, since=None, until=None,
               speaker: str | None = None, k: int = 10,
               include_vanished: bool = False) -> list[dict]:
        """关键词检索。scope 是**硬过滤**，不做跨会话漂移。

        排序 = 0.75 × 名次分 + 0.25 × 新近分（30 天半衰）。
        """
        expr = T.fts_expr(query)
        if not expr:
            return []
        sql = ("SELECT m.*, bm25(message_fts) AS bm"
               " FROM message_fts JOIN message m ON m.id = message_fts.rowid"
               " WHERE message_fts MATCH ?")
        args: list[Any] = [expr]
        if scope:
            sql += " AND m.conversation_key = ?"; args.append(scope)
        if since is not None:
            sql += " AND m.ts_epoch >= ?"; args.append(self._epoch(since))
        if until is not None:
            sql += " AND m.ts_epoch <= ?"; args.append(self._epoch(until))
        if speaker:
            sql += " AND (m.sender_norm = ? OR m.sender_entity_key = ?)"
            args += [T.normalize(speaker), speaker]
        if not include_vanished:
            sql += " AND m.state <> 'vanished'"
        sql += " ORDER BY bm ASC LIMIT ?"
        args.append(max(k * 5, 50))

        rows = self.conn.execute(sql, args).fetchall()
        now = datetime.now(timezone.utc).timestamp()
        out = []
        for i, r in enumerate(rows):
            age_days = max(0.0, (now - r["ts_epoch"]) / 86400.0)
            score = 0.75 * (1.0 / (1 + i)) + 0.25 * math.exp(-age_days / 30.0)
            out.append(self._row_to_dict(r, score))
        out.sort(key=lambda x: x["_score"], reverse=True)
        return out[:k]

    def recent(self, scope: str, n: int = 30, include_vanished: bool = False) -> list[dict]:
        sql = "SELECT * FROM message WHERE conversation_key=?"
        args: list[Any] = [scope]
        if not include_vanished:
            sql += " AND state <> 'vanished'"
        sql += " ORDER BY ts_epoch DESC, ordinal DESC, id DESC LIMIT ?"
        args.append(n)
        rows = self.conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def digest(self, scope: str, since=None, limit: int = 200) -> dict:
        """返回**原始素材**（不调 LLM）。摘要由 agent 自己生成。"""
        conv = self.conn.execute("SELECT * FROM conversation WHERE key=?", (scope,)).fetchone()
        if not conv:
            return {"conversation": None, "messages": [], "participants": []}
        sql = ("SELECT * FROM message WHERE conversation_key=? AND state<>'vanished'")
        args: list[Any] = [scope]
        if since is not None:
            sql += " AND ts_epoch >= ?"; args.append(self._epoch(since))
        sql += " ORDER BY ts_epoch ASC, ordinal ASC, id ASC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        parts = self.conn.execute(
            "SELECT sender_label, sender_entity_key, COUNT(*) AS n FROM message"
            " WHERE conversation_key=? AND state<>'vanished' GROUP BY sender_label"
            " ORDER BY n DESC", (scope,)).fetchall()
        return {
            "conversation": {"key": conv["key"], "title": conv["title"],
                             "kind": conv["kind"], "app": conv["app"],
                             "last_deep_read_at": conv["last_deep_read_at"]},
            "message_count": len(rows),
            "participants": [dict(p) for p in parts],
            "messages": [self._row_to_dict(r) for r in rows],
        }

    def unresolved(self, limit: int = 50) -> dict:
        """「我不确定」的一等出口。agent 靠它决定要不要复核/追问。"""
        senders = self.conn.execute(
            "SELECT sender_label, sender_norm, conversation_key, COUNT(*) AS n,"
            "       MIN(ts_est) AS first_ts, MAX(ts_est) AS last_ts"
            " FROM message WHERE sender_entity_key IS NULL AND state<>'vanished'"
            " GROUP BY sender_label, conversation_key ORDER BY n DESC LIMIT ?",
            (limit,)).fetchall()
        unconfirmed = self.conn.execute(
            "SELECT key, kind, display_name FROM entity WHERE confirmed=0 LIMIT ?",
            (limit,)).fetchall()
        vanished = self.conn.execute(
            "SELECT id, conversation_key, sender_label, text, ts_est FROM message"
            " WHERE state='vanished' ORDER BY ts_epoch DESC LIMIT ?", (limit,)).fetchall()
        low_conf_ts = self.conn.execute(
            "SELECT COUNT(*) FROM message WHERE ts_conf='inferred'").fetchone()[0]
        return {
            "sender_labels": [dict(r) for r in senders],
            "entities_unconfirmed": [dict(r) for r in unconfirmed],
            "vanished": [dict(r) for r in vanished],
            "inferred_ts_count": int(low_conf_ts),
        }

    def coverage(self) -> list[dict]:
        """覆盖盲区：每个会话最后见到的时间、消息量、多久没深读。"""
        rows = self.conn.execute(
            "SELECT c.key, c.app, c.title, c.kind, c.last_deep_read_at,"
            "       COUNT(m.id) AS n, MAX(m.ts_epoch) AS last_epoch"
            " FROM conversation c LEFT JOIN message m ON m.conversation_key=c.key"
            " GROUP BY c.key ORDER BY last_epoch DESC").fetchall()
        now = datetime.now(timezone.utc).timestamp()
        out = []
        for r in rows:
            d = dict(r)
            d["stale_days"] = (round((now - r["last_epoch"]) / 86400.0, 1)
                               if r["last_epoch"] else None)
            out.append(d)
        return out

    def stats(self) -> dict:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {
            "conversations": q("SELECT COUNT(*) FROM conversation"),
            "messages": q("SELECT COUNT(*) FROM message"),
            "observations": q("SELECT COUNT(*) FROM observation"),
            "entities": q("SELECT COUNT(*) FROM entity"),
            "aliases": q("SELECT COUNT(*) FROM entity_alias"),
            "unresolved_messages": q(
                "SELECT COUNT(*) FROM message WHERE sender_entity_key IS NULL"),
            "vanished": q("SELECT COUNT(*) FROM message WHERE state='vanished'"),
            "embedded": q("SELECT COUNT(*) FROM message_vec"),
        }

    # ============================================================== 向量（可选）
    def set_embedder(self, fn: Callable, model: str | None = None) -> None:
        self._embedder = fn
        self._embed_model = model

    def embed_pending(self, limit: int = 200) -> int:
        if not self._embedder:
            raise RuntimeError("未设置 embedder：store.set_embedder(fn)")
        rows = self.conn.execute(
            "SELECT m.id, m.text FROM message m LEFT JOIN message_vec v ON v.message_id=m.id"
            " WHERE v.message_id IS NULL LIMIT ?", (limit,)).fetchall()
        if not rows:
            return 0
        vecs = self._embedder([r["text"] for r in rows])
        with self._tx():
            for r, v in zip(rows, vecs):
                buf = array("f", v)
                self.conn.execute(
                    "INSERT OR REPLACE INTO message_vec(message_id, dim, vec, model)"
                    " VALUES (?,?,?,?)",
                    (r["id"], len(v), buf.tobytes(), self._embed_model))
        return len(rows)

    def vector_search(self, query: str, scope: str | None = None, k: int = 10) -> list[dict]:
        if not self._embedder:
            return []
        qv = self._embedder([query])[0]
        sql = ("SELECT m.*, v.vec, v.dim FROM message_vec v JOIN message m ON m.id=v.message_id"
               " WHERE m.state<>'vanished'")
        args: list[Any] = []
        if scope:
            sql += " AND m.conversation_key=?"; args.append(scope)
        rows = self.conn.execute(sql, args).fetchall()
        scored = []
        for r in rows:
            v = array("f"); v.frombytes(r["vec"])
            scored.append((self._cosine(qv, v), r))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for sim, r in scored[:k]:
            d = self._row_to_dict(r, sim)
            d["_vector_sim"] = round(sim, 4)
            out.append(d)
        return out

    @staticmethod
    def _cosine(a, b) -> float:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a))
        dbb = math.sqrt(sum(y * y for y in b))
        return num / (da * dbb) if da and dbb else 0.0

    # ============================================================== 内部
    @staticmethod
    def _epoch(v) -> int:
        if isinstance(v, (int, float)):
            return int(v)
        return T.parse_ts(v)[1]

    @staticmethod
    def _row_to_dict(r: sqlite3.Row, score: float | None = None) -> dict:
        d = {
            "id": r["id"],
            "scope": r["conversation_key"],
            "sender": r["sender_label"],
            "sender_entity": r["sender_entity_key"],
            "text": r["text"],
            "ts": r["ts_est"],
            "ts_conf": r["ts_conf"],
            "type": r["msg_type"],
            "state": r["state"],
            "observation_id": r["source_observation_id"],
        }
        if score is not None:
            d["_score"] = round(score, 4)
        return d

    def to_json(self, obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)
