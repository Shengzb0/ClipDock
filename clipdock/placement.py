"""按快捷键时，面板放到输入光标旁边的哪里。

面板呼出后会一直开着，所以不放在你正在打字的那一行的同一高度：
那一行还要往右接着写，中文输入法的候选框也在光标正下方。坐标都是 Qt 的逻辑像素。

1. 当前窗口没最大化、旁边有空位、离光标又不远：贴在窗口外侧（同样避开光标那一行），
   不挡这个程序的任何内容。
2. 否则像下拉菜单一样，放在光标那一行的上方或下方，左边和光标对齐：
   - 上方优先：接着写的内容往右、往下长，输入法候选框在下面，表单里下面是还没填的格子，
     上方挡的只是已经写完、填完的；
   - 上方放不下才放下方（让开输入法候选框）；
   - 地方不够时面板变矮，最矮 MIN_HEIGHT。
3. 屏幕实在太矮、上下都放不下，才放到同一高度的右边或左边。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRect, QSize

TEXT_ROOM = 300        # 光标右边留给接着要输入、粘贴的文字的宽度
IME_ROOM = 64          # 光标下面留给输入法候选框的高度
GAP = 10               # 面板和光标那一行、和窗口之间的间隔
MARGIN = 8             # 面板离屏幕边缘至少这么远
NEAR = 560             # 贴在窗口外侧时，面板离光标不能超过这么远
MIN_HEIGHT = 300       # 为了放得下而缩矮时，最矮这么高（大约能看到 4 条）
KEEP_SLACK = 100       # 面板已经在光标那一行上方/下方不远处：再按快捷键不挪，免得跳来跳去
KEEP_SIDEWAYS = 240    # ……并且横向上光标没有偏出面板左右这么远


@dataclass(frozen=True)
class Box:
    """矩形，right/bottom 不包含在内（比 QRect 的 right()/bottom() 好算）。"""

    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def of(cls, rect: QRect) -> "Box":
        return cls(rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height())

    def rect(self) -> QRect:
        return QRect(self.left, self.top, self.width, self.height)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def overlap(self, other: "Box") -> int:
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return w * h if w > 0 and h > 0 else 0

    def inside(self, other: "Box") -> bool:
        return (other.left <= self.left and self.right <= other.right
                and other.top <= self.top and self.bottom <= other.bottom)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high)) if high >= low else low


def protected_zone(caret: QRect, window: QRect | None = None) -> Box:
    """光标那一行（往下算上输入法候选框）：上边和下边就是面板不能越过的那一“行”。
    左右范围是光标往右接着要写的地方，只在面板不得不放到同一高度时用到。"""
    c = Box.of(caret)
    right = c.left + TEXT_ROOM
    if window is not None:  # 文字不会超出当前窗口
        right = max(c.left + 40, min(right, Box.of(window).right))
    return Box(c.left - 12, c.top - 6, right, c.bottom + IME_ROOM)


def _above_or_below(zone: Box, area: Box, x: int, width: int, height: int,
                    min_height: int) -> list[tuple[str, Box]]:
    """面板放在光标那一行的上方或下方，按优先顺序列出放得下的。"""
    room = {"上方": zone.top - GAP - area.top, "下方": area.bottom - (zone.bottom + GAP)}

    def box(where: str) -> Box:
        h = min(height, room[where])
        if where == "上方":
            return Box(x, zone.top - GAP - h, x + width, zone.top - GAP)
        return Box(x, zone.bottom + GAP, x + width, zone.bottom + GAP + h)

    return [(where, box(where)) for where in ("上方", "下方") if room[where] >= min_height]


def place_near_caret(caret: QRect, panel: QSize, screen: QRect, window: QRect | None = None,
                     outside_ok: bool = False, list_top: int = 90,
                     min_height: int = MIN_HEIGHT) -> tuple[QRect, str]:
    """算出面板的位置和大小。

    caret：光标；panel：面板想要的大小（放不下时会变矮）；screen：光标所在屏幕的可用区域；
    window：当前窗口；outside_ok：窗口没最大化，可以贴在窗口外侧；
    list_top：面板顶边到第一条记录的距离（只在不得不放到同一高度时用来对齐）。
    返回 (面板的位置和大小, 用了哪条规则)。
    """
    c = Box.of(caret)
    area = Box.of(screen)
    area = Box(area.left + MARGIN, area.top + MARGIN, area.right - MARGIN, area.bottom - MARGIN)
    width = min(panel.width(), area.width)
    height = min(panel.height(), area.height)
    min_height = min(min_height, height)
    zone = protected_zone(caret, window)

    candidates: list[tuple[str, Box]] = []
    if window is not None and outside_ok:
        win = Box.of(window)
        right_x = win.right + GAP
        if right_x - c.left <= NEAR:
            candidates += [(f"窗口右侧、光标{where}", b)
                           for where, b in _above_or_below(zone, area, right_x, width, height, min_height)]
        left_x = win.left - GAP - width
        if c.left - (left_x + width) <= NEAR:
            candidates += [(f"窗口左侧、光标{where}", b)
                           for where, b in _above_or_below(zone, area, left_x, width, height, min_height)]
    column_x = _clamp(c.left - 20, area.left, area.right - width)  # 和光标左对齐，像下拉菜单
    candidates += [(f"光标{where}", b)
                   for where, b in _above_or_below(zone, area, column_x, width, height, min_height)]
    for name, b in candidates:
        if b.inside(area) and not b.overlap(zone):
            return b.rect(), name

    # 屏幕太矮，上下都放不下：只好和光标同一高度，放在接着要写的文字右边，再不行放左边
    side_y = _clamp(c.top - list_top, area.top, area.bottom - height)
    beside = [("光标右侧", zone.right + GAP, side_y), ("光标左侧", zone.left - GAP - width, side_y)]
    for name, x, y in beside:
        b = Box(x, y, x + width, y + height)
        if b.inside(area) and not b.overlap(zone):
            return b.rect(), name

    # 哪儿都放不下：挤进屏幕，选挡得最少的
    squeezed = beside + [("光标上方", column_x, zone.top - GAP - height), ("光标下方", column_x, zone.bottom + GAP)]
    best: tuple[int, Box, str] | None = None
    for name, x, y in squeezed:
        x, y = _clamp(x, area.left, area.right - width), _clamp(y, area.top, area.bottom - height)
        b = Box(x, y, x + width, y + height)
        covered = b.overlap(zone)
        if best is None or covered < best[0]:
            best = (covered, b, name)
    _, b, name = best
    return b.rect(), f"{name}（挤一挤）"


def still_good(current: QRect, caret: QRect, screen: QRect, window: QRect | None = None) -> bool:
    """面板已经在光标那一行的上方或下方不远处：就不挪，免得一按快捷键面板就跳来跳去。"""
    p, c, area = Box.of(current), Box.of(caret), Box.of(screen)
    zone = protected_zone(caret, window)
    if not p.inside(area):
        return False
    if p.bottom <= zone.top:
        distance = zone.top - p.bottom
    elif p.top >= zone.bottom:
        distance = p.top - zone.bottom
    else:
        return False  # 和光标在同一高度
    return distance <= KEEP_SLACK and max(p.left - c.left, c.left - p.right, 0) <= KEEP_SIDEWAYS


def native_to_logical(x: float, y: float, screens: list[tuple[QRect, float]]) -> QPointF:
    """Windows 给的物理像素 → Qt 的逻辑坐标。

    Qt 6 里每块屏幕左上角的坐标不缩放，屏幕内部按这块屏幕的缩放比例换算；
    所以先找到点在哪块屏幕上（按物理大小算），再换算。
    """
    if not screens:
        return QPointF(x, y)
    best = None
    for geo, ratio in screens:
        ratio = ratio or 1.0
        left, top = geo.x(), geo.y()
        right, bottom = left + geo.width() * ratio, top + geo.height() * ratio
        dx = max(left - x, 0.0, x - right)
        dy = max(top - y, 0.0, y - bottom)
        distance = dx * dx + dy * dy
        if best is None or distance < best[0]:
            best = (distance, left, top, ratio)
    _, left, top, ratio = best
    return QPointF(left + (x - left) / ratio, top + (y - top) / ratio)


def native_rect_to_logical(left: int, top: int, right: int, bottom: int,
                           screens: list[tuple[QRect, float]]) -> QRect:
    a = native_to_logical(left, top, screens)
    b = native_to_logical(right, bottom, screens)
    return QRect(round(a.x()), round(a.y()), max(1, round(b.x() - a.x())), max(1, round(b.y() - a.y())))
