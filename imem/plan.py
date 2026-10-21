"""访问策略：什么时候可以抢屏幕，抢谁的。

这个模块存在的唯一理由，是那个不能忽略的副作用：

    在微信里点进一个会话，就等于把它标成已读。

所以"切换会话"不是一次中性的读取，它**会改变用户的未读状态**。
一个只顾着刷全量的 agent 会让你的会话列表永远是空的——信息全都进了库，
但你再也分不清哪些是自己看过的、哪些是它替你吞掉的。

由此得出两条硬规则，写死在策略里：

  1. **人在用电脑的时候不许动。** 抢焦点是所有做法里最招人烦的一种，
     而且会打断用户正在打的字。用 `user_idle_s` 卡住。
  2. **有预算。** 每轮上限 + 每小时总上限。宁可漏几条，不可刷不停。

剩下的才是"谁先谁后"：从列表顶部往下（微信里越靠上 = 消息越新），
同一会话有冷却，已经打开的那个不用点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class SwitchPolicy:
    """调度预算。默认值偏保守——先让它少动作，再逐步放开。"""

    # 一轮（一次 tick）最多切进几个会话
    max_per_cycle: int = 3
    # 同一个会话多久内不重复切
    cooldown_min: int = 30
    # 用户多少秒没动键鼠才敢动手；None = 不检查（无人值守模式）
    user_idle_s: int | None = 60
    # 每小时总切换次数上限，防抖动
    hourly_allowance: int = 20
    # 切完是否回到原来那个会话
    restore_after: bool = True


@dataclass(frozen=True)
class Visit:
    """一次计划中的切换。`y` 用来点，`title` 用来核对。"""

    y: int
    row: int
    title: str
    reason: str


@dataclass
class Plan:
    visits: list[Visit] = field(default_factory=list)
    # (会话名或行号, 跳过原因)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.visits)

    def __len__(self) -> int:
        return len(self.visits)

    def explain(self) -> str:
        if not self.visits:
            return f"不切（{self.reason}）"
        names = "、".join(v.title or f"行{v.row}" for v in self.visits)
        tail = f"；跳过 {len(self.skipped)} 个" if self.skipped else ""
        return f"切 {len(self.visits)} 个：{names}（{self.reason}）{tail}"


def plan(signals: Sequence[Mapping] | Iterable[Mapping], *,
         policy: SwitchPolicy,
         now: float,
         last_visit: Mapping[str, float] | None = None,
         user_idle_s: float | None = None,
         visits_this_hour: int = 0,
         current_title: str = "") -> Plan:
    """把"哪些会话有未读"变成"这一轮该点哪几个"。

    `signals` 来自 `scan.to_signals()`，每项至少有 {row, y, title}。
    `last_visit` 是 会话名 → 上次切过去的时间戳（秒）。
    `user_idle_s` 是用户空闲秒数；传 None 表示无从得知，按无人值守处理。
    """
    rows = list(signals or [])

    # 1) 人在键盘前面 —— 一律不动
    if (policy.user_idle_s is not None and user_idle_s is not None
            and user_idle_s < policy.user_idle_s):
        return Plan(reason=f"user_active(空闲 {int(user_idle_s)}s)")

    # 2) 这一小时已经切够了
    if visits_this_hour >= policy.hourly_allowance:
        return Plan(reason=f"hourly_budget(已切 {visits_this_hour} 次)")

    if not rows:
        return Plan(reason="no_unread")

    cooldown_s = policy.cooldown_min * 60
    seen_titles: set[str] = set()
    visits: list[Visit] = []
    skipped: list[tuple[str, str]] = []

    for sig in sorted(rows, key=lambda s: s.get("y", 0)):
        title = str(sig.get("title") or "")
        row = int(sig.get("row", 0))
        label = title or f"行{row}"

        if title and title == current_title:
            skipped.append((label, "already_open"))
            continue

        if title and last_visit:
            last = last_visit.get(title)
            if last is not None and (now - last) < cooldown_s:
                skipped.append((label, "cooldown"))
                continue

        if title and title in seen_titles:
            skipped.append((label, "duplicate"))
            continue

        if len(visits) >= policy.max_per_cycle:
            skipped.append((label, "cycle_budget"))
            continue

        if title:
            seen_titles.add(title)
        visits.append(Visit(y=int(sig.get("y", 0)), row=row,
                            title=title, reason="unread"))

    if not visits:
        # 全是冷却/已读，和"根本没有未读"要区分开——前者说明策略在起作用
        reason = "all_cooled_down" if skipped else "no_unread"
        return Plan(visits=[], skipped=skipped, reason=reason)

    return Plan(visits=visits, skipped=skipped,
                reason=f"top_down(上限 {policy.max_per_cycle})")


def record_visit(last_visit: dict[str, float], title: str, now: float) -> None:
    """把一次成功切换记进冷却表。标题为空的不记（下次没法认它）。"""
    if title:
        last_visit[title] = now
