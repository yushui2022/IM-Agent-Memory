#!/usr/bin/env python3
"""端到端演示：读 → 检索 → 起草 → 闸门 → 发送 → 回读验证。

不接任何真实 IM。屏幕和输入框都是打桩的，目的是把整条链路的**契约**跑通：
    agent 感知 → ingest() → search() → enqueue_draft() → approve() → mark_sent()
              → 下一帧感知 → ingest() 自动回读验证 → sent_by='agent'

运行： python tools/mock_demo.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T


def line(title: str = "") -> None:
    print(f"\n{'─' * 62}" + (f"\n{title}" if title else ""))


def show(msg: dict) -> str:
    tag = {"agent": "🤖agent发", "human": "👤本人发", "other": "·他人", "unknown": "?"}[
        msg["sent_by"]]
    return f"  [{tag}] {msg['sender']}: {msg['text']}"


def main() -> int:
    s = Store(":memory:")
    s.set_self_label("wechat", "我")

    dm = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp_pm")

    # ---------------------------------------------------------------- 0 前情
    line("0. 历史（之前已经看过并存入记忆的）")
    s.ingest(dm, [
        {"sender": "张三", "text": "周四的评审你来吗", "ts": "2026-10-19T10:00:00"},
        {"sender": "我", "text": "来，几点？", "ts": "2026-10-19T10:02:00"},
        {"sender": "张三", "text": "下午三点，会议室 B", "ts": "2026-10-19T10:03:00"},
    ])
    for m in reversed(s.recent(dm, n=3)):
        print(show(m))

    # ---------------------------------------------------------------- 1 收到新消息
    line("1. 感知层抓到一条新消息 → 写入")
    obs = s.add_observation("wechat", captured_at="2026-10-20T09:30:00",
                            window_title="张三", image_ref="shot-0101.png")
    res = s.ingest(dm, [
        {"sender": "张三", "text": "明天评审改到上午十点了",
         "ts": "2026-10-20T09:29:00", "client_ref": "shot-0101:0"},
    ], observation_id=obs, captured_at="2026-10-20T09:30:00")
    print(f"  写入 {res['inserted']} 条（跳过 {res['skipped']}）")
    print(show(s.recent(dm, n=1)[0]))

    # ---------------------------------------------------------------- 2 检索
    line("2. agent 检索相关记忆，决定怎么回")
    hits = s.search("评审", scope=dm)
    print(f"  命中 {len(hits)} 条：")
    for h in hits:
        src = f"观测#{h['observation_id']}" if h["observation_id"] else "无观测出处"
        print(f"    {h['ts']}  {h['sender']}: {h['text']}   ({src})")

    # ---------------------------------------------------------------- 3 群聊拦阻
    line("3. 假如 agent 想在群里回 —— 被拦（默认禁止代发）")
    blocked = s.enqueue_draft(grp, "好的，十点我来")
    print(f"  state={blocked['state']}  risk={blocked['risk']}")
    print(f"  理由：{blocked['reason']}")

    # ---------------------------------------------------------------- 4 私聊起草
    line("4. 私聊起草（只入队，不发送）")
    draft = s.enqueue_draft(dm, "好的，十点我到。要不要我提前把上次的结论整理一版？")
    oid = draft["id"]
    print(f"  outbox#{oid}  state={draft['state']}  risk={draft['risk']}")

    # ---------------------------------------------------------------- 5 预检
    line("5. 发送前窗口预检（防切错窗口）")
    good = s.preflight(dm, "张三")
    bad = s.preflight(dm, "产品群")
    print(f"  窗口=「张三」   → ok={good['ok']}")
    print(f"  窗口=「产品群」 → ok={bad['ok']}  {bad['reason']}")

    # ---------------------------------------------------------------- 6 发送
    line("6. 审批 → agent 在输入框打字并发送")
    s.approve(oid)
    s.mark_sent(oid)
    ob = s.outbox_get(oid)
    print(f"  outbox#{oid}  state={ob['state']}  attempts={ob['attempts']}")
    print("  ⚠️ 此刻【没有任何证据】证明真的发出去了 —— 只能回读")

    # ---------------------------------------------------------------- 7 回读
    line("7. 下一帧感知读到自己那条 → ingest() 自动完成回读验证")
    res2 = s.ingest(dm, [
        {"sender": "我", "text": "好的，十点我到。要不要我提前把上次的结论整理一版？",
         "ts": "2026-10-20T09:31:00"},
    ], captured_at="2026-10-20T09:31:05")
    print(f"  新增 {res2['inserted']} 条；自动验证 outbox：{res2.get('verified_outbox')}")

    m = s.recent(dm, n=1)[0]
    print(show(m))
    print(f"  → outbox#{m['outbox_id']} state={s.outbox_get(m['outbox_id'])['state']}")

    line("8. 会话现在的完整样子")
    for x in reversed(s.recent(dm, n=10)):
        print(show(x))

    # ---------------------------------------------------------------- 9 失败形态
    line("9. 另一种常见形态：发出去了但回读不到（静默失败）")
    d2 = s.enqueue_draft(dm, "这条假设发送失败")
    s.approve(d2["id"])
    s.mark_sent(d2["id"])
    old = (datetime.now(T.TZ) - timedelta(minutes=10)).isoformat(timespec="seconds")
    s.conn.execute("UPDATE outbox SET sent_at=? WHERE id=?", (old, d2["id"]))
    stuck = s.outbox_unverified(older_than_s=120)
    print(f"  超时未验证：{[x['id'] for x in stuck]}")
    print(f"  agent 每轮应检查的 pending：{[(x['id'], x['state']) for x in s.outbox_pending(dm)]}")

    # ---------------------------------------------------------------- 10 死循环防线
    line("10. 死循环防线：同一句话重复入队被拦")
    again = s.enqueue_draft(dm, "这条假设发送失败")
    print(f"  risk={again['risk']}  理由：{again['reason']}")

    line("完成")
    print(f"  统计：{s.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
