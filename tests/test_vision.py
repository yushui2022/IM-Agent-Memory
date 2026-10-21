"""帧闸门的行为测试。

这些测试守的是一条成本线：**不该送模型的帧，绝不能送出去。**
每个用例对应一类真实噪声（光标闪、自己打字、切会话、分辨率变化）。

运行： python tests/test_vision.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import vision

W, H, TILE = 320, 200, 16
ROWS, COLS = H // TILE, W // TILE          # 12 x 20


# ------------------------------------------------------------------ 夹具
def frame(value: int = 240) -> bytearray:
    """一整帧纯色灰度图。"""
    return bytearray([value]) * (W * H)


def paint(buf: bytearray, x0: int, y0: int, x1: int, y1: int, value: int = 10) -> None:
    for y in range(y0, y1):
        base = y * W
        for x in range(x0, x1):
            buf[base + x] = value


def tiles(row0: int, row1: int, col0: int, col1: int) -> set[tuple[int, int]]:
    return {(r, c) for r in range(row0, row1) for c in range(col0, col1)}


def gate(**kw) -> vision.Gate:
    kw.setdefault("tile", TILE)
    return vision.Gate(**kw)


def grid(buf) -> list[list[int]]:
    return vision.to_grid(buf, W, H, tile=TILE)


# ------------------------------------------------------------------ 降采样
def test_grid_shape_and_pooling():
    g = grid(frame())
    assert len(g) == ROWS, len(g)
    assert len(g[0]) == COLS, len(g[0])
    # 纯色图降采样后每一格都必须还是那个值
    assert {v for row in g for v in row} == {240}


def test_grid_rejects_short_buffer():
    try:
        vision.to_grid(bytearray(10), W, H, tile=TILE)
    except ValueError:
        return
    raise AssertionError("像素数不足时应当抛 ValueError")


def test_grid_rejects_too_small_frame():
    try:
        vision.to_grid(bytearray(4), 2, 2, tile=16)
    except ValueError:
        return
    raise AssertionError("画面小于一格时应当抛 ValueError")


# ------------------------------------------------------------------ 差分
def test_grid_diff_finds_exact_tiles():
    a = grid(frame())
    b_frame = frame()
    paint(b_frame, 160, 32, 224, 96)          # 列 10..13，行 2..5
    d = vision.grid_diff(a, grid(b_frame))
    assert d == tiles(2, 6, 10, 14), sorted(d)[:5]


def test_grid_diff_ignores_subthreshold_noise():
    a = grid(frame())
    b_frame = frame()
    paint(b_frame, 160, 32, 224, 96, value=237)   # 只差 3，低于默认阈值 7
    assert vision.grid_diff(a, grid(b_frame)) == set()


def test_grid_diff_rejects_mismatched_shape():
    a = [[1, 2], [3, 4]]
    try:
        vision.grid_diff(a, [[1, 2]])
    except ValueError:
        return
    raise AssertionError("行数不一致时应当抛 ValueError")


# ------------------------------------------------------------------ 区域
def test_area_tiles_covers_whole_grid():
    assert len(vision.area_tiles((0.0, 0.0, 1.0, 1.0), ROWS, COLS)) == ROWS * COLS


def test_area_tiles_is_clamped_not_crashing():
    # 越界的相对坐标要夹取，不能抛
    assert len(vision.area_tiles((-1.0, -1.0, 9.0, 9.0), ROWS, COLS)) == ROWS * COLS


def test_changed_in_filters_by_area():
    area = vision.DEFAULT_LAYOUT.message
    inside = vision.changed_in({(2, 10)}, area, ROWS, COLS)
    assert inside == {(2, 10)}
    outside = vision.changed_in({(0, 1)}, area, ROWS, COLS)   # 左上角在会话列表
    assert outside == set()


# ------------------------------------------------------------------ 闸门
def test_gate_first_frame_always_triggers():
    v = gate().push(frame(), W, H)
    assert v.trigger and v.reason == "first_frame", v.explain()


def test_gate_quiet_when_nothing_changed():
    g = gate()
    f = frame()
    g.push(f, W, H)
    v = g.push(f, W, H)
    assert not v.trigger, v.explain()
    assert v.reason == "no_meaningful_change"


def test_gate_triggers_on_message_area_change():
    g = gate()
    g.push(frame(), W, H)
    b = frame()
    paint(b, 160, 32, 224, 96)
    v = g.push(b, W, H)
    assert v.trigger and v.reason == "message_changed", v.explain()
    assert v.counts["message"] == 16, v.counts


def test_gate_ignores_single_tile_flicker():
    """一个格子闪一下（光标、loading 小圆点）不该花钱。"""
    g = gate(min_tiles=4)
    g.push(frame(), W, H)
    b = frame()
    paint(b, 160, 32, 176, 48)                # 单个格子
    v = g.push(b, W, H)
    assert not v.trigger, v.explain()


def test_gate_ignores_own_typing_in_composer():
    """自己在输入框打字，绝不能把自己刷醒。"""
    g = gate()
    g.push(frame(), W, H)
    b = frame()
    paint(b, 160, 144, 224, 176)              # 行 9..10，全在输入框区
    v = g.push(b, W, H)
    assert not v.trigger, v.explain()


def test_gate_triggers_on_header_change():
    """标题栏变了 = 换会话了，必须重读。"""
    g = gate()
    g.push(frame(), W, H)
    b = frame()
    paint(b, 160, 0, 224, 16)
    v = g.push(b, W, H)
    assert v.trigger and v.reason == "header_changed", v.explain()


def test_gate_triggers_on_chat_list_change():
    """别处来了新消息（红点/新条目），也要触发。"""
    g = gate()
    g.push(frame(), W, H)
    b = frame()
    paint(b, 0, 32, 64, 64)
    v = g.push(b, W, H)
    assert v.trigger and v.reason == "chat_list_changed", v.explain()


def test_gate_triggers_when_shape_changes():
    """分辨率/窗口尺寸变了，旧基准帧作废。"""
    g = gate()
    g.push(frame(), W, H)
    v = g.push(frame(), W - TILE, H)
    assert v.trigger and v.reason == "shape_changed", v.explain()


def test_gate_reset_forces_retrigger():
    """掉线重连后画面可能一模一样，但内容必须重读。"""
    g = gate()
    f = frame()
    g.push(f, W, H)
    g.push(f, W, H)
    g.reset()
    v = g.push(f, W, H)
    assert v.trigger and v.reason == "first_frame", v.explain()


def test_gate_force_next_forces_retrigger():
    g = gate()
    f = frame()
    g.push(f, W, H)
    g.force_next()
    assert g.push(f, W, H).trigger


def test_peek_does_not_advance_baseline():
    g = gate()
    g.push(frame(), W, H)
    b = frame()
    paint(b, 160, 32, 224, 96)
    assert g.peek(b, W, H).trigger
    assert g.peek(b, W, H).trigger        # 基准帧没动，还是"新变化"
    g.push(b, W, H)
    assert not g.peek(b, W, H).trigger    # 推进之后就不该再触发


def test_custom_layout_is_respected():
    """换个 IM 的界面布局，判断要跟着变。"""
    # 假设布局是左右反的：聊天区在左半边
    flipped = vision.Layout(
        chat_list=(0.76, 0.08, 0.96, 0.96),
        header=(0.00, 0.02, 0.76, 0.09),
        message=(0.00, 0.09, 0.76, 0.78),
        composer=(0.00, 0.78, 0.76, 0.96),
    )
    g = gate(layout=flipped)
    g.push(frame(), W, H)
    b = frame()
    paint(b, 32, 32, 96, 96)                  # 左半边 → 现在算消息区
    v = g.push(b, W, H)
    assert v.trigger and v.reason == "message_changed", v.explain()


# ------------------------------------------------------------------ 指纹
def test_signature_is_stable_and_discriminating():
    a = grid(frame())
    b_frame = frame()
    paint(b_frame, 160, 32, 224, 96)
    b = grid(b_frame)
    assert vision.signature(a) == vision.signature(grid(frame()))
    assert vision.signature(a) != vision.signature(b)
