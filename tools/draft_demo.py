#!/usr/bin/env python3
"""草稿模式演示：读到新消息 → 起草 → 入队（**绝不发送**）。

用的是内置的占位 composer，只为验证链路。真实回复质量需要你自己接 LLM：
    from imem import drafter
    drafter.run_once(store, composer=my_llm_composer)

运行： python tools/draft_demo.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T
from imem import drafter


def line(title: str = "") -> None:
    print(f"\n{'─' * 62}" + (f"\n{title}" if title else ""))


def ago(seconds: int) -> str:
    return (datetime.now(T.TZ) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def main() -> int:
    s = Store(":memory:")
    s.set_self_label("wechat", "我")

    zhangsan = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    lisi = s.ensure_conversation("wechat", "李四", kind="dm", app_chat_key="dm_ls")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp_pm")

    # ---------------------------------------------------------------- 0 历史
    line("0. 先有历史上下文")
    s.ingest(zhangsan, [
        {"sender": "张三", "text": "周四的评审改到几点了", "ts": ago(3600)},
        {"sender": "我", "text": "我查下日程", "ts": ago(3500)},
    ])
    for m in reversed(s.recent(zhangsan, n=2)):
        print(f"  {m['sender']}: {m['text']}")

    # ---------------------------------------------------------------- 1 收到新消息
    line("1. 感知层抓到三条新消息 → 写入")
    s.ingest(zhangsan, [
        {"sender": "张三", "text": "评审改到几点了，我这边好安排", "ts": ago(20)},
    ])
    s.ingest(lisi, [
        {"sender": "李四", "text": "那个合同你签了吗？", "ts": ago(15)},
    ])
    s.ingest(grp, [
        {"sender": "王五", "text": "这个方案谁看过", "ts": ago(10)},
    ])
    print(f"  私聊 张三 ×1，私聊 李四 ×1，群聊 产品群 ×1")

    # ---------------------------------------------------------------- 2 起草
    line("2. 起草一轮（默认策略：跳过群聊、跳过已回复、1 小时内）")
    r1 = drafter.run_once(s)
    print(f"  {drafter.summarize(r1)}")
    for d in r1["drafted"]:
        print(f"\n  → 给「{d['sender']}」的草稿（outbox#{d['outbox_id']}）")
        print(f"    收到：{d['incoming']}")
        print(f"    拟回：{d['draft']}")
        print(f"    状态：{d['state']}  风险：{d['risk']}")

    # ---------------------------------------------------------------- 3 群聊
    line("3. 群聊为什么没被起草")
    print(f"  默认 Policy(skip_groups=True)：群消息不进候选（群发本来就被闸门拦）")
    r_group = drafter.run_once(s, policy=drafter.Policy(skip_groups=False, min_gap_s=0))
    for d in r_group["drafted"]:
        print(f"  群聊「{d['sender']}」尝试起草 → {d['risk']}：{d['blocked_reason']}")

    # ---------------------------------------------------------------- 4 幂等
    line("4. 再跑一轮 —— 同一条消息不会再起草")
    r2 = drafter.run_once(s)
    print(f"  {drafter.summarize(r2)}")
    print(f"  outbox 总数：{s.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]}")

    # ---------------------------------------------------------------- 5 待审队列
    line("5. 待审队列（这就是要推给你手机的内容）")
    for ob in s.outbox_pending():
        conv = s.conn.execute("SELECT title FROM conversation WHERE key=?",
                              (ob["conversation_key"],)).fetchone()["title"]
        print(f"  outbox#{ob['id']}  [{conv}]  state={ob['state']}")
        print(f"    {ob['text']}")

    # ---------------------------------------------------------------- 6 红线
    line("6. 红线验证：整个流程没有产生任何发送")
    states = [x["state"] for x in
              s.conn.execute("SELECT state FROM outbox ORDER BY id").fetchall()]
    agent_msgs = s.conn.execute(
        "SELECT COUNT(*) FROM message WHERE sent_by='agent'").fetchone()[0]
    print(f"  outbox 全部状态：{states}")
    print(f"  sent_by='agent' 的消息数：{agent_msgs}   （应为 0）")
    print(f"  message 总数：{s.stats()['messages']}（只增加了收到的那些）")
    assert "sent" not in states and agent_msgs == 0, "出现了发送动作！"

    line("完成")
    print("  下一步可接：草稿推送（复用网关 notify.py）→ 你确认 → 才进入 approve 流程")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
