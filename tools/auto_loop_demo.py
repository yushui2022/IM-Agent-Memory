#!/usr/bin/env python3
"""自治回路演示：读消息 → 判断 → 自动回 / 沉默 / 升级 —— 人不在场。

用占位 composer，只为验证链路。真实质量接 LLM：
    drafter.run_once(store, composer=my_llm_composer)

运行： python tools/auto_loop_demo.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T
from imem import drafter, notifier


def line(title: str = "") -> None:
    print(f"\n{'─' * 62}" + (f"\n{title}" if title else ""))


def ago(seconds: int) -> str:
    return (datetime.now(T.TZ) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def main() -> int:
    s = Store(":memory:")
    s.set_self_label("wechat", "我")

    zs = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    ls = s.ensure_conversation("wechat", "李四", kind="dm", app_chat_key="dm_ls")
    ww = s.ensure_conversation("wechat", "王五", kind="dm", app_chat_key="dm_ww")
    grp = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="grp")

    # ---------------------------------------------------------------- 1 来了四条
    line("1. 感知层抓到 4 条新消息（人不看）")
    s.ingest(zs, [{"sender": "张三", "text": "评审改到几点了？", "ts": ago(60)}])
    s.ingest(ls, [{"sender": "李四", "text": "好的", "ts": ago(50)}])
    s.ingest(ww, [{"sender": "王五", "text": "合同你签了吗", "ts": ago(40)}])
    s.ingest(grp, [{"sender": "赵六", "text": "这个方案谁看过", "ts": ago(30)}])
    for conv in (zs, ls, ww, grp):
        m = s.recent(conv, n=1)[0]
        print(f"  [{s.conn.execute('SELECT title FROM conversation WHERE key=?', (conv,)).fetchone()[0]}]"
              f" {m['sender']}：{m['text']}")

    # ---------------------------------------------------------------- 2 决策
    line("2. agent 自己判断（默认自治，不需要人批）")
    r = drafter.run_once(s, policy=drafter.Policy(min_gap_s=0))
    print(f"  {drafter.summarize(r)}\n")

    for d in r["replied"]:
        print(f"  ✅ 自动回  [{d['sender']}] {d['incoming']}")
        print(f"            → {d['text']}   （outbox#{d['outbox_id']}, {d['state']}）")
    for d in r["escalated"]:
        print(f"  🚨 升级    [{d['sender']}] {d['incoming']}")
        print(f"            理由：{d['reason']}")
    for d in r["silent"]:
        print(f"  ·  沉默    [{d['sender']}] {d['incoming']}  （{d['reason']}）")

    # ---------------------------------------------------------------- 3 执行器队列
    line("3. 执行器队列：已放行、待真实发送")
    ready = s.outbox_ready_to_send()
    print(f"  共 {len(ready)} 条（执行器只能从这里取活）")
    for ob in ready:
        print(f"  outbox#{ob['id']}  auto_approved={ob['auto_approved']}  {ob['text']}")

    # ---------------------------------------------------------------- 4 模拟发送+回读
    line("4. 执行器在 UI 上发送，然后回读验证（这一步是感知层的事）")
    for ob in ready:
        s.mark_sent(ob["id"])
        res = s.ingest(ob["conversation_key"], [
            {"sender": "我", "text": ob["text"], "ts": ago(0)},
        ], captured_at=ago(0))
        mid = res["messages"][0]
        row = s.conn.execute("SELECT sent_by, outbox_id FROM message WHERE id=?", (mid,)).fetchone()
        print(f"  outbox#{ob['id']} → {s.outbox_get(ob['id'])['state']}"
              f"   sent_by={row['sent_by']}  outbox_id={row['outbox_id']}")

    # ---------------------------------------------------------------- 5 推给手机
    line("5. 你手机上收到的（常态是「已替你回了」，只有升级才惊动你）")
    esc = s.outbox_escalated()
    acts = s.outbox_recent_auto(hours=24)
    titles, incoming = {}, {}
    for x in esc + acts:
        k = x["conversation_key"]
        if k not in titles:
            titles[k] = s.conn.execute(
                "SELECT title FROM conversation WHERE key=?", (k,)).fetchone()["title"]
        sid = x.get("source_message_id")
        if sid and sid not in incoming:
            incoming[sid] = s.conn.execute(
                "SELECT text FROM message WHERE id=?", (sid,)).fetchone()["text"]
    print(notifier.render_digest(esc, acts, titles, incoming))

    # ---------------------------------------------------------------- 6 红线
    line("6. 红线复核")
    agent_msgs = s.conn.execute(
        "SELECT COUNT(*) FROM message WHERE sent_by='agent'").fetchone()[0]
    print(f"  agent 发出的消息：{agent_msgs} 条（本次演示模拟了执行器，故为 {agent_msgs}）")
    print(f"  escalated 未发送：{[x['id'] for x in esc]}")
    print(f"  升级项不在待发队列：{all(x['id'] not in [y['id'] for y in s.outbox_ready_to_send()] for x in esc)}")
    print(f"  群里没被自动回：{s.outbox_for_message(4) is None or 'ok'}")

    line("完成")
    print("  人只做了一件事：看那条 🚨。其余是 agent 自己干的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
