"""起草层测试。

核心断言只有一句：**起草永远不发送。**
其余是策略正确性：该回的回、该跳的跳、同一条只起草一次。

运行： python tests/test_drafter.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T
from imem import drafter


def now_ts(offset_s: int = 0) -> str:
    return (datetime.now(T.TZ) - timedelta(seconds=offset_s)).isoformat(timespec="seconds")


def setup():
    s = Store(":memory:")
    s.set_self_label("wechat", "我")
    dm = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp")
    return s, dm, grp


def test_incoming_dm_gets_drafted():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "明天的会议改到几点了？", "ts": now_ts(5)}])
    r = drafter.run_once(s)
    assert r["candidates"] == 1, r
    assert len(r["drafted"]) == 1, r
    d = r["drafted"][0]
    assert d["state"] == "draft", d
    assert d["risk"] == "normal", d
    assert d["draft"], d


def test_draft_is_never_sent():
    """整套设计的红线：起草不产生任何发送动作。"""
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "在吗", "ts": now_ts(5)}])
    drafter.run_once(s)

    states = [x["state"] for x in s.conn.execute("SELECT state FROM outbox").fetchall()]
    assert states == ["draft"], states
    # 没有 sent / 没有消息被标成 agent 发的
    assert s.stats()["messages"] == 1
    assert s.recent(dm, n=10)[0]["sent_by"] == "other"


def test_group_skipped_by_default():
    s, dm, grp = setup()
    s.ingest(grp, [{"sender": "李四", "text": "这个方案谁看过了？", "ts": now_ts(5)}])
    r = drafter.run_once(s)
    assert r["candidates"] == 0, r


def test_group_drafted_but_gate_blocks_when_policy_allows():
    s, dm, grp = setup()
    s.ingest(grp, [{"sender": "李四", "text": "这个方案谁看过了？", "ts": now_ts(5)}])
    r = drafter.run_once(s, policy=drafter.Policy(skip_groups=False))
    assert len(r["drafted"]) == 1, r
    d = r["drafted"][0]
    assert d["risk"] == "blocked", d
    assert "群聊" in d["blocked_reason"], d
    # 被拦的不会留在 pending 里
    assert s.outbox_pending() == []


def test_own_messages_never_drafted():
    s, dm, _ = setup()
    s.ingest(dm, [
        {"sender": "我", "text": "我先说一句", "ts": now_ts(10)},
        {"sender": "张三", "text": "收到", "ts": now_ts(8)},
        {"sender": "张三", "text": "对了那个文件在哪？", "ts": now_ts(5)},
    ])
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=0))
    # 只有张三的两条是候选，"我"那条不算
    assert r["candidates"] == 2, r
    assert all(x["sender"] == "张三" for x in r["drafted"]), r["drafted"]


def test_answered_message_skipped():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "明天开会吗", "ts": now_ts(60)}])
    s.ingest(dm, [{"sender": "我", "text": "开，九点", "ts": now_ts(30)}])
    r = drafter.run_once(s)
    assert r["candidates"] == 0, r


def test_idempotent_one_draft_per_message():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "在吗", "ts": now_ts(5)}])
    r1 = drafter.run_once(s)
    assert len(r1["drafted"]) == 1
    r2 = drafter.run_once(s)
    assert r2["candidates"] == 0, r2       # 第二轮不再重复起草
    assert s.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

    # 第二道防线：绕过 find_candidates 直接再起一次，闸门也会拦
    mid = s.recent(dm, n=1)[0]["id"]
    msg = dict(s.conn.execute("SELECT * FROM message WHERE id=?", (mid,)).fetchone())
    again = drafter.draft_for(s, msg)
    assert again["risk"] == "blocked", again
    assert "已起草过" in again["blocked_reason"], again


def test_stale_message_skipped():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "这条是两小时前的", "ts": now_ts(7200)}])
    r = drafter.run_once(s, policy=drafter.Policy(max_age_s=3600))
    assert r["candidates"] == 0, r


def test_composer_returning_none_skips():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "嗯", "ts": now_ts(5)}])
    r = drafter.run_once(s, composer=lambda ctx: None)
    assert r["candidates"] == 1
    assert r["drafted"] == [], r
    assert "不值得回" in r["skipped"][0]["reason"], r


def test_source_message_linked():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "文件发我一下", "ts": now_ts(5)}])
    mid = s.recent(dm, n=1)[0]["id"]
    drafter.run_once(s)
    ob = s.outbox_for_message(mid)
    assert ob is not None and ob["source_message_id"] == mid, ob
    assert ob["state"] == "draft"


def test_min_gap_prevents_flood():
    s, dm, _ = setup()
    s.ingest(dm, [
        {"sender": "张三", "text": "第一条", "ts": now_ts(20)},
        {"sender": "张三", "text": "第二条", "ts": now_ts(15)},
        {"sender": "张三", "text": "第三条", "ts": now_ts(10)},
    ])
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=300))
    assert len(r["drafted"]) == 1, r
    assert len(r["skipped"]) == 2, r
    assert all("不足 300s" in x["reason"] for x in r["skipped"]), r["skipped"]


def test_max_drafts_per_run_capped():
    s, dm, grp = setup()
    dm2 = s.ensure_conversation("wechat", "李四", kind="dm", app_chat_key="dm_ls")
    dm3 = s.ensure_conversation("wechat", "王五", kind="dm", app_chat_key="dm_ww")
    for conv in (dm, dm2, dm3):
        s.ingest(conv, [{"sender": "某人", "text": "问你个事", "ts": now_ts(5)}])
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=0, max_drafts_per_run=2))
    assert len(r["drafted"]) == 2, r
    assert any("单轮上限" in x["reason"] for x in r["skipped"]), r["skipped"]


def test_composer_receives_context():
    s, dm, _ = setup()
    # 第二条包含第一条的子串，所以子串检索能召回（这就是当前的真实能力边界）
    s.ingest(dm, [
        {"sender": "张三", "text": "周四的评审改到几点了", "ts": now_ts(120)},
        {"sender": "张三", "text": "评审改到几点了，我这边好安排", "ts": now_ts(5)},
    ])
    seen = []

    def composer(ctx):
        seen.append(ctx)
        return "收到，我看下日程回你。"

    drafter.run_once(s, composer=composer, policy=drafter.Policy(min_gap_s=0))
    by_text = {c["message"]["text"]: c for c in seen}
    assert by_text, seen

    ctx = by_text["评审改到几点了，我这边好安排"]
    assert ctx["conversation"]["title"] == "张三", ctx["conversation"]
    assert len(ctx["recent"]) >= 2, ctx["recent"]
    related_texts = [r["text"] for r in ctx["related"]]
    assert any("周四的评审改到几点了" in t for t in related_texts), related_texts


def test_blank_message_not_drafted():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "张三", "text": "…", "ts": now_ts(5)}])
    r = drafter.run_once(s)
    assert r["drafted"] == [], r       # 太短，composer 返回 None


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
