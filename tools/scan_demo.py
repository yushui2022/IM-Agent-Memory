#!/usr/bin/env python3
"""会话切换调度演示：同步合成画布 → 角标 → 计划 → 切换 → 核对 → 入库。

不需要真微信，也不需要真模型。要证明三件事：

    1. 只在"有未读 + 用户空闲 + 没超预算"时才动手
    2. 切过去之后，会话身份以**标题栏读到的**为准，不是以"我点了第几行"为准
    3. 核对不上就整批丢弃——一条都不写

跑法：
    python tools/scan_demo.py
    python tools/scan_demo.py --db scan.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, extract, pipeline, plan, scan, vision

W, H = 320, 200
ROW_H = 64
RED = (250, 80, 80)
BG = 250

# 会话列表里"有未读"的三行，从上到下
BADGE_Y = [30, 94, 158]


# ---------------------------------------------------------------- 造画面
def blank() -> bytearray:
    return bytearray([BG]) * (W * H * 3)


def paint(buf, x0, y0, x1, y1, c) -> None:
    for y in range(y0, y1):
        base = y * W * 3
        for x in range(x0, x1):
            i = base + x * 3
            buf[i], buf[i + 1], buf[i + 2] = c


def screens_with_badges(rows) -> bytearray:
    buf = blank()
    for r in rows:
        y = BADGE_Y[r]
        paint(buf, 60, y, 72, y + 12, RED)
    return buf


def list_sig(frame) -> int:
    """会话列表区域的粗指纹。列表一动（新消息顶上来、滚动），行号就作废。"""
    gray = bytes((frame[i] + frame[i + 1] + frame[i + 2]) // 3
                 for i in range(0, len(frame), 3))
    return vision.signature(vision.to_grid(gray, W, H, tile=8))


# ---------------------------------------------------------------- 假模型
def model_says(conversation: str, messages: list[tuple[str, str]]) -> str:
    return json.dumps({
        "conversation": conversation,
        "messages": [{"sender": s, "text": t, "ts": None,
                      "msg_type": "text", "confidence": 0.9}
                     for s, t in messages],
        "notes": "",
    }, ensure_ascii=False)


# 点第 r 行之后，屏幕上真正打开的是什么（模拟"切成功了"）
LANDED = {
    0: ("产品群", [("老王", "方案明天再看"), ("小李", "收到")]),
    1: ("老王", [("老王", "那份报价我改好了")]),
    2: ("家人群", [("妈", "周末回来吃饭吗")]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=":memory:")
    args = ap.parse_args()

    store = Store(args.db)
    policy = plan.SwitchPolicy(max_per_cycle=3, cooldown_min=30,
                               user_idle_s=60, hourly_allowance=20)
    last_visit: dict[str, float] = {}
    now = 1_000_000.0
    titles = scan.RowTitles()

    # ---------------- 第一轮：发现未读 ----------------
    print("=== 第 1 轮：看着屏幕上有 3 个红角标 ===")
    frame = screens_with_badges([0, 1, 2])
    badges = scan.find_red_badges(frame, W, H)
    signals = scan.to_signals(badges, row_h=ROW_H)

    print(f"  角标 {len(badges)} 个 → 信号 {len(signals)} 个："
          f"{[(s['row'], s['y']) for s in signals]}")

    p = plan.plan(signals, policy=policy, now=now, last_visit=last_visit,
                  user_idle_s=600)
    print(f"  计划：{p.explain()}")

    for visit in p.visits:
        # 首次接触：还不知道名字，身份以标题栏读到的为准
        result = extract.parse_model_output(model_says(*LANDED[visit.row]))
        conv = store.ensure_conversation("wechat", result.conversation, kind="unknown")
        stats = pipeline.commit(store, conv, result, captured_at="2026-10-21T14:40:00")
        plan.record_visit(last_visit, result.conversation, now)
        titles.learn(visit.row, result.conversation)   # 记住"这一行是谁"
        print(f"    行{visit.row} → 「{result.conversation}」"
              f" 入库 {stats['inserted']} 条")

    # ---------------- 第二轮：用户正在打字 ----------------
    print()
    print("=== 第 2 轮：用户正在敲键盘 ===")
    p = plan.plan(signals, policy=policy, now=now + 5,
                  last_visit=last_visit, user_idle_s=3)
    print(f"  计划：{p.explain()}   ← 抢焦点会打断人，一律不动")

    # ---------------- 第三轮：冷却期内 ----------------
    print()
    print("=== 第 3 轮：30 秒后又看到同样的角标（行号已知）===")
    known = titles.apply(signals, signature=list_sig(frame))
    print(f"  补出名字：{[(s['row'], s['title']) for s in known]}")
    p = plan.plan(known, policy=policy, now=now + 30,
                  last_visit=last_visit, user_idle_s=600)
    print(f"  计划：{p.explain()}   ← 冷却期内，同样的角标不再重切")

    # ---------------- 第四轮：身份核对 ----------------
    print()
    print("=== 第 4 轮：点进去但屏幕没刷新，标题栏还是上一个会话 ===")
    stale = extract.parse_model_output(
        model_says("产品群", [("老王", "方案明天再看")]))   # 读回的是旧标题
    stats = pipeline.commit(store, conv, stale,
                            captured_at="2026-10-21T14:45:00",
                            expect_conversation="老王")
    print(f"  结果：rejected={stats.get('rejected')}  入库 {stats['inserted']} 条")
    print(f"  说明：{stats['warnings'][0]}")

    # ---------------- 盘点 ----------------
    print()
    print("=== 记忆里到底有什么 ===")
    for row in store.coverage():
        n = store.recent(row["key"], n=10)
        print(f"  「{row['title']}」{row['n']} 条")
        for m in reversed(n):
            print(f"      [{m['sender']}] {m['text']}")

    st = store.stats()
    print()
    print(f"会话 {st['conversations']} 个｜消息 {st['messages']} 条")
    print("切换被拒绝 1 次（身份对不上）——这条数据没有写进任何一个会话。")

    if args.db != ":memory:":
        print(f"\n库已落盘：{os.path.abspath(args.db)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
