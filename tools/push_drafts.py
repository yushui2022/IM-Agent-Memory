#!/usr/bin/env python3
"""把待审草稿推到你手机上，并在本机完成确认。

手机端不是我们做的：飞书 / Telegram / 邮件的 App 就是手机端，
我们只给机器人发一条消息，推送走它们自家的通道。

用法：
    # 只看（默认，不联网）
    python tools/push_drafts.py --db imem.db

    # 推到飞书（建群 → 加群机器人 → 复制 webhook 地址）
    export IMEM_FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'
    python tools/push_drafts.py --db imem.db --send

    # 确认第 1、3 条（只是置为 approved，**不发送**）
    python tools/push_drafts.py --db imem.db --approve 1 3

    # 作废第 2 条
    python tools/push_drafts.py --db imem.db --cancel 2

红线：approve 只把状态从 draft 改成 approved。真正的发送要另外在 UI 上执行，
      并用 ingest() 回读验证。这个脚本永远不会替你发消息。
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
    ap = argparse.ArgumentParser(description="推送并确认待审草稿")
    ap.add_argument("--db", default="imem.db", help="SQLite 路径")
    ap.add_argument("--send", action="store_true", help="真的发出去（默认只打印）")
    ap.add_argument("--approve", type=int, nargs="+", metavar="N", help="确认第 N 条")
    ap.add_argument("--cancel", type=int, nargs="+", metavar="N", help="作废第 N 条")
    ap.add_argument("--limit", type=int, default=8, help="消息里最多列几条")
    args = ap.parse_args()

    s = Store(args.db)
    pending = s.outbox_pending()

    if not pending:
        print("📭 没有待审草稿。")
        return 0

    # 编号固定按 id 升序，避免每次跑顺序不同导致确认错条目
    pending.sort(key=lambda r: r["id"])
    titles = {}
    incoming = {}
    for r in pending:
        if r["conversation_key"] not in titles:
            row = s.conn.execute("SELECT title FROM conversation WHERE key=?",
                                 (r["conversation_key"],)).fetchone()
            titles[r["conversation_key"]] = row["title"] if row else r["conversation_key"]
        src_id = r.get("source_message_id")
        if src_id and src_id not in incoming:
            row = s.conn.execute("SELECT text FROM message WHERE id=?", (src_id,)).fetchone()
            if row:
                incoming[src_id] = row["text"]

    # ---------------- 确认 / 作废
    if args.approve or args.cancel:
        for n in (args.approve or []):
            if not 1 <= n <= len(pending):
                print(f"  ✗ #{n} 不存在（共 {len(pending)} 条）")
                continue
            ob = pending[n - 1]
            ok = s.approve(ob["id"])
            print(f"  ✓ #{n} → approved（outbox#{ob['id']}）" if ok
                  else f"  ✗ #{n} 状态不是 draft，未处理")
        for n in (args.cancel or []):
            if not 1 <= n <= len(pending):
                print(f"  ✗ #{n} 不存在（共 {len(pending)} 条）")
                continue
            ob = pending[n - 1]
            ok = s.cancel(ob["id"])
            print(f"  ✓ #{n} → cancelled（outbox#{ob['id']}）" if ok
                  else f"  ✗ #{n} 状态不允许作废")
        print("\n提醒：approved 只是允许发送，**并没有发出去**。")
        print("     发送要在 UI 上执行，然后回读验证（message.sent_by 会变成 agent）。")
        return 0

    # ---------------- 渲染 + 推送
    text = notifier.render_drafts(pending, titles, max_items=args.limit,
                                  incoming=incoming,
                                  approve_hint="本机执行： python tools/push_drafts.py "
                                               "--approve N   （N 见编号）")
    print(text)
    print()

    if not args.send:
        print("（dry-run：没有发送。加 --send 才会真的推到你手机）")
        return 0

    n = notifier.Notifier()
    results = notifier.push(
        n, text,
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
