#!/usr/bin/env python3
"""端到端演示：合成帧 → 帧闸门 → 多模态抽取 → 入库 → 检索。

不需要真微信，也不需要真模型。这里要证明的只有一件事：

    「画面变了才花钱，变了就必须记下来，记下来就能查得到」

跑法：
    python tools/collect_demo.py
    python tools/collect_demo.py --db demo.db      # 落盘，方便自己翻
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, extract, pipeline, vision

W, H, TILE = 320, 200, 16
ROWS, COLS = H // TILE, W // TILE


# ---------------------------------------------------------------- 造画面
def blank() -> bytearray:
    return bytearray([240]) * (W * H)


def draw_line(buf: bytearray, n: int) -> None:
    """在消息区画一条"新消息"横条。n 从 0 开始，最多 3 条。"""
    row0 = 2 + n * 2
    for y in range(row0 * TILE, (row0 + 2) * TILE):
        base = y * W
        for x in range(5 * TILE, 18 * TILE):
            buf[base + x] = 40


def line(sender: str, text: str) -> dict:
    return {"sender": sender, "text": text, "ts": None,
            "msg_type": "text", "confidence": 0.88}


# ---------------------------------------------------------------- 假模型
class ScriptedModel:
    """按调用次数返回预置结果，模拟一个"看懂了"的多模态模型。"""

    def __init__(self, outputs: list[list[dict]]):
        self.outputs = outputs
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, images: list[bytes], prompt: str) -> str:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return json.dumps({"messages": self.outputs[idx], "notes": ""},
                          ensure_ascii=False)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=":memory:", help="sqlite 路径，默认内存")
    args = ap.parse_args()

    store = Store(args.db)
    conv = store.ensure_conversation("wechat", "产品群", kind="group",
                                     app_chat_key="111@chatroom")

    model = ScriptedModel([
        [],                                            # 首次触发：空窗口
        [line("老王", "方案明天再看")],                  # 第一条进来
        [line("老王", "方案明天再看"), line("小李", "收到")],
        [line("老王", "方案明天再看"), line("小李", "收到")],
    ])
    extractor = extract.CallableExtractor(model)

    gate = vision.Gate(tile=TILE)
    screen = blank()
    known: list[dict] = []          # 已入库的消息，喂给模型做增量
    total_calls = 0

    script = [
        ("冷启动，空白窗口", None),
        ("老王发来一条", lambda b: draw_line(b, 0)),
        ("同样的画面，再抓一次", None),
        ("小李回了一句", lambda b: draw_line(b, 1)),
        ("又抓了一次，没变化", None),
    ]

    print("步骤｜画面｜闸门｜模型调用｜入库｜升级确认")
    print("-" * 68)

    for step, (desc, mutate) in enumerate(script, 1):
        if mutate:
            mutate(screen)

        verdict = gate.push(bytes(screen), W, H)
        called = 0
        inserted = 0
        confirmed = 0

        if verdict.trigger:
            req = extract.ExtractRequest(
                app="wechat", window_title="产品群",
                frames=[bytes(screen)], captured_at=f"2026-10-21T14:3{step}:00",
                previous=known,
            )
            result = extractor.extract(req)
            called = 1
            total_calls += 1

            stats = pipeline.commit(
                store, conv, result, captured_at=f"2026-10-21T14:3{step}:00")
            inserted = stats["inserted"]
            confirmed = stats["confirmed"]

            for draft, mid in zip(result.messages, stats["messages"]):
                known.append({"message_id": mid, "sender": draft["sender"],
                              "text": draft["text"]})

        print(f"{step:>3} ｜{desc:<18}｜{verdict.reason:<18}｜"
              f"{called:^8}｜{inserted:^4}｜{confirmed:^4}")

    print()
    print("=== 记忆里到底有什么 ===")
    for m in reversed(store.recent(conv, n=10)):
        print(f"  #{m['id']} [{m['sender']}] {m['text']}  ({m['state']}, ts_conf={m['ts_conf']})")

    print()
    print("=== 检索「方案」 ===")
    for hit in store.search("方案", scope=conv):
        print(f"  #{hit['id']} {hit['sender']}: {hit['text']}  score={hit['_score']}")

    stats = store.stats()
    print()
    stats = store.stats()
    print()
    print(f"消息 {stats['messages']} 条｜模型被调用 {total_calls} 次"
          f"（共 {len(script)} 次抓屏，省下 {len(script) - total_calls} 次）")

    if args.db != ":memory:":
        print(f"\n库已落盘：{os.path.abspath(args.db)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    raise SystemExit(main())
