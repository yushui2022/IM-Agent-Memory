"""访问策略的测试。

守的是一条底线：**不该动手的时候一次都不许动**。
误触发一次的结果不是"多读了一点"，而是用户的未读标记被永久吃掉。
所以这里大部分用例都是"必须拒绝"的场景。

运行： python tests/test_plan.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import plan as P

Policy = P.SwitchPolicy


def sig(y: int, title: str = "", row: int = 0) -> dict:
    return {"y": y, "row": row, "title": title}


def titles(p) -> list[str]:
    return [v.title for v in p.visits]


# ------------------------------------------------------------------ 拒绝
def test_user_active_blocks_everything():
    p = P.plan([sig(10, "A")], policy=Policy(user_idle_s=60),
               now=1000, user_idle_s=5)
    assert not p
    assert p.reason.startswith("user_active")


def test_user_active_beats_everything_else():
    """人在打字的时候，再有未读也不许抢焦点。"""
    p = P.plan([sig(10, "A"), sig(20, "B")],
               policy=Policy(user_idle_s=60, max_per_cycle=9),
               now=1000, user_idle_s=0)
    assert not p


def test_unknown_idle_is_treated_as_unattended():
    """拿不到空闲时间就按无人值守走——这是有意的默认。"""
    p = P.plan([sig(10, "A")], policy=Policy(user_idle_s=60),
               now=1000, user_idle_s=None)
    assert len(p) == 1


def test_hourly_budget_blocks():
    p = P.plan([sig(10, "A")], policy=Policy(hourly_allowance=5),
               now=1000, user_idle_s=999, visits_this_hour=5)
    assert not p
    assert p.reason.startswith("hourly_budget")


def test_no_signals():
    p = P.plan([], policy=Policy(), now=1000, user_idle_s=999)
    assert not p
    assert p.reason == "no_unread"


def test_already_open_is_skipped():
    """已经打开的那个会话不用点——点了也没新信息，白吃一次已读。"""
    p = P.plan([sig(10, "A")], policy=Policy(), now=1000,
               user_idle_s=999, current_title="A")
    assert not p
    assert ("A", "already_open") in p.skipped


# ------------------------------------------------------------------ 顺序
def test_top_down_order():
    p = P.plan([sig(300, "C"), sig(100, "A"), sig(200, "B")],
               policy=Policy(max_per_cycle=5), now=1000, user_idle_s=999)
    assert titles(p) == ["A", "B", "C"]


def test_cycle_budget_caps_visits():
    p = P.plan([sig(10, "A"), sig(20, "B"), sig(30, "C")],
               policy=Policy(max_per_cycle=2), now=1000, user_idle_s=999)
    assert titles(p) == ["A", "B"]
    assert ("C", "cycle_budget") in p.skipped


def test_duplicate_title_visited_once():
    p = P.plan([sig(10, "A"), sig(20, "A")],
               policy=Policy(max_per_cycle=5), now=1000, user_idle_s=999)
    assert titles(p) == ["A"]
    assert ("A", "duplicate") in p.skipped


# ------------------------------------------------------------------ 冷却
def test_cooldown_skips_recent():
    p = P.plan([sig(10, "A")], policy=Policy(cooldown_min=30),
               now=1000, user_idle_s=999, last_visit={"A": 940})
    assert not p
    assert p.reason == "all_cooled_down"


def test_cooldown_expires():
    p = P.plan([sig(10, "A")], policy=Policy(cooldown_min=30),
               now=10000, user_idle_s=999, last_visit={"A": 0})
    assert titles(p) == ["A"]


def test_cooldown_does_not_block_unknown_titles():
    """还不知道名字的行没法比对冷却，只能放行——这是上游没读标题的代价。"""
    p = P.plan([sig(10, "")], policy=Policy(cooldown_min=30),
               now=1000, user_idle_s=999, last_visit={"A": 999})
    assert len(p) == 1


def test_cooled_down_is_distinct_from_no_unread():
    """两种"不切"要能区分：一个是真没事，一个是策略在压着。"""
    cooled = P.plan([sig(10, "A")], policy=Policy(cooldown_min=30),
                    now=1000, user_idle_s=999, last_visit={"A": 999})
    idle = P.plan([], policy=Policy(), now=1000, user_idle_s=999)
    assert cooled.reason != idle.reason


def test_record_visit_ignores_empty_title():
    d: dict[str, float] = {}
    P.record_visit(d, "", 123)
    assert d == {}
    P.record_visit(d, "A", 123)
    assert d == {"A": 123}


# ------------------------------------------------------------------ 表达
def test_explain_is_human_readable():
    p = P.plan([sig(10, "产品群")], policy=Policy(), now=1000, user_idle_s=999)
    text = p.explain()
    assert "产品群" in text
    assert "切 1 个" in text


def test_explain_when_idle():
    p = P.plan([], policy=Policy(), now=1000, user_idle_s=999)
    assert "不切" in p.explain()


def test_visit_carries_click_y():
    p = P.plan([sig(137, "A")], policy=Policy(), now=1000, user_idle_s=999)
    assert p.visits[0].y == 137
