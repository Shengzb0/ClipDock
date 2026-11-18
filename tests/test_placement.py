"""面板放在输入光标旁边哪里：纯计算，不需要窗口。坐标按 1536×864 的笔记本屏幕（125% 缩放）来设。"""
from PySide6.QtCore import QPointF, QRect, QSize

from clipdock import placement
from clipdock.placement import Box, place_near_caret, protected_zone, still_good

SCREEN = QRect(0, 0, 1536, 816)     # 可用区域（去掉任务栏）
PANEL = QSize(340, 580)
AREA = Box(8, 8, 1528, 808)         # 去掉屏幕边距


def caret(x, y, h=20):
    return QRect(x, y, 2, h)


def off_the_line(rect, at, window=None):
    """面板整个在光标那一行（含输入法候选框）的上面或下面，不在同一高度。"""
    box, zone = Box.of(rect), protected_zone(at, window)
    return box.bottom <= zone.top or box.top >= zone.bottom


def test_never_on_the_same_line_anywhere_on_screen():
    # 光标走遍整块屏幕：面板都在屏幕里、不和光标同一高度、不比 300 矮
    for x in range(0, 1536, 64):
        for y in range(0, 800, 24):
            for window, outside in ((SCREEN, False), (QRect(300, 120, 900, 560), True)):
                at = caret(x, y)
                rect, rule = place_near_caret(at, PANEL, SCREEN, window, outside)
                assert Box.of(rect).inside(AREA), (x, y, rule)
                assert off_the_line(rect, at, window), (x, y, rule)
                assert rect.height() >= placement.MIN_HEIGHT, (x, y, rule)


def test_chat_window_not_maximized_goes_beside_the_window_above_the_line():
    # 微信那样居中的小窗口，光标在底部输入框：贴在窗口右侧、光标那一行上面，不挡聊天记录
    window = QRect(368, 132, 800, 600)
    at = caret(680, 600)
    rect, rule = place_near_caret(at, PANEL, SCREEN, window, outside_ok=True)
    assert rule == "窗口右侧、光标上方"
    assert rect.x() == 368 + 800 + placement.GAP
    assert off_the_line(rect, at, window)


def test_maximized_window_goes_above_like_a_dropdown():
    # 最大化的浏览器里填表：上面放得下就放上面（挡的是填好的，下面要填的不挡）
    at = caret(580, 400)
    rect, rule = place_near_caret(at, PANEL, SCREEN, SCREEN)
    assert rule == "光标上方"
    assert rect.x() == 580 - 20                                  # 和光标左对齐
    assert rect.bottom() < 400 - 6 - placement.GAP + 1           # 底边在光标那一行上面
    assert rect.height() == 400 - 6 - placement.GAP - 8          # 上面只有这么高：面板变矮


def test_near_the_top_goes_below_past_the_input_method():
    at = caret(580, 120)
    rect, rule = place_near_caret(at, PANEL, SCREEN, SCREEN)
    assert rule == "光标下方"
    assert rect.y() == 120 + 20 + placement.IME_ROOM + placement.GAP   # 让开输入法候选框
    assert rect.height() == PANEL.height()                              # 下面宽敞，不用变矮


def test_above_first_then_below():
    rect, rule = place_near_caret(caret(580, 700), PANEL, SCREEN, SCREEN)
    assert rule == "光标上方" and rect.height() == PANEL.height()
    # 上面只有 306 高、下面更宽敞：上面放得下，还是放上面（面板矮一点）
    rect, rule = place_near_caret(caret(580, 330), PANEL, SCREEN, SCREEN)
    assert rule == "光标上方" and rect.height() == 306
    # 上面只剩 276，放不下：放下面
    rect, rule = place_near_caret(caret(580, 300), PANEL, SCREEN, SCREEN)
    assert rule == "光标下方" and rect.height() == 808 - (320 + placement.IME_ROOM + placement.GAP)


def test_caret_near_right_edge_shifts_left_but_stays_off_the_line():
    at = caret(1500, 600)
    rect, rule = place_near_caret(at, PANEL, SCREEN, SCREEN)
    assert rule == "光标上方" and rect.right() == 1528 - 1
    assert off_the_line(rect, at, SCREEN)


def test_window_snapped_to_the_right_uses_space_on_its_left():
    window = QRect(768, 0, 768, 816)
    rect, rule = place_near_caret(caret(900, 600), PANEL, SCREEN, window, outside_ok=True)
    assert rule == "窗口左侧、光标上方"
    assert rect.x() + rect.width() == 768 - placement.GAP


def test_window_side_must_be_close_to_the_caret():
    # 窗口很宽、光标在最左边：贴到窗口外侧就离光标太远了，改放光标上下
    window = QRect(100, 50, 1100, 700)
    rect, rule = place_near_caret(caret(150, 600), PANEL, SCREEN, window, outside_ok=True)
    assert rule == "光标上方"


def test_short_screen_falls_back_to_the_side():
    # 屏幕太矮，上下都放不下 300 高的面板：只好放到同一高度，在要写的文字右边
    short = QRect(0, 0, 800, 500)
    at = caret(100, 230)
    rect, rule = place_near_caret(at, PANEL, short)
    assert rule == "光标右侧"
    assert Box.of(rect).overlap(protected_zone(at)) == 0


def test_tiny_screen_squeezes_in():
    small = QRect(0, 0, 500, 400)
    rect, rule = place_near_caret(caret(200, 200), PANEL, small)
    assert rule.endswith("（挤一挤）")
    assert Box.of(rect).inside(Box.of(small))


def test_text_room_stops_at_the_window_edge():
    window = QRect(200, 200, 260, 160)
    assert protected_zone(caret(220, 260), window).right == 460


def test_still_good_avoids_jumping_around():
    at = caret(580, 400)
    rect, _ = place_near_caret(at, PANEL, SCREEN, SCREEN)
    assert still_good(rect, at, SCREEN, SCREEN)
    assert still_good(rect, caret(700, 420), SCREEN, SCREEN)       # 往右写了几个字、换了一行：不动
    assert not still_good(rect, caret(580, 300), SCREEN, SCREEN)   # 光标挪到面板那个高度了
    assert not still_good(rect, caret(580, 700), SCREEN, SCREEN)   # 离得太远
    assert not still_good(rect, caret(1400, 420), SCREEN, SCREEN)  # 横着离得太远


def test_native_to_logical_with_mixed_scaling():
    # 主屏 1920×1080 物理像素、150% 缩放；右边副屏 1920×1080、100%
    screens = [(QRect(0, 0, 1280, 720), 1.5), (QRect(1920, 0, 1920, 1080), 1.0)]
    assert placement.native_to_logical(960, 540, screens) == QPointF(640, 360)
    assert placement.native_to_logical(2000, 100, screens) == QPointF(2000, 100)
    assert placement.native_rect_to_logical(300, 150, 303, 180, screens) == QRect(200, 100, 2, 20)
    assert placement.native_to_logical(5, 5, []) == QPointF(5, 5)
