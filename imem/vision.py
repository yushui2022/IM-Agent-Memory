"""帧变化检测 —— 多模态采集的成本闸门。

为什么必须有这一层
------------------
把屏幕帧连续喂给视觉模型是最贵的一种做法。1280×800 一帧大约上千 token，
1fps 常驻 = 一天几十亿 token。真实可用的形态是：

    本地廉价地判断「画面变没变」  →  只有变了才送模型

这个模块只做前者，纯标准库、零模型调用。它回答三个问题：

  1. 和上一帧比，哪些区块的像素变了？        to_grid / grid_diff
  2. 变的区块落在界面的哪个区域？            area_tiles / changed_in
  3. 这一帧值不值得花钱送模型？              Gate.push

它**不认识微信**，也不知道屏幕上是啥。区域划分由 `Layout` 给，默认值是
微信 PC 端的相对坐标，别的 IM 换一份 Layout 即可。

调用者应当先降采样再进来
------------------------
`to_grid` 自己在降采样，但纯 Python 遍历 100 万像素仍然要 0.1s 级别。
生产环境推荐先用 cv2/numpy 把整帧缩到 ~320×200，再交给本模块，
此时 `width/height` 传缩放后的尺寸、`tile` 取 4 左右即可。
本模块不 import numpy/cv2，就是为了让测试与演示保持零依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# 相对坐标 (x0, y0, x1, y1)，取值 0.0 ~ 1.0，原点左上
Area = tuple[float, float, float, float]


@dataclass(frozen=True)
class Layout:
    """界面分区。默认值按微信 PC 端（左侧会话列表 + 右侧聊天区）估的。

    换了 IM、换了分辨率、或者用户把窗口拖窄了，这里都要重估——
    这也是为什么它被显式建模，而不是写死在判断里。
    """

    # 会话列表：新消息红点、新会话冒出来，都在这一带
    chat_list: Area = (0.04, 0.08, 0.24, 0.96)
    # 聊天标题栏：切会话时会变，是个很强的「换人聊了」信号
    header: Area = (0.24, 0.02, 1.00, 0.09)
    # 消息正文区：真正要读的内容
    message: Area = (0.24, 0.09, 1.00, 0.78)
    # 输入框：自己打字会造成噪声，默认不参与触发判断
    composer: Area = (0.24, 0.78, 1.00, 0.96)

    def named(self) -> dict[str, Area]:
        return {
            "chat_list": self.chat_list,
            "header": self.header,
            "message": self.message,
            "composer": self.composer,
        }


DEFAULT_LAYOUT = Layout()


# ---------------------------------------------------------------- 降采样


def to_grid(gray: Sequence[int], width: int, height: int, *,
            tile: int = 16, sample: int = 2) -> list[list[int]]:
    """灰度帧 → 每格平均亮度的二维网格。

    `gray` 是长度 width*height 的行优先序列（bytes / list[int] 都行）。
    `sample` 是格内抽样步长，用来把纯 Python 的遍历成本压下来。
    右侧和底部不足一格的部分直接丢弃，不参与比较。
    """
    if tile <= 0 or sample <= 0:
        raise ValueError("tile 和 sample 必须为正")
    if len(gray) < width * height:
        raise ValueError(f"像素数不足: 期望 {width * height}，实际 {len(gray)}")

    rows = height // tile
    cols = width // tile
    if rows == 0 or cols == 0:
        raise ValueError(f"画面太小: {width}x{height} 放不下 {tile}px 的格")

    grid: list[list[int]] = []
    for r in range(rows):
        y0 = r * tile
        line: list[int] = []
        for c in range(cols):
            x0 = c * tile
            total = 0
            n = 0
            for y in range(y0, y0 + tile, sample):
                base = y * width
                for x in range(x0, x0 + tile, sample):
                    total += gray[base + x]
                    n += 1
            line.append(total // n if n else 0)
        grid.append(line)
    return grid


def grid_diff(a: list[list[int]], b: list[list[int]], *,
              threshold: int = 6) -> set[tuple[int, int]]:
    """两帧网格的差异 → 变化格子的 (row, col) 集合。

    threshold 是**每格平均亮度差**的阈值。太小会把光标闪烁、时钟秒数
    也算成变化；太大又会漏掉一行浅色小字。6~10 是实测比较稳的区间。
    """
    if len(a) != len(b):
        raise ValueError(f"网格行数不一致: {len(a)} vs {len(b)}")
    changed: set[tuple[int, int]] = set()
    for r, (ra, rb) in enumerate(zip(a, b)):
        if len(ra) != len(rb):
            raise ValueError(f"第 {r} 行列数不一致: {len(ra)} vs {len(rb)}")
        for c, (va, vb) in enumerate(zip(ra, rb)):
            if abs(va - vb) >= threshold:
                changed.add((r, c))
    return changed


def grid_shape(grid: list[list[int]]) -> tuple[int, int]:
    return (len(grid), len(grid[0]) if grid else 0)


# ---------------------------------------------------------------- 区域


def area_tiles(area: Area, rows: int, cols: int) -> set[tuple[int, int]]:
    """相对矩形 → 覆盖到的格子集合。边界做夹取，不抛异常。"""
    x0, y0, x1, y1 = area
    c0 = _clamp(int(x0 * cols), 0, cols - 1)
    c1 = _clamp(int(round(x1 * cols)) - 1, c0, cols - 1)
    r0 = _clamp(int(y0 * rows), 0, rows - 1)
    r1 = _clamp(int(round(y1 * rows)) - 1, r0, rows - 1)
    return {(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}


def changed_in(changed: Iterable[tuple[int, int]], area: Area,
               rows: int, cols: int) -> set[tuple[int, int]]:
    """变化格子与某个区域的交集。"""
    return set(changed) & area_tiles(area, rows, cols)


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# ---------------------------------------------------------------- 闸门


@dataclass
class GateVerdict:
    """一次「要不要送模型」的判断结果。字段都是给人看的，便于调阈值。"""

    trigger: bool
    reason: str
    counts: dict[str, int] = field(default_factory=dict)
    total_changed: int = 0

    def explain(self) -> str:
        parts = ", ".join(f"{k}={v}" for k, v in self.counts.items())
        return f"{'触发' if self.trigger else '跳过'}（{self.reason}；{parts}）"


class Gate:
    """有状态的帧闸门：记住上一帧，只在新帧"值得一提"时放行。

    触发条件（满足其一）：
      - 消息区变化 ≥ `min_tiles`     —— 有新内容进来
      - 标题栏发生变化                —— 换会话了，必须重新读
      - 会话列表区发生变化            —— 别处来了新消息（红点/新条目）
    输入框区默认不参与，否则自己打字会把自己刷醒。

    第一条帧永远触发（`first_frame`），否则冷启动会永远判定"没变化"。
    """

    def __init__(self, layout: Layout | None = None, *, tile: int = 16,
                 threshold: int = 7, min_tiles: int = 4, sample: int = 2):
        self.layout = layout or DEFAULT_LAYOUT
        self.tile = tile
        self.threshold = threshold
        self.min_tiles = min_tiles
        self.sample = sample
        self._prev: list[list[int]] | None = None

    def reset(self) -> None:
        """丢掉基准帧。掉线重连、切了分辨率之后必须调。"""
        self._prev = None

    def peek(self, gray: Sequence[int], width: int, height: int) -> GateVerdict:
        """只判断，不更新基准帧。适合想先看一眼再决定要不要推进的情况。"""
        grid = to_grid(gray, width, height, tile=self.tile, sample=self.sample)
        return self._judge(grid)

    def push(self, gray: Sequence[int], width: int, height: int) -> GateVerdict:
        """判断并推进基准帧。返回 trigger=True 时，调用者应当取这一帧去送模型。"""
        grid = to_grid(gray, width, height, tile=self.tile, sample=self.sample)
        verdict = self._judge(grid)
        if verdict.trigger:
            self._prev = grid
        return verdict

    def force_next(self) -> None:
        """让下一帧无条件触发（比如刚从掉线恢复，画面可能一模一样但得重读）。"""
        self._prev = None

    # ------------------------------------------------------------ 内部

    def _judge(self, grid: list[list[int]]) -> GateVerdict:
        rows, cols = grid_shape(grid)

        if self._prev is None:
            return GateVerdict(trigger=True, reason="first_frame",
                               counts={}, total_changed=0)
        if grid_shape(self._prev) != (rows, cols):
            # 分辨率变了，旧基准帧作废
            return GateVerdict(trigger=True, reason="shape_changed",
                               counts={}, total_changed=0)

        changed = grid_diff(self._prev, grid, threshold=self.threshold)
        counts = {
            name: len(changed_in(changed, area, rows, cols))
            for name, area in self.layout.named().items()
            if name != "composer"
        }

        if counts.get("header", 0) > 0:
            return GateVerdict(True, "header_changed", counts, len(changed))
        if counts.get("message", 0) >= self.min_tiles:
            return GateVerdict(True, "message_changed", counts, len(changed))
        if counts.get("chat_list", 0) > 0:
            return GateVerdict(True, "chat_list_changed", counts, len(changed))
        return GateVerdict(False, "no_meaningful_change", counts, len(changed))


def signature(grid: list[list[int]]) -> int:
    """粗指纹。用来在日志里标出「同一帧被处理了几次」，不做 cryptography 用途。"""
    h = 1469598103934665603
    for row in grid:
        for v in row:
            h ^= v
            h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h
