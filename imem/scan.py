"""会话列表读取：这一帧里，哪些会话有未读。

为什么必须单独一层
------------------
`vision.Gate` 只看"当前画面变没变"，它回答不了"别的会话里有没有新消息"——
因为那些会话根本没被打开，消息区显示的是别人。

**会话列表是唯一能同时看到所有会话的地方。** 所以这里只做一件事：
在列表右侧找红色未读角标。廉价的、确定性的、不花 token 的。

刻意不做的两件事
----------------
1. **不读会话名。** 识别文字要么上模型（每轮都花钱），要么上 OCR（依赖重）。
   名字等切进去之后从标题栏读，那时候一次就够。
   所以在切换完成之前，"第几行"是暂时的、不可信的。
2. **不数未读条数。** 角标里的数字要做 OCR，为了一个计数不值。
   对调度来说"有未读"就足够触发，条数不影响决策。

这两条合起来有一个直接后果：**切换后必须用标题栏核对身份**。
点错行会把 A 的消息记成 B 的——这是本模块边界之外、由
`pipeline.commit(expect_conversation=...)` 兜住的事。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from . import vision

# 角标所在的一条窄带：会话列表的右侧。
# 收窄是有意的——会话列表里还有头像和消息预览，
# 红色的头像照片会造成大量误报，而角标永远靠右对齐。
BADGE_BAND: vision.Area = (0.16, 0.08, 0.24, 0.96)

# 微信 PC 会话行高（1280x800 窗口下约 64px）。只用于把角标归到第几行。
DEFAULT_ROW_H = 64


@dataclass(frozen=True)
class Badge:
    """一个红色角标的包围盒。坐标是原图坐标。"""

    x: int
    y: int
    w: int
    h: int
    pixels: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def row_index(self, row_h: int = DEFAULT_ROW_H, row_top: int = 0) -> int:
        """角标中心落在第几行。列表滚动后这个值就不可信了，别拿它当身份。"""
        return max(0, (self.cy - row_top) // max(1, row_h))


# ------------------------------------------------------------------ 判色


def is_red(r: int, g: int, b: int, *, threshold: int = 50, floor: int = 140,
           max_gb: int = 150, balance: int = 60) -> bool:
    """够不够「未读角标那种红」。

    只判 r 明显大于 g/b 是不够的——橙色头像 (255,165,0) 也满足，
    而会话列表里塞满了彩色头像，误报会让调度器每一帧都以为有未读。
    所以再加两条：绿蓝都得够暗（max_gb），而且两者要接近（balance：
    红角标是 r 高、g≈b 低，橙色则是 g 高 b 低）。

    微信角标实测约 (250, 80, 80)。
    """
    return (r >= floor
            and (r - g) >= threshold
            and (r - b) >= threshold
            and g <= max_gb
            and b <= max_gb
            and abs(g - b) <= balance)

# ------------------------------------------------------------------ 簇


def _cluster(points: set[tuple[int, int]], gap: int = 2
             ) -> list[tuple[int, int, int, int, int]]:
    """把相邻的红点连成块，返回 (x0, y0, x1, y1, 像素数)。

    `gap` 允许跨过小缝隙——角标中间有白色的数字，纯粹的四邻接会把
    一个角标切成碎片。默认 2 像素的容差在 1x 分辨率下够用。
    """
    if not points:
        return []

    offsets = [(dx, dy)
               for dx in range(-gap, gap + 1)
               for dy in range(-gap, gap + 1)
               if dx or dy]

    remaining = set(points)
    blobs: list[tuple[int, int, int, int, int]] = []

    while remaining:
        seed = remaining.pop()
        stack = [seed]
        comp = [seed]
        while stack:
            cx, cy = stack.pop()
            for dx, dy in offsets:
                p = (cx + dx, cy + dy)
                if p in remaining:
                    remaining.discard(p)
                    stack.append(p)
                    comp.append(p)
        xs = [p[0] for p in comp]
        ys = [p[1] for p in comp]
        blobs.append((min(xs), min(ys), max(xs), max(ys), len(comp)))

    return blobs


# ------------------------------------------------------------------ 主入口


def find_red_badges(rgb: Sequence[int], width: int, height: int, *,
                    area: vision.Area | None = None,
                    threshold: int = 50, floor: int = 140,
                    max_gb: int = 150, balance: int = 60,
                    min_pixels: int = 6, max_side: int = 48,
                    merge_gap: int = 2) -> list[Badge]:
    """在指定区域里找未读角标。返回按 y 从上到下排好序的角标。

    `rgb` 是行优先的 R,G,B 三元组展开（长度 width*height*3）。
    `min_pixels` 过滤单像素噪声，`max_side` 过滤掉整片红色的误报。
    """
    if width <= 0 or height <= 0:
        raise ValueError("宽高必须为正")
    if len(rgb) < width * height * 3:
        raise ValueError(
            f"像素数不足: 期望 {width * height * 3}，实际 {len(rgb)}")

    x0, y0, x1, y1 = _area_px(area or BADGE_BAND, width, height)

    points: set[tuple[int, int]] = set()
    for y in range(y0, y1):
        base = y * width * 3
        for x in range(x0, x1):
            i = base + x * 3
            if is_red(rgb[i], rgb[i + 1], rgb[i + 2],
                      threshold=threshold, floor=floor,
                      max_gb=max_gb, balance=balance):
                points.add((x, y))

    out: list[Badge] = []
    for bx0, by0, bx1, by1, n in _cluster(points, merge_gap):
        w = bx1 - bx0 + 1
        h = by1 - by0 + 1
        if n < min_pixels or w > max_side or h > max_side:
            continue
        out.append(Badge(x=bx0, y=by0, w=w, h=h, pixels=n))

    out.sort(key=lambda b: (b.y, b.x))
    return out


def has_unread(rgb: Sequence[int], width: int, height: int, **kw) -> bool:
    """调度器的第一道门：这一帧值不值得做切换。"""
    return bool(find_red_badges(rgb, width, height, **kw))


def unread_rows(badges: Iterable[Badge], *, row_h: int = DEFAULT_ROW_H,
                row_top: int = 0) -> list[int]:
    """角标 → 去重后的行号列表（从上到下）。"""
    return sorted({b.row_index(row_h, row_top) for b in badges})


def to_signals(badges: Iterable[Badge], *, row_h: int = DEFAULT_ROW_H,
               row_top: int = 0) -> list[dict]:
    """角标 → 给 `plan` 用的最小信号。

    还没有 title：得等切进会话、从标题栏读出来才知道是谁。
    """
    # 按行去重：一行就是一个会话，多出来的角标（免打扰图标、@ 标记）不该重复算
    seen: set[int] = set()
    signals: list[dict] = []
    for b in badges:
        row = b.row_index(row_h, row_top)
        if row in seen:
            continue
        seen.add(row)
        signals.append({"row": row, "y": b.cy, "cx": b.cx, "title": ""})
    signals.sort(key=lambda s: s["y"])
    return signals


class RowTitles:
    """行号 → 会话名 的短期缓存。

    为什么必须有
    ------------
    角标只告诉你"第几行有未读"，不告诉你那是谁。而没有名字，
    `plan` 的冷却就完全失效——每轮都会把同样的三行重切一遍，
    正好是策略想避免的"刷不停"。

    名字的第一次来源是切进去之后从标题栏读到的。读到就记下来，
    下一轮同样的行号直接复用。

    有效性
    ------
    行号只有在**列表没变**时才可靠：来了新消息、会话被顶下去、列表滚动，
    行号就全错位了。所以 apply() 接受一个列表区域的指纹，
    指纹一变就整张表作废。宁可重新认一遍，也不能拿旧行号去点。
    """

    def __init__(self) -> None:
        self._map: dict[int, str] = {}
        self._sig: int | None = None

    def __len__(self) -> int:
        return len(self._map)

    def learn(self, row: int, title: str | None) -> None:
        """记下"这一行是谁"。名字为空不覆盖已有认知。"""
        if title:
            self._map[int(row)] = str(title)

    def clear(self) -> None:
        self._map.clear()
        self._sig = None

    def apply(self, signals, *, signature: int | None = None) -> list[dict]:
        """把已知的名字补进信号里。指纹变了先清表。"""
        if signature is not None:
            if self._sig is not None and signature != self._sig:
                self._map.clear()      # 列表动过了，行号作废
            self._sig = signature

        out: list[dict] = []
        for s in signals:
            item = dict(s)
            if not item.get("title"):
                item["title"] = self._map.get(int(item.get("row", -1)), "")
            out.append(item)
        return out


def _area_px(area: vision.Area, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = area
    return (
        _clamp(int(x0 * width), 0, width - 1),
        _clamp(int(y0 * height), 0, height - 1),
        _clamp(int(round(x1 * width)), 1, width),
        _clamp(int(round(y1 * height)), 1, height),
    )


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def find_selected_row(rgb: Sequence[int], width: int, height: int, *,
                      area: vision.Area | None = None,
                      row_h: int = DEFAULT_ROW_H, row_top: int = 0,
                      min_delta: float = 4.0) -> int | None:
    """找出当前打开的是第几行。

    为什么需要它
    ------------
    "切回去"要知道原来打开的那个会话在哪一行。界面上没有行号，但这行
    有个别的行没有的特征：它被选中了，整行背景色和别的行不一样。
    按 row_h 切条、逐行算平均亮度，偏离中位数最多的那条就是它。

    为什么敢返回 None
    -----------------
    认错行的代价是点错一个会话——白白吃掉别人的未读。所以宁可不认：

      - 偏离不够大（背景色差异小于 min_delta）
      - 有两行偏离得差不多（分不清谁是选中行）

    两种情况都返回 None，调用方就不该恢复。恢复是锦上添花，不是必需品，
    没有它最多是屏幕停错了地方，用户自己点一下就好。

    已知的脆弱点
    ------------
    带图片缩略图的会话行，平均亮度也会明显偏离。靠"只允许一个赢家"挡掉
    一部分，但挡不干净。换主题、改深色模式、改字号，min_delta 都要重标。
    """
    if row_h <= 0:
        raise ValueError("row_h 必须为正")
    if len(rgb) < width * height * 3:
        raise ValueError(
            f"像素数不足: 期望 {width * height * 3}，实际 {len(rgb)}")

    x0, y0, x1, y1 = _area_px(area or vision.DEFAULT_LAYOUT.chat_list, width, height)

    means: dict[int, float] = {}
    # 从区域的顶边开始切条，行号仍按 row_top 计 —— 否则窗口下移一点，
    # 所有行号就整体偏一位，点回去会点错行
    top = max(row_top, y0)
    while top + row_h <= y1:
        row = (top - row_top) // row_h
        total = 0
        n = 0
        for y in range(top, top + row_h):
            base = y * width * 3
            for x in range(x0, x1):
                i = base + x * 3
                total += (rgb[i] + rgb[i + 1] + rgb[i + 2]) // 3
                n += 1
        if n:
            means[row] = total / n
        top += row_h

    # 少于 4 行就没有可信的基线：中位数可能正好落在选中行自己身上，
    # 于是"最亮的那一行"反而被当成选中行。真实会话列表几乎总是超过 4 行，
    # 少于此数一律不认。
    if len(means) < 4:
        return None

    baseline = sorted(means.values())[len(means) // 2]
    ranked = sorted(((abs(m - baseline), r) for r, m in means.items()), reverse=True)

    best_delta, best_row = ranked[0]
    if best_delta < min_delta:
        return None
    if len(ranked) > 1 and (best_delta - ranked[1][0]) < min_delta:
        return None
    return best_row
