"""草稿生成：读到新消息 → 起草回复 → 入队。

**只入队，绝不发送。** 状态停在 `draft`，等人（或更高层策略）审批。

为什么要单独一层：
  - 选哪些消息值得回、什么时候回，是策略问题，不该散在 agent 主循环里
  - 起草质量可以单独评估（你只看草稿，不用承担发错的后果）
  - 幂等、限流、冷场判断集中在一处，好测

Composer 是可插拔的：`fn(context) -> str | None`。返回 None 表示"这条不值得回"。
本文件自带的 `template_composer` 只是**占位实现**，用来验证链路，回复质量不代表真实能力。
接真模型时换成你自己的 composer 即可：

    def my_composer(ctx):
        prompt = build_prompt(ctx)     # ctx 里有 conversation / message / recent / related
        return call_llm(prompt)

    run_once(store, my_composer)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import text as T

Composer = Callable[[dict], str | None]


@dataclass
class Policy:
    """起草策略。全部可调，默认值偏保守。"""

    max_age_s: int = 3600        # 太老的消息不再起草（默认 1 小时）
    min_gap_s: int = 30          # 同一会话两条草稿的最小间隔，防刷
    skip_groups: bool = True     # 群聊默认不起草（群发本来就被闸门拦住）
    only_unanswered: bool = True  # 已经被本人/agent 回过的消息不再起草
    max_drafts_per_run: int = 5  # 单轮最多起几条


# ------------------------------------------------------------------ 判定
def is_answered(store, conversation_key: str, msg: dict) -> bool:
    """这条消息之后，会话里有没有出现过「我」的发言（本人或 agent）。"""
    row = store.conn.execute(
        "SELECT 1 FROM message WHERE conversation_key=? AND ts_epoch > ?"
        " AND sent_by IN ('human','agent') AND id <> ? LIMIT 1",
        (conversation_key, msg["ts_epoch"], msg["id"])).fetchone()
    return row is not None


def _too_soon(store, conversation_key: str, gap_s: int) -> dict | None:
    row = store.conn.execute(
        "SELECT id, created_at FROM outbox WHERE conversation_key=? AND created_at >= ?"
        " ORDER BY id DESC LIMIT 1",
        (conversation_key, store._iso_ago(gap_s))).fetchone()
    return dict(row) if row else None


def find_candidates(store, conversation_key: str | None = None,
                    policy: Policy | None = None) -> list[dict]:
    """找出「该回但还没回、也没起草过」的消息。"""
    policy = policy or Policy()
    now = T.parse_ts(None)[1]
    sql = ("SELECT m.* FROM message m JOIN conversation c ON c.key = m.conversation_key"
           " WHERE m.sent_by IN ('other','unknown')"
           "   AND m.state <> 'vanished'"
           "   AND m.ts_epoch >= ?")
    args: list = [now - policy.max_age_s]
    if policy.skip_groups:
        sql += " AND c.kind <> 'group'"
    if conversation_key:
        sql += " AND m.conversation_key = ?"
        args.append(conversation_key)
    sql += " ORDER BY m.ts_epoch ASC"

    out: list[dict] = []
    for r in store.conn.execute(sql, args).fetchall():
        if store.outbox_for_message(r["id"]):
            continue                       # 同一条只起草一次
        if policy.only_unanswered and is_answered(store, r["conversation_key"], r):
            continue
        out.append(dict(r))
    return out


def build_context(store, msg: dict) -> dict:
    """给 composer 的素材：会话、这条消息、最近上下文、检索到的相关记忆。

    注意：`related` 走的是**子串检索**（见 README §6），找得到「同一句话的重复出现」，
    找不到「换个说法问同一件事」。真实语义召回要靠 v1.2 的向量/RRF，别在这里假装有。
    """
    conv = store.conn.execute(
        "SELECT * FROM conversation WHERE key=?", (msg["conversation_key"],)).fetchone()
    recent = store.recent(msg["conversation_key"], n=10)

    raw = (msg["text"] or "").strip().strip("?？!！。.~～ ")
    # 用有序去重而不是 set：set 的遍历顺序受哈希随机化影响，会让行为不可复现
    probes = list(dict.fromkeys(p for p in (raw[:60], raw[:8]) if p))
    related: list[dict] = []
    for probe in probes:
        if not T.fts_expr(probe):
            continue
        try:
            hits = store.search(probe, scope=msg["conversation_key"], k=3)
        except Exception:  # noqa: BLE001 —— 检索失败不该阻断起草
            hits = []
        # 必须先剔除「自己」：否则全文检索只会命中本条消息，
        # 命中列表非空导致 break，真正的相关历史反而被跳过
        hits = [h for h in hits if h["id"] != msg["id"]]
        if hits:
            related = hits
            break

    return {
        "conversation": dict(conv) if conv else None,
        "message": msg,
        "recent": recent,
        "related": related,
    }


# ------------------------------------------------------------------ 默认 composer
_QUESTION_HINTS = ("?", "？", "吗", "几点", "什么时候", "多少", "哪个", "要不要", "能不能", "是否")
_SCHEDULE_HINTS = ("改到", "调整", "延期", "提前", "推迟", "时间", "会议")
_THANKS_HINTS = ("谢谢", "感谢", "辛苦")


def template_composer(ctx: dict) -> str | None:
    """占位实现 —— 只为验证链路，别拿它当真实回复能力。"""
    text = (ctx["message"]["text"] or "").strip()
    if len(text) < 2:
        return None
    if any(k in text for k in _THANKS_HINTS):
        return "不客气。"
    if any(k in text for k in _QUESTION_HINTS):
        return "收到，我确认一下再回你。"
    if any(k in text for k in _SCHEDULE_HINTS):
        return "收到，时间上的变动我记下了，稍后跟你确认。"
    return "收到，我看一下。"


# ------------------------------------------------------------------ 主流程
def draft_for(store, msg: dict, composer: Composer | None = None) -> dict:
    """为单条消息起草并入队。返回结果摘要（含闸门判定）。"""
    composer = composer or template_composer
    ctx = build_context(store, msg)
    text = composer(ctx)
    if not text:
        return {"message_id": msg["id"], "drafted": False,
                "reason": "composer 判断这条不值得回"}

    res = store.enqueue_draft(msg["conversation_key"], text,
                              source_message_id=msg["id"])
    return {
        "message_id": msg["id"],
        "conversation_key": msg["conversation_key"],
        "sender": msg["sender_label"],
        "incoming": msg["text"],
        "drafted": True,
        "draft": text,
        "outbox_id": res["id"],
        "state": res["state"],
        "risk": res["risk"],
        "blocked_reason": res["reason"],
    }


def run_once(store, composer: Composer | None = None, policy: Policy | None = None,
             conversation_key: str | None = None) -> dict:
    """一轮起草。返回 {drafted: [...], skipped: [...], candidates: n}。"""
    policy = policy or Policy()
    cands = find_candidates(store, conversation_key, policy)

    drafted: list[dict] = []
    skipped: list[dict] = []
    handed = 0

    for msg in cands:
        if handed >= policy.max_drafts_per_run:
            skipped.append({"message_id": msg["id"], "reason": "达到单轮上限"})
            continue
        soon = _too_soon(store, msg["conversation_key"], policy.min_gap_s) \
            if policy.min_gap_s > 0 else None
        if soon:
            skipped.append({"message_id": msg["id"],
                            "reason": f"距上一条草稿不足 {policy.min_gap_s}s（outbox#{soon['id']}）"})
            continue
        r = draft_for(store, msg, composer)
        if r["drafted"]:
            handed += 1
            drafted.append(r)
        else:
            skipped.append({"message_id": msg["id"], "reason": r["reason"]})

    return {"candidates": len(cands), "drafted": drafted, "skipped": skipped}


def summarize(result: dict) -> str:
    """给日志/通知用的一行摘要。"""
    d = result["drafted"]
    ok = [x for x in d if x["risk"] == "normal"]
    blocked = [x for x in d if x["risk"] == "blocked"]
    return (f"候选 {result['candidates']} 条 → 起草 {len(d)} 条"
            f"（可审批 {len(ok)}，被拦 {len(blocked)}），"
            f"跳过 {len(result['skipped'])} 条")
