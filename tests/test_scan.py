"""未读角标检测的测试。

这一层如果误报，调度器就会一直在"没未读"的时候切会话——而每次切换
都会把用户的未读状态吃掉。所以这里守的其实是**误报率**：
宁可漏一个角标，也不能把橙色头像当成未读。

运行： python tests/test_scan.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import scan

W, H = 320, 200
RED = (250, 80, 80)

# 默认角标带是 (0.16,0.08,0.24,0.96) → x ∈ [51,77)
BAND_X = 60


# ------------------------------------------------------------------ 夹具
def blank(v: int = 250) -> bytearray:
    return bytearray([v]) * (W * H * 3)


def paint(buf: bytearray, x0: int, y0: int, x1: int, y1: int, c=RED) -> None:
    for y in range(y0, y1):
        base = y * W * 3
        for x in range(x0, x1):
            i = base + x * 3
            buf[i], buf[i + 1], buf[i + 2] = c


def frame(*rects) -> bytearray:
    b = blank()
    for r in rects:
        paint(b, *r)
    return b


def badge(y: int, x: int = BAND_X) -> tuple[int, int, int, int]:
    return (x, y, x + 12, y + 12)


def find(buf, **kw):
    return scan.find_red_badges(buf, W, H, **kw)


# ------------------------------------------------------------------ 判色
def test_is_red_accepts_wechat_badge():
    assert scan.is_red(250, 80, 80)
    assert scan.is_red(255, 60, 70)


def test_is_red_rejects_orange_avatar():
    """橙色头像 (255,165,0)：红通道确实远大于蓝，但绿也高——不是角标。"""
    assert not scan.is_red(255, 165, 0)


def test_is_red_rejects_pink_and_purple():
    assert not scan.is_red(255, 105, 180)      # 粉：蓝太高
    assert not scan.is_red(180, 90, 200)       # 紫：蓝高于绿太多


def test_is_red_rejects_gray_white_and_dark():
    assert not scan.is_red(150, 150, 150)
    assert not scan.is_red(255, 255, 255)
    assert not scan.is_red(139, 0, 0)          # 暗红，低于 floor


# ------------------------------------------------------------------ 检测
def test_no_badge_on_plain_frame():
    assert find(blank()) == []


def test_finds_single_badge():
    bs = find(frame(badge(30)))
    assert len(bs) == 1, [(b.x, b.y) for b in bs]
    b = bs[0]
    assert (b.x, b.y, b.w, b.h) == (BAND_X, 30, 12, 12)
    assert b.cx == BAND_X + 6
    assert b.cy == 36


def test_multiple_badges_sorted_top_down():
    bs = find(frame(badge(150), badge(30), badge(90)))
    assert [b.y for b in bs] == [30, 90, 150]


def test_ignores_orange_avatar_inside_band():
    assert find(frame((BAND_X, 30, BAND_X + 12, 42, (255, 165, 0)))) == []


def test_ignores_red_outside_the_badge_band():
    """左侧是头像区，那里再红也不算未读。"""
    assert find(frame((10, 30, 40, 60))) == []


def test_custom_area_widens_the_search():
    bs = find(frame((10, 30, 22, 42)), area=(0.0, 0.0, 0.30, 1.0))
    assert len(bs) == 1


def test_merges_badge_split_by_white_digit():
    """角标中间有白色数字，纯四邻接会把一个角标切成两块。"""
    b = frame((BAND_X, 30, BAND_X + 6, 42), (BAND_X + 7, 30, BAND_X + 12, 42))
    bs = find(b)
    assert len(bs) == 1, [(x.x, x.w) for x in bs]


def test_vertical_gap_keeps_badges_separate():
    """上下隔开 8px 就是两个角标，不能被 merge_gap 连成一个。"""
    assert len(find(frame(badge(30), badge(50)))) == 2


def test_two_rows_of_badges():
    assert len(find(frame(badge(30), badge(94)))) == 2


def test_filters_tiny_noise():
    """两三个红像素不算角标。"""
    assert find(frame((BAND_X, 30, BAND_X + 2, 32))) == []


def test_filters_oversized_red_block():
    """整片红不是角标（可能是一张红色图片）。"""
    assert find(frame((0, 0, 100, 60)), area=(0.0, 0.0, 1.0, 1.0)) == []


# ------------------------------------------------------------------ 归行
def test_unread_rows():
    bs = find(frame(badge(30), badge(110)))
    assert scan.unread_rows(bs) == [0, 1]


def test_row_index_respects_row_top():
    bs = find(frame(badge(100)))
    assert bs[0].row_index(64, 0) == 1
    assert bs[0].row_index(64, 64) == 0


# ------------------------------------------------------------------ 信号
def test_to_signals_shape():
    bs = find(frame(badge(30), badge(110)))
    sig = scan.to_signals(bs)
    assert [s["row"] for s in sig] == [0, 1]
    assert all(s["title"] == "" for s in sig)
    assert sig[0]["y"] < sig[1]["y"]


def test_to_signals_dedupes_same_row():
    """同一行叠了两个角标（未读 + 免打扰图标），只算一个信号。"""
    bs = find(frame(badge(30), badge(34, x=BAND_X + 14)))
    assert len(bs) == 2
    assert len(scan.to_signals(bs)) == 1


def test_has_unread():
    assert scan.has_unread(frame(badge(30)), W, H)
    assert not scan.has_unread(blank(), W, H)


# ------------------------------------------------------------------ 边界
def test_rejects_short_buffer():
    try:
        scan.find_red_badges(bytearray(10), W, H)
    except ValueError:
        return
    raise AssertionError("像素数不足时应当抛 ValueError")


def test_rejects_bad_size():
    try:
        scan.find_red_badges(bytearray(W * H * 3), 0, H)
    except ValueError:
        return
    raise AssertionError("宽高为 0 时应当抛 ValueError")


# ------------------------------------------------------------------ 行号缓存
def test_row_titles_fills_signals():
    t = scan.RowTitles()
    t.learn(0, "产品群")
    t.learn(1, "老王")
    out = t.apply(scan.to_signals(find(frame(badge(30), badge(94)))))
    assert [s["title"] for s in out] == ["产品群", "老王"]


def test_row_titles_keeps_unknown_rows_blank():
    t = scan.RowTitles()
    t.learn(1, "老王")
    out = t.apply(scan.to_signals(find(frame(badge(30), badge(94)))))
    assert [s["title"] for s in out] == ["", "老王"]


def test_row_titles_ignores_empty_learn():
    """读不到名字就不该覆盖已有认知。"""
    t = scan.RowTitles()
    t.learn(0, "产品群")
    t.learn(0, "")
    assert t.apply([{"row": 0, "y": 0}])[0]["title"] == "产品群"


def test_row_titles_invalidated_by_signature_change():
    """列表一动行号就全错位，宁可重新认一遍。"""
    t = scan.RowTitles()
    t.learn(0, "产品群")
    assert t.apply([{"row": 0, "y": 0}], signature=111)[0]["title"] == "产品群"
    assert t.apply([{"row": 0, "y": 0}], signature=222)[0]["title"] == ""


def test_row_titles_same_signature_keeps_cache():
    t = scan.RowTitles()
    t.learn(0, "产品群")
    assert t.apply([{"row": 0, "y": 0}], signature=111)[0]["title"] == "产品群"
    assert t.apply([{"row": 0, "y": 0}], signature=111)[0]["title"] == "产品群"
    assert len(t) == 1


def test_row_titles_does_not_mutate_input():
    t = scan.RowTitles()
    t.learn(0, "产品群")
    sig = [{"row": 0, "y": 0, "title": ""}]
    t.apply(sig)
    assert sig[0]["title"] == ""


def test_row_titles_clear():
    t = scan.RowTitles()
    t.learn(0, "产品群")
    t.clear()
    assert len(t) == 0
