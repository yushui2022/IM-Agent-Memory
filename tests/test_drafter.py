"""决策层测试。

核心断言：
  - **默认自治**：普通消息直接处理掉，人不需要在场
  - **该沉默就沉默**：确认语、纯附件不回
  - **该找人时才找人**：涉及钱/合同/投诉 → 升级，且不自作主张
  - **红线不变**：整个过程不产生任何发送动作

运行： python tests/test_drafter.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T
from imem import drafter


def ago(s: int) -> str:
    return (datetime.now(T.TZ) - timedelta(seconds=s)).isoformat(timespec="seconds")


def setup():
    s = Store(":memory:")
    s.set_self_label("wechat", "我")
    dm = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp")
    return s, dm, grp


def ask(s, conv, text, sender="张三", ts=5):
    s.ingest(conv, [{"sender": sender, "text": text, "ts": ago(ts)}])
    return s.recent(conv, n=1)[0]["id"]


# ------------------------------------------------------------------ 默认自治
def test_autonomous_reply_by_default():
    s, dm, _ = setup()
    ask(s, dm, "评审改到几点了？")
    r = drafter.run_once(s)

    assert len(r["replied"]) == 1, r
    d = r["replied"][0]
    assert d["state"] == "approved", d          # 自动放行，不等人
    ob = s.outbox_get(d["outbox_id"])
    assert ob["state"] == "approved" and ob["auto_approved"] == 1, ob
    assert r["escalated"] == [] and r["silent"] == [], r


def test_autonomous_does_not_send():
    """红线：自动放行 ≠ 发送。"""
    s, dm, _ = setup()
    ask(s, dm, "评审改到几点了？")
    drafter.run_once(s)

    states = [x["state"] for x in s.conn.execute("SELECT state FROM outbox").fetchall()]
    assert "sent" not in states and "verified" not in states, states
    assert s.conn.execute(
        "SELECT COUNT(*) FROM message WHERE sent_by='agent'").fetchone()[0] == 0
    # 进入了执行器队列，等着被真正发出
    assert len(s.outbox_ready_to_send()) == 1


def test_manual_mode_still_available():
    """想要人工审的时候，把 auto_approve 关掉即可回到草稿模式。"""
    s, dm, _ = setup()
    ask(s, dm, "评审改到几点了？")
    r = drafter.run_once(s, policy=drafter.Policy(auto_approve=False))
    assert r["replied"][0]["state"] == "draft", r
    assert s.outbox_ready_to_send() == []


# ------------------------------------------------------------------ 沉默
def test_ack_only_stays_silent():
    s, dm, _ = setup()
    for i, t in enumerate(["收到", "好的", "嗯", "谢谢", "👌"]):
        ask(s, dm, t, ts=100 - i * 5)
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=0))
    assert r["replied"] == [], r
    assert len(r["silent"]) == 5, r
    assert all("不需要回应" in x["reason"] for x in r["silent"]), r["silent"]


def test_attachment_only_stays_silent():
    s, dm, _ = setup()
    ask(s, dm, "[图片]")
    ask(s, dm, "[语音]", ts=3)
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=0))
    assert r["replied"] == [], r


def test_composer_returning_none_stays_silent():
    s, dm, _ = setup()
    ask(s, dm, "这个要怎么弄？")
    r = drafter.run_once(s, composer=lambda ctx: None)
    assert r["replied"] == [], r
    assert "不值得回" in r["silent"][0]["reason"], r


def test_allowlist_restricts_autonomy():
    s, dm, _ = setup()
    dm2 = s.ensure_conversation("wechat", "李四", kind="dm", app_chat_key="dm_ls")
    ask(s, dm, "几点开会？")
    ask(s, dm2, "几点开会？")

    # 空白名单 = 全部禁用
    r = drafter.run_once(s, policy=drafter.Policy(scope_allowlist=[]))
    assert r["replied"] == [], r
    assert len(r["silent"]) == 2, r

    # 只放开其中一个
    r2 = drafter.run_once(s, policy=drafter.Policy(scope_allowlist=[dm]))
    assert len(r2["replied"]) == 1, r2
    assert r2["replied"][0]["conversation_key"] == dm, r2


def test_group_never_autonomous():
    s, dm, grp = setup()
    ask(s, grp, "这个方案谁看过？", sender="王五")
    r = drafter.run_once(s)
    assert r["candidates"] == 0 and r["replied"] == [], r


# ------------------------------------------------------------------ 升级
def test_escalates_on_money_or_contract():
    s, dm, _ = setup()
    ask(s, dm, "合同你签了吗")
    r = drafter.run_once(s)

    assert r["replied"] == [], r                 # 没有自作主张
    assert len(r["escalated"]) == 1, r
    e = r["escalated"][0]
    assert "合同" in e["reason"], e
    ob = s.outbox_get(e["outbox_id"])
    assert ob["state"] == "escalated" and ob["text"] == "", ob
    assert s.outbox_for_message(e["message_id"]) is not None


def test_escalation_not_reprocessed():
    s, dm, _ = setup()
    ask(s, dm, "投诉这个怎么处理")
    r1 = drafter.run_once(s)
    assert len(r1["escalated"]) == 1, r1
    r2 = drafter.run_once(s)
    assert r2["candidates"] == 0, r2


def test_escalation_can_be_resolved_by_human():
    s, dm, _ = setup()
    ask(s, dm, "这个报价要不要发")
    e = drafter.run_once(s)["escalated"][0]
    assert len(s.outbox_escalated()) == 1
    assert s.resolve_escalation(e["outbox_id"]) == 1
    assert s.outbox_escalated() == []


def test_daily_fuse_escalates():
    """单会话一天自动回太多 → 熔断交给人工，而不是继续刷。"""
    s, dm, _ = setup()
    ask(s, dm, "第一件事几点？", ts=60)
    ask(s, dm, "第二件事几点？", ts=50)
    pol = drafter.Policy(min_gap_s=0, max_auto_per_day=1)
    r = drafter.run_once(s, policy=pol)

    assert len(r["replied"]) == 1, r
    assert len(r["escalated"]) == 1, r
    assert "上限" in r["escalated"][0]["reason"], r


# ------------------------------------------------------------------ 其他
def test_answered_message_skipped():
    s, dm, _ = setup()
    ask(s, dm, "明天开会吗", ts=60)
    s.ingest(dm, [{"sender": "我", "text": "开", "ts": ago(30)}])
    r = drafter.run_once(s)
    assert r["candidates"] == 0, r


def test_idempotent_one_pass_per_message():
    s, dm, _ = setup()
    ask(s, dm, "文件在哪？")
    r1 = drafter.run_once(s)
    assert len(r1["replied"]) == 1, r1
    r2 = drafter.run_once(s)
    assert r2["candidates"] == 0, r2
    assert s.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_min_gap_prevents_flood():
    s, dm, _ = setup()
    ask(s, dm, "第一件事几点？", ts=20)
    ask(s, dm, "第二件事几点？", ts=15)
    ask(s, dm, "第三件事几点？", ts=10)
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=300))
    assert len(r["replied"]) == 1, r
    assert len(r["silent"]) == 2, r


def test_own_messages_never_processed():
    s, dm, _ = setup()
    s.ingest(dm, [{"sender": "我", "text": "我先说一句", "ts": ago(10)}])
    ask(s, dm, "你刚说什么？", ts=5)
    r = drafter.run_once(s)
    assert len(r["replied"]) == 1, r
    assert r["replied"][0]["incoming"] == "你刚说什么？", r


def test_should_reply_logic():
    assert drafter.should_reply("评审改到几点了？") is True
    assert drafter.should_reply("帮我发一下文件") is True
    assert drafter.should_reply("收到") is False
    assert drafter.should_reply("好的") is False
    assert drafter.should_reply("嗯") is False
    assert drafter.should_reply("[图片]") is False
    assert drafter.should_reply("今天天气不错") is False    # 默认沉默


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
