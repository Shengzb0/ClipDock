"""ClipDock 的界面：常驻面板（最近 / 收藏两个页签）、条目绘制、搜索弹层、复制成功提示。

面板本身永远不会被激活（不抢焦点），所以面板上不能直接打字。
搜索时会在搜索框的位置弹出一个小窗口（SearchPopup），它可以被激活、可以打字；
选好之后再把焦点还给原来的窗口，然后填入。
"""
from __future__ import annotations

import html
import logging
import ntpath
import re
import time
from dataclasses import dataclass
from typing import Callable, Union

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QFileInfo,
    QModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import win32
from .store import Item, Project

log = logging.getLogger(__name__)

ALIGN_LEFT_VCENTER = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
ALIGN_RIGHT_VCENTER = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
ALIGN_CENTER = int(Qt.AlignmentFlag.AlignCenter)


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str
    border: str
    card: str
    card_hover: str
    card_flash: str
    text: str
    subtext: str
    accent: str
    star: str
    field: str
    field_border: str
    scrollbar: str
    tab_active: str


LIGHT = Theme(
    dark=False, window="#F3F4F6", border="#CDD2D9", card="#FFFFFF", card_hover="#EAF1FF",
    card_flash="#CFE0FF", text="#1F2328", subtext="#6B7280", accent="#2F6FEB", star="#E39B2D",
    field="#FFFFFF", field_border="#D9DDE3", scrollbar="#C3C8D0", tab_active="#FFFFFF",
)
DARK = Theme(
    dark=True, window="#1F2023", border="#3A3D44", card="#2A2C31", card_hover="#313A4C",
    card_flash="#35508C", text="#E7E8EB", subtext="#9BA1A9", accent="#6B9BFF", star="#E8A33D",
    field="#2A2C31", field_border="#41454D", scrollbar="#4A4E57", tab_active="#3A3D44",
)


def detect_theme() -> Theme:
    """跟随系统的深色/浅色模式。"""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return DARK
        if scheme == Qt.ColorScheme.Light:
            return LIGHT
    except AttributeError:  # Qt 6.5 以前没有 colorScheme
        pass
    if win32.IS_WINDOWS:
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return LIGHT if value else DARK
        except OSError:
            pass
    return LIGHT


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------
_ICON_FAMILIES = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]  # Windows 自带的图标字体
_GLYPHS = {
    "search": "", "pause": "", "play": "", "more": "", "hide": "",
    "clear": "", "star": "", "star_fill": "", "folder": "",
    "chevron": "", "add": "",
}
_FALLBACK_GLYPHS = {
    "search": "⌕", "pause": "❚❚", "play": "▶", "more": "⋯", "hide": "—", "clear": "✕",
    "star": "☆", "star_fill": "★", "folder": "▤", "chevron": "›", "add": "+",
}


def make_icon_font(point_size: float = 9.5) -> tuple[QFont, bool]:
    """返回（图标字体, 系统里是否真的有图标字体）。"""
    installed = set(QFontDatabase.families())
    available = [f for f in _ICON_FAMILIES if f in installed]
    font = QFont(QGuiApplication.font())
    if available:
        font.setFamilies(available)
    font.setPointSizeF(point_size)
    return font, bool(available)


def glyph(name: str, has_icon_font: bool) -> str:
    return (_GLYPHS if has_icon_font else _FALLBACK_GLYPHS)[name]


def make_app_icon(accent: str = "#2F6FEB") -> QIcon:
    """用代码画一个剪贴板图标，省得带图片文件。"""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = size / 32.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(accent))
        p.drawRoundedRect(QRectF(5 * s, 4 * s, 22 * s, 26 * s), 4 * s, 4 * s)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(QRectF(8.5 * s, 9 * s, 15 * s, 18 * s), 2 * s, 2 * s)
        p.setBrush(QColor("#1F2A44"))
        p.drawRoundedRect(QRectF(11 * s, 2 * s, 10 * s, 5.5 * s), 2 * s, 2 * s)
        p.setBrush(QColor(accent))
        for i, width in enumerate((10, 10, 6)):
            p.drawRoundedRect(QRectF(11 * s, (13 + i * 4.5) * s, width * s, 1.8 * s), 0.9 * s, 0.9 * s)
        p.end()
        icon.addPixmap(pixmap)
    return icon


# ---------------------------------------------------------------------------
# 文本与时间
# ---------------------------------------------------------------------------
LINE_BREAK_MARK = " ↵ "


def preview_text(text: str, limit: int = 300) -> str:
    """列表里显示的预览：去掉空行，换行显示成 ↵，压缩连续空格。"""
    s = text[: limit * 3].replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    lines = [line.strip() for line in s.split("\n")]
    s = LINE_BREAK_MARK.join(line for line in lines if line)
    s = re.sub(r"[ 　]{2,}", " ", s)
    return s[:limit]


def wrap_lines(text: str, font: QFont, width: int, max_lines: int) -> list[str]:
    """按宽度折行，最多 max_lines 行，最后一行放不下时用 … 结尾。"""
    if not text:
        return [""]
    # QTextLayout 的位置按 UTF-16 计，Python 字符串按字符计；有 emoji 时要换算
    if any(ord(ch) > 0xFFFF for ch in text):
        index_of = []
        for i, ch in enumerate(text):
            index_of.append(i)
            if ord(ch) > 0xFFFF:
                index_of.append(i)
        index_of.append(len(text))

        def to_py(u16: int) -> int:
            return index_of[min(u16, len(index_of) - 1)]
    else:

        def to_py(u16: int) -> int:
            return u16

    metrics = QFontMetrics(font)
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    lines: list[str] = []
    layout.beginLayout()
    try:
        while len(lines) < max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            start = to_py(line.textStart())
            end = to_py(line.textStart() + line.textLength())
            if len(lines) == max_lines - 1:
                lines.append(metrics.elidedText(text[start:], Qt.TextElideMode.ElideRight, width))
                break
            lines.append(text[start:end].rstrip())
    finally:
        layout.endLayout()
    return lines or [""]


def clock_time(timestamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))


def day_label(timestamp: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    then, today = time.localtime(timestamp), time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return "今天"
    yesterday = time.localtime(now - 86400)
    if (then.tm_year, then.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "昨天"
    if then.tm_year == today.tm_year:
        return f"{then.tm_mon}月{then.tm_mday}日"
    return f"{then.tm_year}年{then.tm_mon}月{then.tm_mday}日"


def relative_time(timestamp: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    delta = now - timestamp
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    day = day_label(timestamp, now)
    if day == "今天":
        return f"{int(delta // 3600)} 小时前"
    return f"{day} {clock_time(timestamp)}" if day == "昨天" else day


def burst_groups(items: list[Item], gap_seconds: float) -> list[list[Item]]:
    """把“连续复制”的记录归成一组：相邻两条的间隔不超过 gap_seconds 就算同一组。

    items 要按复制时间从新到旧排好。
    """
    groups: list[list[Item]] = []
    for item in items:
        if groups and groups[-1][-1].last_copied - item.last_copied <= gap_seconds:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def burst_label(group: list[Item], now: float | None = None) -> str:
    """组标题，例如“今天 14:02 – 14:10 · 4 条”。"""
    newest, oldest = group[0].last_copied, group[-1].last_copied
    end_day, start_day = day_label(newest, now), day_label(oldest, now)
    start, end = clock_time(oldest), clock_time(newest)
    if start_day != end_day:
        span = f"{start_day} {start} – {end_day} {end}"
    elif start == end:
        span = f"{end_day} {end}"
    else:
        span = f"{end_day} {start} – {end}"
    return f"{span} · {len(group)} 条"


def tooltip_html(item: Item, missing: bool = False) -> str:
    if item.kind == "image":
        body = f"图片 {item.meta.get('w', '?')}×{item.meta.get('h', '?')}，点击填入"
    elif item.kind == "files":
        body = "\n".join(item.paths[:20]) + ("\n（已经不在原来的位置）" if missing else "")
    else:
        body = item.preview[:1200] + (f"\n…（共 {item.length:,} 字）" if item.length > 1200 else "")
    return f"<div style='white-space:pre-wrap;'>{html.escape(body)}</div>"


# ---------------------------------------------------------------------------
# 列表的行
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HeaderRow:
    """分组小标题，不能点。"""
    label: str
    key = None


@dataclass(frozen=True)
class ItemRow:
    item: Item
    missing: bool = False     # 文件已经不在原来的位置
    project_name: str = ""    # 在全部收藏里搜索时，显示它属于哪个项目

    @property
    def key(self) -> tuple:
        return ("item", self.item.id)


@dataclass(frozen=True)
class ProjectRow:
    project: Project

    @property
    def key(self) -> tuple:
        return ("project", self.project.id)


@dataclass(frozen=True)
class NewProjectRow:
    @property
    def key(self) -> tuple:
        return ("new",)


Row = Union[HeaderRow, ItemRow, ProjectRow, NewProjectRow]


class RowModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Row] = []
        self.hover_key: tuple | None = None      # 鼠标悬停的行
        self.flash_key: tuple | None = None      # 刚填入、闪一下的行
        self.highlight_key: tuple | None = None  # 搜索时用 ↑↓ 选中的行
        self.time_style = "clock"                 # "clock"：显示 14:05；"relative"：显示 3 天前

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        row = self.row(index.row()) if index.isValid() else None
        if row is None:
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            if isinstance(row, ItemRow):
                return tooltip_html(row.item, row.missing)
            if isinstance(row, ProjectRow):
                return f"{row.project.name}：{row.project.count} 条收藏，点击进入"
            if isinstance(row, NewProjectRow):
                return "新建一个项目，用来给收藏分类"
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if isinstance(row, ItemRow):
                return row.item.preview[:100]
            if isinstance(row, ProjectRow):
                return row.project.name
            if isinstance(row, HeaderRow):
                return row.label
        return None

    def flags(self, index):
        row = self.row(index.row()) if index.isValid() else None
        if row is None or row.key is None:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled

    def row(self, i: int) -> Row | None:
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def item(self, i: int) -> Item | None:
        row = self.row(i)
        return row.item if isinstance(row, ItemRow) else None

    def index_of(self, key: tuple | None) -> int:
        if key is None:
            return -1
        for i, row in enumerate(self._rows):
            if row.key == key:
                return i
        return -1

    def selectable(self) -> list[int]:
        """可以被选中（↑↓）的行号：分组标题除外。"""
        return [i for i, row in enumerate(self._rows) if row.key is not None]

    def set_rows(self, rows: list[Row]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class RowDelegate(QStyledItemDelegate):
    """把每一行画成卡片：文字两行预览、图片缩略图、文件图标；右上角是收藏星标。"""

    MARGIN_X, MARGIN_Y = 2, 3
    PAD_X, PAD_TOP, PAD_BOTTOM, GAP = 11, 7, 7, 3
    MAX_LINES = 2
    RADIUS = 7
    STAR = 18          # 星标的点击范围
    THUMB_H = 72       # 缩略图最高多少
    FILE_ICON = 28
    HEADER_H = 26
    PROJECT_H = 46
    NEW_H = 36

    def __init__(self, model: RowModel, view: QListView, theme: Theme, icon_font: QFont,
                 has_icons: bool, thumb_loader: Callable[[int], bytes | None]) -> None:
        super().__init__(view)
        self._model = model
        self._view = view
        self.theme = theme
        self._icon_font = QFont(icon_font)
        self._has_icons = has_icons
        self._thumb_loader = thumb_loader
        self._lines_cache: dict[tuple[int, int], list[str]] = {}
        self._thumbs: dict[int, QPixmap | None] = {}
        self._file_icons: dict[str, QIcon] = {}
        self._icon_provider = QFileIconProvider()
        self.set_fonts(view.font())

    def set_fonts(self, base: QFont) -> None:
        self.text_font = QFont(base)
        self.bold_font = QFont(base)
        self.bold_font.setWeight(QFont.Weight.DemiBold)
        self.meta_font = QFont(base)
        self.meta_font.setPointSizeF(max(7.5, base.pointSizeF() - 1.5))
        self.header_font = QFont(self.meta_font)
        self.header_font.setWeight(QFont.Weight.DemiBold)
        self._line_h = QFontMetrics(self.text_font).lineSpacing()
        self._meta_h = QFontMetrics(self.meta_font).lineSpacing()
        self.clear_cache()

    def clear_cache(self) -> None:
        self._lines_cache.clear()
        if len(self._thumbs) > 300:
            self._thumbs.clear()

    # ---- 尺寸 ----
    def _view_width(self) -> int:
        return self._view.viewport().width()

    def _content_width(self) -> int:
        """卡片里内容区的宽度（右上角留出星标的位置）。"""
        return max(40, self._view_width() - 2 * self.MARGIN_X - 2 * self.PAD_X - self.STAR)

    def _lines(self, item: Item, width: int) -> list[str]:
        key = (item.id, width)
        lines = self._lines_cache.get(key)
        if lines is None:
            lines = wrap_lines(preview_text(item.preview), self.text_font, width, self.MAX_LINES)
            self._lines_cache[key] = lines
        return lines

    def _thumb_size(self, item: Item, width: int) -> QSize:
        w = max(1, int(item.meta.get("w", 1)))
        h = max(1, int(item.meta.get("h", 1)))
        scale = min(width / w, self.THUMB_H / h, 1.0)
        out_h = max(18, round(h * scale))
        return QSize(max(18, min(width, round(out_h * w / h))), out_h)

    def _content_height(self, row: ItemRow) -> int:
        item = row.item
        if item.kind == "image":
            return self._thumb_size(item, self._content_width()).height()
        if item.kind == "files":
            return max(self.FILE_ICON, 2 * self._line_h)
        return len(self._lines(item, self._content_width())) * self._line_h

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        width = self._view_width()
        row = self._model.row(index.row())
        if isinstance(row, HeaderRow):
            return QSize(width, self.HEADER_H)
        if isinstance(row, ProjectRow):
            return QSize(width, self.PROJECT_H)
        if isinstance(row, NewProjectRow):
            return QSize(width, self.NEW_H)
        if isinstance(row, ItemRow):
            height = (2 * self.MARGIN_Y + self.PAD_TOP + self._content_height(row)
                      + self.GAP + self._meta_h + self.PAD_BOTTOM)
            return QSize(width, height)
        return QSize(width, 0)

    def card_rect(self, rect: QRect) -> QRectF:
        return QRectF(rect).adjusted(self.MARGIN_X, self.MARGIN_Y, -self.MARGIN_X, -self.MARGIN_Y)

    def star_rect(self, rect: QRect) -> QRectF:
        """星标（收藏开关）在卡片右上角。"""
        card = self.card_rect(rect)
        return QRectF(card.right() - self.PAD_X - self.STAR + 6, card.top() + 4, self.STAR + 4, self.STAR + 2)

    # ---- 绘制 ----
    def paint(self, painter: QPainter, option, index) -> None:
        row = self._model.row(index.row())
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        try:
            if isinstance(row, HeaderRow):
                self._paint_header(painter, option.rect, row)
            elif isinstance(row, ProjectRow):
                self._paint_project(painter, option.rect, row)
            elif isinstance(row, NewProjectRow):
                self._paint_new_project(painter, option.rect, row)
            elif isinstance(row, ItemRow):
                self._paint_item(painter, option.rect, row)
        finally:
            painter.restore()

    def _background(self, key: tuple) -> str:
        m, t = self._model, self.theme
        if key == m.flash_key:
            return t.card_flash
        if key in (m.hover_key, m.highlight_key):
            return t.card_hover
        return t.card

    def _paint_card(self, painter: QPainter, card: QRectF, key: tuple) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._background(key)))
        painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        if key == self._model.highlight_key:
            pen = QPen(QColor(self.theme.accent))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card.adjusted(0.75, 0.75, -0.75, -0.75), self.RADIUS, self.RADIUS)

    def _paint_header(self, painter: QPainter, rect: QRect, row: HeaderRow) -> None:
        painter.setFont(self.header_font)
        painter.setPen(QColor(self.theme.subtext))
        metrics = QFontMetrics(self.header_font)
        x = rect.left() + self.MARGIN_X + 4
        text = metrics.elidedText(row.label, Qt.TextElideMode.ElideRight, rect.width() - 40)
        text_rect = QRectF(x, rect.top() + 6, metrics.horizontalAdvance(text) + 2, rect.height() - 6)
        painter.drawText(text_rect, ALIGN_LEFT_VCENTER, text)
        line = QColor(self.theme.border)
        line.setAlphaF(0.7)
        painter.setPen(QPen(line, 1))
        y = text_rect.center().y() + 0.5
        painter.drawLine(int(text_rect.right() + 8), int(y), int(rect.right() - self.MARGIN_X - 4), int(y))

    def _paint_project(self, painter: QPainter, rect: QRect, row: ProjectRow) -> None:
        t = self.theme
        card = self.card_rect(rect)
        self._paint_card(painter, card, row.key)
        icon_rect = QRectF(card.left() + self.PAD_X - 2, card.center().y() - 11, 22, 22)
        icon_font = QFont(self._icon_font)
        icon_font.setPointSizeF(self._icon_font.pointSizeF() + 2)
        painter.setFont(icon_font)
        painter.setPen(QColor(t.accent))
        painter.drawText(icon_rect, ALIGN_CENTER, glyph("folder", self._has_icons))
        chevron = QRectF(card.right() - self.PAD_X - 14, card.top(), 16, card.height())
        painter.setFont(self._icon_font)
        painter.setPen(QColor(t.subtext))
        painter.drawText(chevron, ALIGN_CENTER, glyph("chevron", self._has_icons))
        count_text = f"{row.project.count} 条"
        painter.setFont(self.meta_font)
        count_w = QFontMetrics(self.meta_font).horizontalAdvance(count_text) + 4
        count_rect = QRectF(chevron.left() - 6 - count_w, card.top(), count_w, card.height())
        painter.drawText(count_rect, ALIGN_RIGHT_VCENTER, count_text)
        name_x = icon_rect.right() + 8
        name_w = max(10, count_rect.left() - 8 - name_x)
        painter.setFont(self.bold_font)
        painter.setPen(QColor(t.text))
        name = QFontMetrics(self.bold_font).elidedText(row.project.name, Qt.TextElideMode.ElideRight, int(name_w))
        painter.drawText(QRectF(name_x, card.top(), name_w, card.height()), ALIGN_LEFT_VCENTER, name)

    def _paint_new_project(self, painter: QPainter, rect: QRect, row: NewProjectRow) -> None:
        t = self.theme
        card = self.card_rect(rect)
        if row.key in (self._model.hover_key, self._model.highlight_key):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.card_hover))
            painter.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        pen = QPen(QColor(t.border), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        painter.setFont(self.text_font)
        painter.setPen(QColor(t.accent))
        painter.drawText(card.adjusted(self.PAD_X, 0, -self.PAD_X, 0), ALIGN_LEFT_VCENTER, "＋ 新建项目")

    def _paint_item(self, painter: QPainter, rect: QRect, row: ItemRow) -> None:
        t, item = self.theme, row.item
        card = self.card_rect(rect)
        self._paint_card(painter, card, row.key)
        # 右上角星标：已收藏一直显示实心星；没收藏的鼠标移上去才显示空心星
        if item.favorite or row.key in (self._model.hover_key, self._model.highlight_key):
            painter.setFont(self._icon_font)
            painter.setPen(QColor(t.star if item.favorite else t.subtext))
            painter.drawText(self.star_rect(rect), ALIGN_CENTER,
                             glyph("star_fill" if item.favorite else "star", self._has_icons))
        width = self._content_width()
        x = card.left() + self.PAD_X
        y = card.top() + self.PAD_TOP
        if item.kind == "image":
            y += self._paint_thumbnail(painter, item, QRectF(x, y, width, 0))
        elif item.kind == "files":
            y += self._paint_files(painter, row, QRectF(x, y, width, 0))
        else:
            painter.setFont(self.text_font)
            painter.setPen(QColor(t.text))
            for line in self._lines(item, width):
                painter.drawText(QRectF(x, y, width, self._line_h), ALIGN_LEFT_VCENTER, line)
                y += self._line_h
        y += self.GAP
        painter.setFont(self.meta_font)
        painter.setPen(QColor(t.subtext))
        meta = QFontMetrics(self.meta_font).elidedText(self.meta_text(row), Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(x, y, width, self._meta_h), ALIGN_LEFT_VCENTER, meta)

    def _thumb(self, item: Item) -> QPixmap | None:
        if item.id not in self._thumbs:
            pixmap = None
            data = self._thumb_loader(item.id)
            if data:
                pixmap = QPixmap()
                if not pixmap.loadFromData(data, "PNG"):
                    pixmap = None
            self._thumbs[item.id] = pixmap
        return self._thumbs[item.id]

    def _paint_thumbnail(self, painter: QPainter, item: Item, area: QRectF) -> int:
        size = self._thumb_size(item, int(area.width()))
        target = QRectF(area.left(), area.top(), size.width(), size.height())
        clip = QPainterPath()
        clip.addRoundedRect(target, 4, 4)
        pixmap = self._thumb(item)
        painter.save()
        painter.setClipPath(clip)
        if pixmap is not None:
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        else:
            painter.fillRect(target, QColor(self.theme.card_hover))
        painter.restore()
        edge = QColor(self.theme.border)
        edge.setAlphaF(0.8)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(target.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        return size.height()

    def _file_icon(self, path: str) -> QIcon:
        icon = self._file_icons.get(path)
        if icon is None:
            # 网络路径取图标可能要访问网络（断网时会卡住），直接用通用图标
            icon = QIcon() if win32.is_remote_path(path) else self._icon_provider.icon(QFileInfo(path))
            if icon.isNull():
                generic = (QFileIconProvider.IconType.File if ntpath.splitext(path)[1]
                           else QFileIconProvider.IconType.Folder)
                icon = self._icon_provider.icon(generic)
            self._file_icons[path] = icon
        return icon

    def _paint_files(self, painter: QPainter, row: ItemRow, area: QRectF) -> int:
        t, item = self.theme, row.item
        paths = item.paths
        first = paths[0] if paths else ""
        count = int(item.meta.get("count", len(paths)) or 1)
        icon_rect = QRect(int(area.left()), int(area.top()) + 2, self.FILE_ICON, self.FILE_ICON)
        mode = QIcon.Mode.Disabled if row.missing else QIcon.Mode.Normal
        self._file_icon(first).paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter, mode)
        x = area.left() + self.FILE_ICON + 8
        width = int(area.width() - self.FILE_ICON - 8)
        name = ntpath.basename(first.rstrip("\\/")) or first
        if count > 1:
            name += f"  等 {count} 个"
        folder = ntpath.dirname(first.rstrip("\\/"))
        if row.missing:
            folder = "已经不在原来的位置：" + folder
        painter.setFont(self.text_font)
        painter.setPen(QColor(t.subtext if row.missing else t.text))
        metrics = QFontMetrics(self.text_font)
        painter.drawText(QRectF(x, area.top(), width, self._line_h), ALIGN_LEFT_VCENTER,
                         metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, width))
        painter.setPen(QColor(t.subtext))
        painter.drawText(QRectF(x, area.top() + self._line_h, width, self._line_h), ALIGN_LEFT_VCENTER,
                         metrics.elidedText(folder, Qt.TextElideMode.ElideMiddle, width))
        return max(self.FILE_ICON, 2 * self._line_h)

    def meta_text(self, row: ItemRow, now: float | None = None) -> str:
        item = row.item
        parts = []
        if item.kind == "image":
            parts.append(f"图片 {item.meta.get('w', '?')}×{item.meta.get('h', '?')}")
        elif item.kind == "files":
            count = int(item.meta.get("count", len(item.paths)) or 1)
            parts.append(f"{count} 个文件" if count > 1 else "文件")
        parts.append(item.source or "未知来源")
        if self._model.time_style == "clock":
            parts.append(clock_time(item.last_copied))
        else:
            parts.append(relative_time(item.last_copied, now))
        if item.kind == "text":
            if item.meta.get("rich"):
                parts.append("带格式")
            if item.length >= 200:
                parts.append(f"{item.length:,} 字")
        if row.project_name:
            parts.append(f"项目：{row.project_name}")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# 小部件
# ---------------------------------------------------------------------------
class ElidedLabel(QLabel):
    """放不下时显示省略号的标签，并且不会把窗口撑宽。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)
        self.color = QColor("#6B7280")

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        rect = self.contentsRect()
        painter.setPen(self.color)
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, ALIGN_LEFT_VCENTER, text)


class PanelBackground(QFrame):
    """面板的圆角底板。底色带透明度（空白处半透明），卡片和搜索框画在上面，不受影响。"""

    RADIUS = 10

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self.background = QColor(LIGHT.window)
        self.border = QColor(LIGHT.border)
        self.accent = QColor(LIGHT.accent)
        self.opacity = 0.7
        self.glow = 0.0  # 0~1：刚呼出时边框亮一下

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        fill = QColor(self.background)
        fill.setAlphaF(self.opacity)
        painter.fillPath(path, fill)
        edge = QColor(self.border)
        edge.setAlphaF(min(1.0, self.opacity + 0.3))  # 边框比底色实一点，透明时也看得清面板边缘
        painter.setPen(QPen(edge, 1.0))
        painter.drawPath(path)
        if self.glow > 0:
            ring = QColor(self.accent)
            ring.setAlphaF(min(1.0, self.glow))
            painter.setPen(QPen(ring, 2.0))
            inner = QPainterPath()
            inner.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), self.RADIUS - 1, self.RADIUS - 1)
            painter.drawPath(inner)


class HeaderBar(QFrame):
    """顶栏：最近 / 收藏 两个页签和几个按钮；拖动空白处可以移动面板。"""

    tabClicked = Signal(str)
    pauseClicked = Signal()
    menuClicked = Signal(QPoint)
    hideClicked = Signal()
    dragFinished = Signal()

    def __init__(self, icon_font: QFont, has_icons: bool) -> None:
        super().__init__()
        self.setObjectName("Header")
        self.setFixedHeight(30)
        self._has_icons = has_icons
        self._drag_offset: QPoint | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.tab_recent = self._tab("最近", "recent", "按复制时间排列，连续复制的归成一组")
        self.tab_favorites = self._tab("收藏", "favorites", "长期保存的内容，可以分到项目里")
        layout.addWidget(self.tab_recent)
        layout.addWidget(self.tab_favorites)
        layout.addSpacing(6)
        self.paused = QLabel("已暂停")
        self.paused.setObjectName("Paused")
        self.paused.hide()
        layout.addWidget(self.paused)
        layout.addStretch(1)
        self.btn_pause = self._button(icon_font, "pause", "暂停记录")
        self.btn_more = self._button(icon_font, "more", "菜单")
        self.btn_hide = self._button(icon_font, "hide", "隐藏面板（按快捷键可以再呼出）")
        for btn in (self.btn_pause, self.btn_more, self.btn_hide):
            layout.addWidget(btn)
        self.btn_pause.clicked.connect(self.pauseClicked)
        self.btn_more.clicked.connect(
            lambda: self.menuClicked.emit(self.btn_more.mapToGlobal(QPoint(0, self.btn_more.height())))
        )
        self.btn_hide.clicked.connect(self.hideClicked)
        self.set_tab("recent")

    def _tab(self, text: str, name: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("Tab")
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setCheckable(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self.tabClicked.emit(name))
        return btn

    def _button(self, icon_font: QFont, name: str, tip: str) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("HeaderButton")
        btn.setText(glyph(name, self._has_icons))
        btn.setFont(icon_font)
        btn.setToolTip(tip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(26, 24)
        return btn

    def set_tab(self, tab: str) -> None:
        self.tab_recent.setChecked(tab == "recent")
        self.tab_favorites.setChecked(tab == "favorites")

    def set_favorite_count(self, count: int) -> None:
        self.tab_favorites.setText(f"收藏 {count}" if count else "收藏")

    def set_paused(self, paused: bool) -> None:
        self.paused.setVisible(paused)
        self.btn_pause.setText(glyph("play" if paused else "pause", self._has_icons))
        self.btn_pause.setToolTip("继续记录" if paused else "暂停记录")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self.dragFinished.emit()
        super().mouseReleaseEvent(event)


class SearchField(QFrame):
    """看起来像输入框，其实是个按钮：点一下才弹出真正能打字的搜索框。"""

    clicked = Signal()
    cleared = Signal()

    def __init__(self, icon_font: QFont, has_icons: bool) -> None:
        super().__init__()
        self.setObjectName("SearchField")
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(6)
        self.icon = QLabel(glyph("search", has_icons))
        self.icon.setObjectName("SearchIcon")
        self.icon.setFont(icon_font)
        self.text = ElidedLabel()
        self.clear_button = QToolButton()
        self.clear_button.setObjectName("ClearButton")
        self.clear_button.setText(glyph("clear", has_icons))
        self.clear_button.setFont(icon_font)
        self.clear_button.setToolTip("清除搜索")
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setFixedSize(24, 24)
        self.clear_button.clicked.connect(self.cleared)
        layout.addWidget(self.icon)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.clear_button)
        self._query = ""
        self._placeholder = "搜索（点这里输入）"
        self._theme = LIGHT
        self.set_query("")

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.set_query(self._query)

    def set_query(self, query: str) -> None:
        self._query = query
        self.text.setText(query if query else self._placeholder)
        self.clear_button.setVisible(bool(query))
        self._update_color()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._update_color()

    def _update_color(self) -> None:
        self.text.color = QColor(self._theme.text if self._query else self._theme.subtext)
        self.text.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class CrumbBar(QFrame):
    """进入项目后显示：‹ 收藏 / 项目名。点左边返回。"""

    backClicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Crumb")
        self.setFixedHeight(26)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(4)
        self.back = QToolButton()
        self.back.setObjectName("CrumbBack")
        self.back.setText("‹ 收藏")
        self.back.setToolTip("返回收藏（搜索框里按 ← 也可以）")
        self.back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back.clicked.connect(self.backClicked)
        self.slash = QLabel("/")
        self.slash.setObjectName("CrumbSlash")
        self.name = ElidedLabel()
        bold = QFont(self.name.font())
        bold.setWeight(QFont.Weight.DemiBold)
        self.name.setFont(bold)
        layout.addWidget(self.back)
        layout.addWidget(self.slash)
        layout.addWidget(self.name, 1)

    def set_project(self, project: Project) -> None:
        self.name.setText(f"{project.name}（{project.count}）")
        self.name.update()


class ResizeGrip(QWidget):
    """右下角的拖动把手，用来调整面板大小。"""

    resizeFinished = Signal()

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self._window = window
        self._start: tuple[QPoint, QSize] | None = None
        self.color = QColor("#9BA1A9")
        self.setFixedSize(14, 14)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖动调整大小")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = (event.globalPosition().toPoint(), self._window.size())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            origin, size = self._start
            delta = event.globalPosition().toPoint() - origin
            self._window.resize(size.width() + delta.x(), size.height() + delta.y())
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self._start = None
            self.resizeFinished.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        w, h = self.width(), self.height()
        for i in range(3):          # 三角形排列的小圆点，指向右下角
            for j in range(3 - i):
                x, y = w - 3 - 4 * j, h - 3 - 4 * i
                painter.drawEllipse(QRectF(x - 1.1, y - 1.1, 2.2, 2.2))


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
def panel_stylesheet(t: Theme) -> str:
    return f"""
#Paused {{ color: {t.star}; }}
QToolButton#Tab {{ color: {t.subtext}; background: transparent; border: none; border-radius: 7px;
                   padding: 3px 10px; font-weight: 600; }}
QToolButton#Tab:hover {{ color: {t.text}; }}
QToolButton#Tab:checked {{ color: {t.text}; background: {t.tab_active}; border: 1px solid {t.field_border}; }}
QToolButton#HeaderButton {{ color: {t.subtext}; background: transparent; border: none; border-radius: 6px; }}
QToolButton#HeaderButton:hover {{ background: {t.card_hover}; color: {t.text}; }}
QToolButton#HeaderButton:pressed {{ background: {t.card_flash}; }}
#SearchField {{ background: {t.field}; border: 1px solid {t.field_border}; border-radius: 8px; }}
#SearchField:hover {{ border: 1px solid {t.accent}; }}
#SearchIcon {{ color: {t.subtext}; }}
QToolButton#ClearButton {{ color: {t.subtext}; background: transparent; border: none; border-radius: 4px; }}
QToolButton#ClearButton:hover {{ background: {t.card_hover}; color: {t.text}; }}
QToolButton#CrumbBack {{ color: {t.accent}; background: transparent; border: none; border-radius: 5px;
                         padding: 2px 6px; }}
QToolButton#CrumbBack:hover {{ background: {t.card_hover}; }}
#CrumbSlash {{ color: {t.subtext}; }}
QListView {{ background: transparent; border: none; outline: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 0 2px 0; }}
QScrollBar::handle:vertical {{ background: {t.scrollbar}; border-radius: 3px; min-height: 28px; margin: 0 1px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
#Empty {{ color: {t.subtext}; }}
"""


class Panel(QWidget):
    itemClicked = Signal(object)                 # Item：填入
    favoriteClicked = Signal(object)             # Item：点了星标
    itemMenuRequested = Signal(object, QPoint)   # Item, 全局坐标
    projectClicked = Signal(object)              # Project：进入项目
    projectMenuRequested = Signal(object, QPoint)
    newProjectClicked = Signal()
    tabClicked = Signal(str)
    backClicked = Signal()
    searchClicked = Signal()
    searchCleared = Signal()
    pauseClicked = Signal()
    menuClicked = Signal(QPoint)
    hideClicked = Signal()
    hotkeyPressed = Signal(int)
    clipboardUpdated = Signal()
    nativeHandleChanged = Signal()
    geometryEdited = Signal()    # 用户拖动/缩放结束（用来保存位置）
    sizeEdited = Signal()        # 用户拖右下角改了大小
    geometryChanged = Signal()   # 位置或大小有任何变化
    hoverChanged = Signal(bool)  # 鼠标进入/离开面板

    def __init__(self, theme: Theme, thumb_loader: Callable[[int], bytes | None] = lambda _id: None) -> None:
        super().__init__(None)
        # Tool：不出现在任务栏；WindowStaysOnTopHint：置顶；
        # WindowDoesNotAcceptFocus + WA_ShowWithoutActivating：显示时不抢焦点。
        # Qt 的这两个标志在 Windows 上不完全可靠，真正起作用的是 nativeEvent 里
        # 对 WM_MOUSEACTIVATE 的处理和 win32.make_no_activate()。
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)  # 窗口不激活也显示提示
        # 透明窗口：圆角外完全透明，底板按设置的透明度绘制。必须在创建原生窗口之前设置
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("ClipDock")
        self.setMinimumSize(260, 300)
        self.resize(330, 540)
        self.theme = theme
        self.hotkey_text = ""
        self.icon_font, self.has_icon_font = make_icon_font(9.5)
        self._release_pos = QPoint()

        self.root = PanelBackground(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(8, 6, 5, 4)
        layout.setSpacing(6)

        self.header = HeaderBar(self.icon_font, self.has_icon_font)
        self.header.tabClicked.connect(self.tabClicked)
        self.header.pauseClicked.connect(self.pauseClicked)
        self.header.menuClicked.connect(self.menuClicked)
        self.header.hideClicked.connect(self.hideClicked)
        self.header.dragFinished.connect(self.geometryEdited)
        layout.addWidget(self.header)

        self.search = SearchField(self.icon_font, self.has_icon_font)
        self.search.clicked.connect(self.searchClicked)
        self.search.cleared.connect(self.searchCleared)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 3, 0)
        search_row.addWidget(self.search)
        layout.addLayout(search_row)

        self.crumb = CrumbBar()
        self.crumb.backClicked.connect(self.backClicked)
        self.crumb.hide()
        layout.addWidget(self.crumb)

        self.model = RowModel(self)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(24)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(False)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        self.view.clicked.connect(self._on_clicked)
        self.delegate = RowDelegate(self.model, self.view, theme, self.icon_font, self.has_icon_font, thumb_loader)
        self.view.setItemDelegate(self.delegate)

        self.empty = QLabel()
        self.empty.setObjectName("Empty")
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 0, 0)
        footer.setSpacing(4)
        self.status = ElidedLabel()
        footer.addWidget(self.status, 1)
        self.grip = ResizeGrip(self)
        self.grip.resizeFinished.connect(self.geometryEdited)
        self.grip.resizeFinished.connect(self.sizeEdited)
        footer.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(footer)

        self._base_status = ""
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.status.setText(self._base_status))
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._pulse = QVariantAnimation(self)
        self._pulse.setDuration(900)
        self._pulse.setStartValue(1.0)
        self._pulse.setEndValue(0.0)
        self._pulse.setEasingCurve(QEasingCurve.Type.InQuad)  # 先亮着，后面再淡出
        self._pulse.valueChanged.connect(self._on_pulse)
        self.apply_theme(theme)

    # ---- 外观 ----
    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.setStyleSheet(panel_stylesheet(theme))
        self.root.background = QColor(theme.window)
        self.root.border = QColor(theme.border)
        self.root.accent = QColor(theme.accent)
        self.delegate.theme = theme
        self.search.apply_theme(theme)
        self.status.color = QColor(theme.subtext)
        self.crumb.name.color = QColor(theme.text)
        self.grip.color = QColor(theme.subtext)
        self.update()
        self.view.viewport().update()

    def set_background_opacity(self, percent: int) -> None:
        """空白处的不透明度，100 为完全不透明。"""
        self.root.opacity = max(0.1, min(1.0, percent / 100))
        self.root.update()

    # ---- 数据 ----
    def set_rows(self, rows: list[Row], empty_text: str, time_style: str = "clock") -> None:
        bar = self.view.verticalScrollBar()
        keep = bar.value()
        self.delegate.clear_cache()
        self.model.time_style = time_style
        self.model.set_rows(rows)
        if self.model.index_of(self.model.highlight_key) < 0:
            self.model.highlight_key = None
        if rows:
            self.stack.setCurrentWidget(self.view)
            self.view.doItemsLayout()
            bar.setValue(min(keep, bar.maximum()))
        else:
            self.empty.setText(empty_text)
            self.stack.setCurrentWidget(self.empty)

    def set_tab(self, tab: str) -> None:
        self.header.set_tab(tab)
        self.search.set_placeholder("搜索收藏（点这里输入）" if tab == "favorites" else "搜索（点这里输入）")

    def set_project(self, project: Project | None) -> None:
        """进入项目时显示面包屑，回到收藏首页时隐藏。"""
        if project is None:
            self.crumb.hide()
        else:
            self.crumb.set_project(project)
            self.crumb.show()

    def set_status(self, text: str) -> None:
        self._base_status = text
        if not self._toast_timer.isActive():
            self.status.setText(text)

    def show_toast(self, text: str, ms: int = 2600) -> None:
        self.status.setText(text)
        self._toast_timer.start(ms)

    def set_query(self, query: str) -> None:
        self.search.set_query(query)

    def set_paused(self, paused: bool) -> None:
        self.header.set_paused(paused)

    def pulse(self) -> None:
        """面板刚被呼出或换了位置时，边框亮一下再淡出，方便一眼找到它。"""
        self._pulse.stop()
        self._on_pulse(1.0)  # 立刻亮起来，不等动画的第一帧
        self._pulse.start()

    def _on_pulse(self, value) -> None:
        self.root.glow = float(value)
        self.root.update()

    def flash(self, key: tuple) -> None:
        self.model.flash_key = key
        self.view.viewport().update()
        self._flash_timer.start(450)

    def _end_flash(self) -> None:
        self.model.flash_key = None
        self.view.viewport().update()

    def set_highlight(self, key: tuple | None) -> None:
        self.model.highlight_key = key
        row = self.model.index_of(key)
        if row >= 0:
            self.view.scrollTo(self.model.index(row), QAbstractItemView.ScrollHint.EnsureVisible)
        self.view.viewport().update()

    def search_rect_global(self) -> QRect:
        return QRect(self.search.mapToGlobal(QPoint(0, 0)), self.search.size())

    # ---- 鼠标 ----
    def _on_clicked(self, index: QModelIndex) -> None:
        row = self.model.row(index.row())
        if isinstance(row, ItemRow):
            if self.delegate.star_rect(self.view.visualRect(index)).contains(self._release_pos):
                self.favoriteClicked.emit(row.item)
            else:
                self.itemClicked.emit(row.item)
        elif isinstance(row, ProjectRow):
            self.projectClicked.emit(row.project)
        elif isinstance(row, NewProjectRow):
            self.newProjectClicked.emit()

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        row = self.model.row(index.row()) if index.isValid() else None
        where = self.view.viewport().mapToGlobal(pos)
        if isinstance(row, ItemRow):
            self.itemMenuRequested.emit(row.item, where)
        elif isinstance(row, ProjectRow):
            self.projectMenuRequested.emit(row.project, where)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 自己跟踪鼠标悬停（窗口不激活时 Qt 的悬停状态不一定可靠）
        if obj is self.view.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                index = self.view.indexAt(event.position().toPoint())
                row = self.model.row(index.row()) if index.isValid() else None
                hover = row.key if row is not None else None
                if hover != self.model.hover_key:
                    self.model.hover_key = hover
                    self.view.viewport().setCursor(
                        Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor
                    )
                    self.view.viewport().update()
            elif kind == QEvent.Type.MouseButtonRelease:
                self._release_pos = event.position()
            elif kind == QEvent.Type.Leave and self.model.hover_key is not None:
                self.model.hover_key = None
                self.view.viewport().update()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.delegate.clear_cache()
        super().resizeEvent(event)
        self.geometryChanged.emit()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self.geometryChanged.emit()

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.hoverChanged.emit(True)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.hoverChanged.emit(False)

    # ---- Windows 消息 ----
    def nativeEvent(self, event_type, message):  # noqa: N802
        if win32.IS_WINDOWS:
            try:
                msg = win32.MSG.from_address(int(message))
                if msg.message == win32.WM_MOUSEACTIVATE:
                    # 关键：拒绝“点击即激活”，焦点留在原来的输入框
                    return True, win32.MA_NOACTIVATE
                if msg.message == win32.WM_HOTKEY:
                    self.hotkeyPressed.emit(int(msg.wParam))
                    return True, 0
                if msg.message == win32.WM_CLIPBOARDUPDATE:
                    self.clipboardUpdated.emit()
                    return True, 0
            except Exception:  # 这里出错不能让程序崩掉
                log.exception("处理 Windows 消息出错")
        return super().nativeEvent(event_type, message)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WinIdChange:
            QTimer.singleShot(0, self.nativeHandleChanged.emit)
        return super().event(event)


# ---------------------------------------------------------------------------
# 搜索弹层
# ---------------------------------------------------------------------------
def popup_stylesheet(t: Theme) -> str:
    return f"""
#PopupRoot {{ background: {t.field}; border: 1px solid {t.accent}; border-radius: 8px; }}
#SearchIcon {{ color: {t.accent}; }}
#PopupEdit {{ background: transparent; border: none; color: {t.text};
             selection-background-color: {t.accent}; selection-color: #FFFFFF; }}
"""


class SearchPopup(QWidget):
    """盖在面板搜索框上的真正输入框。它会临时拿到焦点，用完由控制器把焦点还回去。"""

    textEdited = Signal(str)
    accepted = Signal()
    cancelled = Signal()
    navigate = Signal(int)
    forward = Signal()    # → ：进入选中的项目
    backward = Signal()   # ← ：从项目返回
    switchTab = Signal()  # Tab：切换 最近 / 收藏
    dismissed = Signal()

    def __init__(self, theme: Theme, icon_font: QFont, has_icons: bool) -> None:
        super().__init__(None)
        self.setObjectName("Popup")
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)  # 圆角外透明，和面板融为一体
        self.suppress_dismiss = False
        self.root = QFrame(self)
        self.root.setObjectName("PopupRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        layout = QHBoxLayout(self.root)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(6)
        self.icon = QLabel(glyph("search", has_icons))
        self.icon.setObjectName("SearchIcon")
        self.icon.setFont(icon_font)
        self.edit = QLineEdit()
        self.edit.setObjectName("PopupEdit")
        self.edit.setFrame(False)
        self.edit.setPlaceholderText("↑↓ 选择 · 回车填入 · → 进项目 · Tab 切页")
        layout.addWidget(self.icon)
        layout.addWidget(self.edit, 1)
        self.edit.textEdited.connect(self.textEdited)
        self.edit.installEventFilter(self)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        self.setStyleSheet(popup_stylesheet(theme))
        palette = self.edit.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.subtext))
        self.edit.setPalette(palette)

    def open_at(self, rect: QRect, text: str) -> None:
        self.setGeometry(rect)
        self.edit.setText(text)
        self.show()
        self.raise_()
        self.activateWindow()
        # 用户刚点过面板，本程序有资格切到前台；Qt 的 activateWindow 有时不够，再补一下
        win32.force_foreground(int(self.winId()))
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.edit.selectAll()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            at_end = self.edit.cursorPosition() == len(self.edit.text()) and not self.edit.hasSelectedText()
            at_start = self.edit.cursorPosition() == 0 and not self.edit.hasSelectedText()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.accepted.emit()
                return True
            if key == Qt.Key.Key_Escape:
                self.cancelled.emit()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.switchTab.emit()
                return True
            if key == Qt.Key.Key_Right and at_end:  # 光标在最后时，→ 留给“进入项目”
                self.forward.emit()
                return True
            if key == Qt.Key.Key_Left and at_start:  # 光标在最前时，← 留给“返回”
                self.backward.emit()
                return True
            steps = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 5, Qt.Key.Key_PageUp: -5}
            if key in steps:
                self.navigate.emit(steps[key])
                return True
        return super().eventFilter(obj, event)

    def event(self, event) -> bool:
        # 用户点了别的程序：通知控制器收起搜索框
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible() and not self.suppress_dismiss:
            QTimer.singleShot(0, self.dismissed.emit)
        return super().event(event)


# ---------------------------------------------------------------------------
# 复制成功提示
# ---------------------------------------------------------------------------
class LimitDialog(QDialog):
    """改一个存储上限：数字框 + “不限”。另一个上限已经是“不限”时，这里不能再选“不限”。"""

    def __init__(self, title: str, text: str, unit: str, value: int, default: int,
                 value_range: tuple[int, int], step: int, can_be_unlimited: bool) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)  # 别被置顶的面板挡住
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        self.spin = QSpinBox()
        self.spin.setRange(*value_range)
        self.spin.setSingleStep(step)
        self.spin.setSuffix(f" {unit}")
        self.spin.setGroupSeparatorShown(True)
        self.spin.setValue(value or default)
        self.spin.setMinimumWidth(140)
        self.unlimited = QCheckBox("不限")
        self.unlimited.setChecked(value == 0)
        self.unlimited.setEnabled(can_be_unlimited or value == 0)
        self.spin.setEnabled(value != 0)
        self.unlimited.toggled.connect(lambda checked: self.spin.setEnabled(not checked))
        row.addWidget(self.spin, 1)
        row.addWidget(self.unlimited)
        layout.addLayout(row)
        if not self.unlimited.isEnabled():
            hint = QLabel("另一个上限已经是“不限”，这里至少要设一个上限，否则历史会一直变多。")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray;")
            layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(340)

    def value(self) -> int:
        """0 表示不限。"""
        return 0 if self.unlimited.isChecked() else self.spin.value()


class CopyToast(QWidget):
    """复制成功时在鼠标旁边弹出的小提示：不抢焦点、鼠标可以穿透、一秒多后淡出。"""

    SHOW_MS = 1300

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput  # 鼠标穿透，不挡操作
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        font = QFont(QGuiApplication.font())
        font.setPointSizeF(font.pointSizeF() + 0.5)
        self.setFont(font)
        self.text = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(260)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)

    def show_message(self, text: str, near: QPoint) -> None:
        self.text = text
        metrics = QFontMetrics(self.font())
        width = min(440, metrics.horizontalAdvance(text) + 48)
        height = max(30, metrics.height() + 14)
        self.resize(width, height)
        screen = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x, y = near.x() + 14, near.y() + 20       # 鼠标右下方
        if x + width > area.right():
            x = near.x() - width - 8
        if y + height > area.bottom():
            y = near.y() - height - 10
        x = max(area.left(), min(x, area.right() - width))
        y = max(area.top(), min(y, area.bottom() - height))
        self.move(x, y)
        self._fade.stop()
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            self.show()
        self.update()
        self._timer.start(self.SHOW_MS)

    def _fade_out(self) -> None:
        self._fade.start()

    def _finish(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        rect = QRectF(0.5, 0.5, w - 1, h - 1)
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(QColor(32, 33, 36, 235))  # 深色小药丸，任何背景上都看得清
        painter.drawRoundedRect(rect, h / 2 - 0.5, h / 2 - 0.5)
        circle = QRectF(9, (h - 18) / 2, 18, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2EA043"))
        painter.drawEllipse(circle)
        pen = QPen(QColor("#FFFFFF"), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        check = QPainterPath()
        check.moveTo(circle.left() + 4.6, circle.center().y() + 0.4)
        check.lineTo(circle.left() + 7.8, circle.bottom() - 5.0)
        check.lineTo(circle.right() - 4.4, circle.top() + 5.4)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(check)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(self.font())
        text_rect = QRectF(circle.right() + 8, 0, w - circle.right() - 18, h)
        text = QFontMetrics(self.font()).elidedText(self.text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, ALIGN_LEFT_VCENTER, text)
