"""起草与决策：读到新消息 → 判断 → 自治回复 / 保持沉默 / 升级给人。

**默认自治。** 目标是代替人，所以正常路径是"直接处理掉"，人只在
真正该由人拍板的时候被惊动（`escalated`）。

三条出口：
  reply      起草并按策略自动放行（`auto_approve`）→ 交给执行器发送
  silent     **大多数消息的正确答案**：确认语、表情、图片、不关我事
  escalate   agent 拿不准（涉及钱 / 合同 / 投诉 / 频次超限）→ 叫人

安全底座（不是审批环节，是自治的前提）：
  - 会话白名单：只有列入的会话才自治，群聊永久排除
  - 发送前 preflight 窗口校验，防切错窗口
  - 限流 + 去重 + 每日熔断
  - 发送后强制回读验证，`sent_by` 归因防死循环

`Composer` 仍是可插拔的 `fn(context) -> str | None`。
内置 `template_composer` 只是**占位实现**，用来验证链路；真实质量请接 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import text as T

Composer = Callable[[dict], str | None]

# 命中这些词就别自作主张——钱、合同、投诉、法律、催办
DEFAULT_ESCALATE_HINTS: tuple[str, ...] = (
    "合同", "金额", "价格", "报价", "付款", "转账", "退款", "发票", "报销",
    "投诉", "律师", "法务", "起诉", "违约", "赔偿",
    "急", "马上", "立刻", "尽快", "今天必须",
    "领导", "老板", "甲方", "客户说",
)

# 纯确认语：回一句反而奇怪
ACK_ONLY: tuple[str, ...] = (
    "收到", "好的", "好", "嗯", "哦", "喔", "ok", "okay", "谢谢", "感谢",
    "辛苦了", "哈哈", "哈", "笑死", "+1", "1", "看到了", "知道了", "明白",
    "👍", "👌", "🙏", "嗯嗯", "好的呢", "行", "可以",
)

# 需要回应的信号
REPLY_HINTS: tuple[str, ...] = (
    "?", "？", "吗", "呢", "几点", "什么时候", "多少", "哪", "谁",
    "要不要", "能不能", "可不可以", "是不是", "有没有",
    "帮我", "麻烦", "请问", "发一下", "看下", "看一下", "确认",
    "怎么", "为什么", "如何",
)


@dataclass
class Policy:
    """决策与安全策略。默认值按"能自治就自治"来给。"""

    # --- 候选筛选
    max_age_s: int = 3600          # 太老的消息不再处理
    min_gap_s: int = 30            # 同会话两条处理的最小间隔（<=0 不限）
    skip_groups: bool = True       # 群聊永久排除（群发是公开事故）
    only_unanswered: bool = True   # 已被本人/agent 回过的跳过
    max_per_run: int = 5           # 单轮上限

    # --- 自治
    auto_approve: bool = True      # 起草后自动放行，不需要人批
    scope_allowlist: list[str] | None = None
    """None = 不限制（私聊全放开）；[] = 全部禁用；[conv...] = 仅这些会话自治"""

    # --- 升级
    escalate_hints: tuple[str, ...] = DEFAULT_ESCALATE_HINTS
    max_auto_per_day: int = 20     # 单会话每日自动回复上限（熔断）

    # --- 兼容旧名
    @property
    def max_drafts_per_run(self) -> int:
        return self.max_per_run


# ------------------------------------------------------------------ 判断
def is_answered(store, conversation_key: str, msg: dict) -> bool:
    row = store.conn.execute(
        "SELECT 1 FROM message WHERE conversation_key=? AND ts_epoch > ?"
        " AND sent_by IN ('human','agent') AND id <> ? LIMIT 1",
        (conversation_key, msg["ts_epoch"], msg["id"])).fetchone()
    return row is not None


def should_reply(text: str) -> bool:
    """这条消息值得回吗？**默认不回。**

    宁可沉默也不要噪音——一个什么都说"收到"的助手比不说话更烦人。
    """
    body = (text or "").strip()
    if len(body) < 2:
        return False
    norm = T.normalize(body)
    if norm in {T.normalize(x) for x in ACK_ONLY}:
        return False
    if body.startswith("[") and body.endswith("]"):   # [图片] [语音] [文件]
        return False
    return any(h in body for h in REPLY_HINTS)


def _too_soon(store, conversation_key: str, gap_s: int) -> dict | None:
    if gap_s <= 0:
        return None
    row = store.conn.execute(
        "SELECT id, created_at FROM outbox WHERE conversation_key=? AND created_at >= ?"
        " ORDER BY id DESC LIMIT 1",
        (conversation_key, store._iso_ago(gap_s))).fetchone()
    return dict(row) if row else None


def _auto_today(store, conversation_key: str) -> int:
    row = store.conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE conversation_key=? AND auto_approved=1"
        " AND created_at >= ?",
        (conversation_key, store._iso_ago(86400))).fetchone()
    return int(row[0])


def _allowed(conv_key: str, policy: Policy) -> bool:
    if policy.scope_allowlist is None:
        return True
    return conv_key in policy.scope_allowlist


# ------------------------------------------------------------------ 候选
def find_candidates(store, conversation_key: str | None = None,
                    policy: Policy | None = None) -> list[dict]:
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
            continue
        if policy.only_unanswered and is_answered(store, r["conversation_key"], r):
            continue
        out.append(dict(r))
    return out


def build_context(store, msg: dict) -> dict:
    """给 composer 的素材：会话、这条消息、最近上下文、检索到的相关记忆。

    `related` 走**子串检索**，只能召回"同一句话的重复出现"，
    换种说法问同一件事召回不到——语义召回是 v1.2 的事，别在这里假装有。
    """
    conv = store.conn.execute(
        "SELECT * FROM conversation WHERE key=?", (msg["conversation_key"],)).fetchone()
    recent = store.recent(msg["conversation_key"], n=10)

    raw = (msg["text"] or "").strip().strip("?？!！。.~～ ")
    probes = list(dict.fromkeys(p for p in (raw[:60], raw[:8]) if p))
    related: list[dict] = []
    for probe in probes:
        if not T.fts_expr(probe):
            continue
        try:
            hits = store.search(probe, scope=msg["conversation_key"], k=3)
        except Exception:  # noqa: BLE001
            hits = []
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
_Q = ("?", "？", "吗", "几点", "什么时候", "多少", "哪", "要不要", "能不能", "是否")
_S = ("改到", "调整", "延期", "提前", "推迟", "时间", "会议")
_THX = ("谢谢", "感谢", "辛苦")


def template_composer(ctx: dict) -> str | None:
    """占位实现 —— 只为验证链路，别拿它当真实回复能力。"""
    text = (ctx["message"]["text"] or "").strip()
    if len(text) < 2:
        return None
    if any(k in text for k in _THX):
        return "不客气。"
    if any(k in text for k in _Q):
        return "收到，我确认一下再回你。"
    if any(k in text for k in _S):
        return "收到，时间上的变动我记下了。"
    return "收到。"


# ------------------------------------------------------------------ 决策
def decide(store, msg: dict, composer: Composer | None = None,
           policy: Policy | None = None) -> dict:
    """单条消息的处理决定。返回 {action: reply|silent|escalate, ...}"""
    policy = policy or Policy()
    composer = composer or template_composer
    conv_key = msg["conversation_key"]
    body = msg["text"] or ""

    # 1) 不在白名单 → 沉默（不是我该管的会话）
    if not _allowed(conv_key, policy):
        return {"action": "silent", "reason": "会话未列入自治白名单"}

    # 2) 超纲关键词 → 升级（优先于起草：这类不该自作主张）
    hit = next((h for h in policy.escalate_hints if h in body), None)
    if hit:
        return {"action": "escalate", "reason": f"涉及「{hit}」，应由本人判断"}

    # 3) 日内熔断
    if policy.auto_approve and _auto_today(store, conv_key) >= policy.max_auto_per_day:
        return {"action": "escalate",
                "reason": f"该会话今日自动回复已达 {policy.max_auto_per_day} 条上限"}

    # 4) 不值得回 → 沉默
    if not should_reply(body):
        return {"action": "silent", "reason": "不需要回应（确认语/过短/纯附件）"}

    # 5) 起草
    draft = composer(build_context(store, msg))
    if not draft:
        return {"action": "silent", "reason": "composer 判断不值得回"}
    return {"action": "reply", "text": draft}


def run_once(store, composer: Composer | None = None, policy: Policy | None = None,
             conversation_key: str | None = None) -> dict:
    """跑一轮。**正常路径不需要人类参与。**

    返回 {candidates, replied, escalated, silent, blocked}
    """
    policy = policy or Policy()
    cands = find_candidates(store, conversation_key, policy)

    replied: list[dict] = []
    escalated: list[dict] = []
    silent: list[dict] = []
    blocked: list[dict] = []
    handed = 0

    for msg in cands:
        base = {"message_id": msg["id"], "conversation_key": msg["conversation_key"],
                "sender": msg["sender_label"], "incoming": msg["text"]}

        if handed >= policy.max_per_run:
            silent.append({**base, "reason": "达到单轮上限"})
            continue

        soon = _too_soon(store, msg["conversation_key"], policy.min_gap_s)
        if soon:
            silent.append({**base, "reason": f"距上一条处理不足 {policy.min_gap_s}s"})
            continue

        d = decide(store, msg, composer, policy)

        if d["action"] == "escalate":
            r = store.escalate(msg["conversation_key"], d["reason"], source_message_id=msg["id"])
            escalated.append({**base, "outbox_id": r["id"], "reason": d["reason"]})
            handed += 1
            continue

        if d["action"] == "silent":
            silent.append({**base, "reason": d["reason"]})
            continue

        res = store.enqueue_draft(
            msg["conversation_key"], d["text"],
            source_message_id=msg["id"],
            auto_approve=policy.auto_approve)
        if res["risk"] == "blocked":
            blocked.append({**base, "outbox_id": res["id"], "reason": res["reason"]})
            continue

        replied.append({**base, "outbox_id": res["id"], "state": res["state"],
                        "text": d["text"]})
        handed += 1

    return {"candidates": len(cands), "replied": replied, "escalated": escalated,
            "silent": silent, "blocked": blocked}


def summarize(result: dict) -> str:
    return (f"候选 {result['candidates']} 条 → "
            f"自动回复 {len(result['replied'])}、升级人工 {len(result['escalated'])}、"
            f"沉默 {len(result['silent'])}、被拦 {len(result['blocked'])}")
