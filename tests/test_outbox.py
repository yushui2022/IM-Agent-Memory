"""出站（代发）层测试。

重点不是"能发"，而是：
  - 该拦的拦住（群聊、超长、限流、循环）
  - 发了没发出去能发现（回读验证）
  - 自己发的和别人发的不会混（归因）

运行： python tests/test_outbox.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T
from imem import db as dbmod


def setup():
    """一个私聊 + 一个群聊，并登记「我叫什么」。"""
    s = Store(":memory:")
    s.set_self_label("wechat", "我")
    dm = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zhangsan")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp1")
    return s, dm, grp


def ago(seconds: int) -> str:
    return (datetime.now(T.TZ) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 闸门
def test_group_send_blocked_by_default():
    s, dm, grp = setup()
    r = s.enqueue_draft(grp, "这条不该能发")
    assert r["risk"] == "blocked", r
    assert r["state"] == "cancelled"
    assert "群聊" in r["reason"], r
    assert s.outbox_pending() == []          # 没有留下待发项


def test_group_allowed_when_explicitly_enabled():
    s, dm, grp = setup()
    r = s.enqueue_draft(grp, "显式放开的群发", allow_group=True)
    assert r["state"] == "draft" and r["risk"] == "normal", r


def test_empty_and_too_long_blocked():
    s, dm, _ = setup()
    assert s.enqueue_draft(dm, "   ")["risk"] == "blocked"
    r = s.enqueue_draft(dm, "长" * 600)      # 默认上限 500
    assert r["risk"] == "blocked" and "上限" in r["reason"], r


def test_rate_limit():
    s, dm, _ = setup()
    assert s.enqueue_draft(dm, "第一条", rate_limit=2)["state"] == "draft"
    assert s.enqueue_draft(dm, "第二条", rate_limit=2)["state"] == "draft"
    r = s.enqueue_draft(dm, "第三条", rate_limit=2)
    assert r["risk"] == "blocked" and "限流" in r["reason"], r


def test_duplicate_blocked_loop_guard():
    """同一句话短时间内重复入队 = 死循环特征，必须拦。"""
    s, dm, _ = setup()
    assert s.enqueue_draft(dm, "好的我看看")["state"] == "draft"
    r = s.enqueue_draft(dm, "好的我看看")
    assert r["risk"] == "blocked" and "重复" in r["reason"], r


# ------------------------------------------------------------------ 正常链路
def test_happy_path_draft_to_verified():
    s, dm, _ = setup()
    r = s.enqueue_draft(dm, "收到，晚点回你")
    oid = r["id"]
    assert r["state"] == "draft" and r["risk"] == "normal"

    assert s.approve(oid) == 1
    assert s.outbox_get(oid)["state"] == "approved"

    assert s.mark_sent(oid) == 1
    ob = s.outbox_get(oid)
    assert ob["state"] == "sent" and ob["sent_at"] and ob["attempts"] == 1

    # 回读：下一帧看到自己刚发的这条
    res = s.ingest(dm, [{"sender": "我", "text": "收到，晚点回你"}])
    assert res["inserted"] == 1, res
    assert res.get("verified_outbox") == [oid], res

    msg = s.recent(dm, n=1)[0]
    assert msg["sent_by"] == "agent", msg
    assert msg["outbox_id"] == oid, msg

    ob = s.outbox_get(oid)
    assert ob["state"] == "verified" and ob["message_id"] == msg["id"]


def test_illegal_transitions_rejected():
    s, dm, _ = setup()
    oid = s.enqueue_draft(dm, "状态机测试")["id"]
    # draft 不能直接 mark_sent
    assert s.mark_sent(oid) == 0
    assert s.outbox_get(oid)["state"] == "draft"
    # 未审批不能取消？可以取消 draft
    assert s.cancel(oid) == 1
    # 已取消不能再审批
    assert s.approve(oid) == 0
    assert s.outbox_get(oid)["state"] == "cancelled"


# ------------------------------------------------------------------ 归因
def test_readback_requires_self_sender():
    """别人说了同样的话，不能被我方的 outbox 认领。"""
    s, dm, _ = setup()
    oid = s.enqueue_draft(dm, "好的")["id"]
    s.approve(oid)
    s.mark_sent(oid)

    res = s.ingest(dm, [{"sender": "张三", "text": "好的"}])
    assert res.get("verified_outbox") is None, res
    assert s.outbox_get(oid)["state"] == "sent"
    assert s.recent(dm, n=1)[0]["sent_by"] == "other"


def test_readback_window_expiry():
    """隔了两小时才读到同一条，不再认为是自己刚发的。"""
    s, dm, _ = setup()
    oid = s.enqueue_draft(dm, "过期的回读")["id"]
    s.approve(oid)
    s.mark_sent(oid)
    s.conn.execute("UPDATE outbox SET sent_at=? WHERE id=?", (ago(7200), oid))

    res = s.ingest(dm, [{"sender": "我", "text": "过期的回读"}])
    assert res.get("verified_outbox") is None, res
    assert s.outbox_get(oid)["state"] == "sent"


def test_self_label_distinguishes_human_and_other():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "我", "text": "我自己手动发的"}])
    s.ingest(dm, [{"sender": "张三", "text": "别人发的"}])
    rows = s.recent(dm, n=10)
    by_text = {r["text"]: r["sent_by"] for r in rows}
    assert by_text["我自己手动发的"] == "human", by_text
    assert by_text["别人发的"] == "other", by_text


def test_explicit_sent_by_respected():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "我", "text": "外部标注的", "sent_by": "agent"}])
    assert s.recent(dm, n=1)[0]["sent_by"] == "agent"


# ------------------------------------------------------------------ 其他
def test_preflight_detects_wrong_window():
    s, dm, _ = setup()
    assert s.preflight(dm, "张三")["ok"] is True
    bad = s.preflight(dm, "李四")
    assert bad["ok"] is False
    assert "不一致" in bad["reason"], bad
    assert s.preflight("wechat:不存在", "x")["ok"] is False


def test_unverified_detection_catches_silent_failure():
    """已发送但回读不到 —— computer use 下最常见的失败形态。"""
    s, dm, _ = setup()
    oid = s.enqueue_draft(dm, "发出去但没回读")["id"]
    s.approve(oid)
    s.mark_sent(oid)
    s.conn.execute("UPDATE outbox SET sent_at=? WHERE id=?", (ago(600), oid))

    stuck = s.outbox_unverified(older_than_s=120)
    assert [x["id"] for x in stuck] == [oid], stuck
    # 说明它还在 pending 里，agent 每轮都该看
    assert oid in [x["id"] for x in s.outbox_pending(dm)]


def test_schema_version_recorded():
    s = Store(":memory:")
    assert dbmod.schema_version(s.conn) == dbmod.SCHEMA_VERSION


def test_auto_approve_goes_straight_to_approved():
    s, dm, _ = setup()
    r = s.enqueue_draft(dm, "策略自动放行", auto_approve=True)
    assert r["state"] == "approved", r
    ob = s.outbox_get(r["id"])
    assert ob["state"] == "approved" and ob["auto_approved"] == 1 and ob["approved_at"], ob
    # 进了执行器队列，但依然没发送
    assert [x["id"] for x in s.outbox_ready_to_send()] == [r["id"]]
    assert s.stats()["messages"] == 0


def test_blocked_never_enters_ready_queue():
    s, dm, grp = setup()
    r = s.enqueue_draft(grp, "群里不该发", auto_approve=True)
    assert r["risk"] == "blocked" and r["state"] == "cancelled", r
    assert s.outbox_ready_to_send() == []


def test_escalate_state_and_resolve():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "合同你签了吗"}])
    mid = s.recent(dm, n=1)[0]["id"]

    e = s.escalate(dm, "涉及「合同」", source_message_id=mid)
    assert e["state"] == "escalated"
    rows = s.outbox_escalated()
    assert len(rows) == 1 and rows[0]["escalate_reason"] == "涉及「合同」", rows
    assert rows[0]["auto_approved"] == 0
    # 升级项不在待发队列里
    assert s.outbox_ready_to_send() == []

    assert s.resolve_escalation(e["id"]) == 1
    assert s.outbox_escalated() == []


def test_recent_auto_for_digest():
    s, dm, _ = setup()
    s.enqueue_draft(dm, "自动回的", auto_approve=True)
    s.enqueue_draft(dm, "没放行的", auto_approve=False)
    rows = s.outbox_recent_auto(hours=24)
    assert len(rows) == 1 and rows[0]["text"] == "自动回的", rows


def test_migration_rebuilds_old_outbox():
    """老库的 CHECK 不允许 'escalated'，迁移要重建表且不丢数据。"""
    import sqlite3
    from imem import db as dbmod

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE conversation(key TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE message(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("""
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY,
            conversation_key TEXT NOT NULL,
            text TEXT NOT NULL,
            text_norm TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft'
                 CHECK (state IN ('draft','approved','sent','verified','failed','cancelled')),
            risk TEXT NOT NULL DEFAULT 'normal',
            block_reason TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT, sent_at TEXT, verified_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            source_message_id INTEGER,
            note TEXT
        )""")
    conn.execute("INSERT INTO conversation(key) VALUES('wechat:x')")
    conn.execute(
        "INSERT INTO outbox(conversation_key, text, text_norm, state, created_at,"
        " source_message_id, attempts) VALUES ('wechat:x','老数据','老数据','draft','2026-01-01',5,2)")

    dbmod._migrate(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert {"escalate_reason", "auto_approved", "source_message_id"} <= cols, cols
    row = conn.execute("SELECT * FROM outbox WHERE id=1").fetchone()
    assert row["text"] == "老数据" and row["source_message_id"] == 5, dict(row)
    assert row["attempts"] == 2, dict(row)
    assert row["auto_approved"] == 0
    # 现在能插 escalated 了
    conn.execute("INSERT INTO outbox(conversation_key, text, text_norm, state, created_at)"
                 " VALUES ('wechat:x','','','escalated','2026-01-02')")
    assert conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE state='escalated'").fetchone()[0] == 1
    assert dbmod.schema_version(conn) == dbmod.SCHEMA_VERSION


# ------------------------------------------------------------------ 运行器
def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
