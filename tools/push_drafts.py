#!/usr/bin/env python3
"""把 agent 的动向推到你手机上：需要你介入的 + 已替你处理的。

默认自治，所以这条消息的常态是「已替你回了 N 条」（看过就行），
只有 agent 拿不准时才出现「🚨 需要你介入」。

手机端不是我们做的：飞书 / Telegram / 邮件的 App 就是手机端。

用法：
    # 只看（默认，不联网）
    python tools/push_drafts.py --db imem.db

    # 推到飞书（建群 → 加群机器人 → 复制 webhook 地址）
    export IMEM_FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
    python tools/push_drafts.py --db imem.db --send

    # 处理升级项（人拍板后关掉）
    python tools/push_drafts.py --db imem.db --resolve 1

    # 手动起草留下的 draft 项仍可放行/作废
    python tools/push_drafts.py --db imem.db --approve 1 3
    python tools/push_drafts.py --db imem.db --cancel 2

红线：approve 只改状态，**本脚本永远不会替你发消息**。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store
from imem import notifier


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def smtp_cfg_from_env() -> dict | None:
    host = env("IMEM_SMTP_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(env("IMEM_SMTP_PORT", "465")),
        "user": env("IMEM_SMTP_USER"),
        "password": env("IMEM_SMTP_PASS"),
        "to": env("IMEM_SMTP_TO"),
        "use_ssl": env("IMEM_SMTP_SSL", "1") not in ("0", "false", "False"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="推送 agent 动向并处理升级项")
    ap.add_argument("--db", default="imem.db")
    ap.add_argument("--send", action="store_true", help="真的发出去（默认只打印）")
    ap.add_argument("--hours", type=int, default=24, help="「已替你处理」回溯多少小时")
    ap.add_argument("--resolve", type=int, nargs="+", metavar="N", help="关掉第 N 条升级项")
    ap.add_argument("--approve", type=int, nargs="+", metavar="N", help="放行第 N 条手写草稿")
    ap.add_argument("--cancel", type=int, nargs="+", metavar="N", help="作废第 N 条手写草稿")
    args = ap.parse_args()

    s = Store(args.db)

    # 编号固定按 id 升序，避免每次顺序不同导致点错条目
    escalations = sorted(s.outbox_escalated(), key=lambda r: r["id"])
    drafts = sorted(s.outbox_pending(), key=lambda r: r["id"])
    drafts = [d for d in drafts if d["state"] == "draft"]
    actions = sorted(s.outbox_recent_auto(args.hours), key=lambda r: r["id"])

    titles: dict[str, str] = {}
    incoming: dict[int, str] = {}
    for r in escalations + drafts + actions:
        key = r["conversation_key"]
        if key not in titles:
            row = s.conn.execute("SELECT title FROM conversation WHERE key=?", (key,)).fetchone()
            titles[key] = row["title"] if row else key
        src_id = r.get("source_message_id")
        if src_id and src_id not in incoming:
            row = s.conn.execute("SELECT text FROM message WHERE id=?", (src_id,)).fetchone()
            if row:
                incoming[src_id] = row["text"]

    # ---------------- 人工处理
    if args.resolve or args.approve or args.cancel:
        for n in (args.resolve or []):
            if not 1 <= n <= len(escalations):
                print(f"  ✗ 升级项 #{n} 不存在（共 {len(escalations)} 条）")
                continue
            ob = escalations[n - 1]
            ok = s.resolve_escalation(ob["id"])
            print(f"  ✓ 升级项 #{n} 已关闭（outbox#{ob['id']}）" if ok else f"  ✗ #{n} 状态不符")
        for n in (args.approve or []):
            if not 1 <= n <= len(drafts):
                print(f"  ✗ 草稿 #{n} 不存在（共 {len(drafts)} 条）")
                continue
            ob = drafts[n - 1]
            ok = s.approve(ob["id"])
            print(f"  ✓ 草稿 #{n} → approved（outbox#{ob['id']}）" if ok else f"  ✗ #{n} 不能放行")
        for n in (args.cancel or []):
            if not 1 <= n <= len(drafts):
                print(f"  ✗ 草稿 #{n} 不存在（共 {len(drafts)} 条）")
                continue
            ob = drafts[n - 1]
            ok = s.cancel(ob["id"])
            print(f"  ✓ 草稿 #{n} → cancelled（outbox#{ob['id']}）" if ok else f"  ✗ #{n} 不能作废")
        print("\n提醒：approved 只是放行，**没有发出去**。发送由执行器在 UI 上做，再回读验证。")
        return 0

    # ---------------- 渲染 + 推送
    if drafts:
        text = notifier.render_drafts(drafts, titles, incoming=incoming,
                                      approve_hint="放行： python tools/push_drafts.py "
                                                   "--approve N")
    else:
        text = notifier.render_digest(escalations, actions, titles, incoming)

    if not text:
        print("📭 没有需要你介入的事，最近也没有自动处理的记录。")
        return 0

    print(text)
    print()
    if not args.send:
        print("（dry-run：没有发送。加 --send 才会真的推到你手机）")
        return 0

    results = notifier.push(
        notifier.Notifier(), text,
        feishu_url=env("IMEM_FEISHU_WEBHOOK"),
        tg_token=env("IMEM_TG_TOKEN"),
        tg_chat=env("IMEM_TG_CHAT"),
        smtp_cfg=smtp_cfg_from_env(),
    )
    for r in results:
        mark = "✓" if r.get("ok") else "✗"
        detail = r.get("error") or f"HTTP {r.get('status')}"
        print(f"  {mark} {r['channel']}: {detail}")
    return 0 if any(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
