"""界面冒烟测试：在无窗口环境（Qt offscreen）里跑完整的控制器，
把 win32 换成假的，用真实的鼠标点击、键盘输入和 Qt 剪贴板检查主要流程。"""
import json
import os
import time

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from clipdock import app as app_module
from clipdock import clipdata, placement, win32
from clipdock.config import Config
from clipdock.store import HistoryStore
from clipdock.ui import HeaderRow, ItemRow, NewProjectRow, ProjectRow

TARGET = 777        # 假装这是用户正在输入的那个程序的窗口
OTHER_PID = 4321    # 假装这是“微信”的进程
ESC = app_module.ESC_HOTKEY_ID


class FakeWin:
    def __init__(self):
        self.foreground = TARGET
        self.seq = 10
        self.owner_pid = 0
        self.next_owner = None
        self.private = False
        self.pastes = []
        self.focus_calls = []
        self.fullscreen = 0
        self.elevated = False
        self.hotkeys = set()
        self.escapes = 0
        self.boot = 1_700_000_000.0
        self.format_count = 0
        self.caret = (60, 300, 2, 18)      # 输入光标（屏幕坐标）；None 表示问不到
        self.window = (0, 0, 800, 800)     # 目标窗口的位置 (左, 上, 右, 下)
        self.maximized = True

    def on_clipboard_changed(self):
        # 模拟系统：剪贴板一变，序号加一，记下是谁放的（默认是 ClipDock 自己）
        self.seq += 1
        self.owner_pid = self.next_owner if self.next_owner is not None else os.getpid()
        self.next_owner = None

    def send_ctrl_v(self):
        mime = QGuiApplication.clipboard().mimeData()
        if mime.hasImage():
            image = QImage(mime.imageData())
            content = f"image {image.width()}x{image.height()}"
        elif mime.hasUrls() and not mime.hasFormat("text/plain"):
            content = [u.toLocalFile() for u in mime.urls()]
        else:
            content = mime.text()
        self.pastes.append((self.foreground, content))
        return True

    def force_foreground(self, hwnd):
        self.focus_calls.append(hwnd)
        self.foreground = hwnd
        return True

    def register_hotkey(self, hwnd, hotkey_id, mods, vk):
        self.hotkeys.add(hotkey_id)
        return True

    def unregister_hotkey(self, hwnd, hotkey_id):
        self.hotkeys.discard(hotkey_id)

    def send_escape(self):
        self.escapes += 1
        return True


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    fake = FakeWin()
    patches = {
        "get_foreground_window": lambda: fake.foreground,
        "send_ctrl_v": fake.send_ctrl_v,
        "force_foreground": fake.force_foreground,
        "clipboard_sequence": lambda: fake.seq,
        "clipboard_owner_pid": lambda: fake.owner_pid,
        "clipboard_is_private": lambda: fake.private,
        "window_pid": lambda h: OTHER_PID if h == TARGET else 0,
        "is_window_elevated": lambda h: fake.elevated,
        "fullscreen_foreground": lambda pid, mon=0: fake.fullscreen,
        "process_name": lambda pid: "WeChat.exe" if pid == OTHER_PID else "",
        "register_hotkey": fake.register_hotkey,
        "unregister_hotkey": fake.unregister_hotkey,
        "send_escape": fake.send_escape,
        "boot_time": lambda: fake.boot,
        "clipboard_format_count": lambda: fake.format_count,
        "caret_rect": lambda timeout=0.3: fake.caret,
        "window_rect": lambda h: fake.window if h == TARGET else None,
        "is_maximized": lambda h: fake.maximized,
    }
    for name, value in patches.items():
        monkeypatch.setattr(win32, name, value)
    clipboard = QGuiApplication.clipboard()
    clipboard.dataChanged.connect(fake.on_clipboard_changed)
    store = HistoryStore(tmp_path / "h.db")
    ctrl = app_module.Controller(qapp, Config(show_on_start=True), tmp_path / "config.json", store, tmp_path)
    QTest.qWait(20)
    yield ctrl, fake, store
    clipboard.dataChanged.disconnect(fake.on_clipboard_changed)
    ctrl.shutdown()
    for widget in (ctrl.panel, ctrl.popup, ctrl.toast):
        widget.close()
    ctrl.server.close()


def copy_from_app(ctrl, fake, *, text=None, html=None, image=None, files=None):
    """模拟别的程序（微信）复制了东西。"""
    mime = QMimeData()
    if files:
        mime.setUrls([QUrl.fromLocalFile(str(f)) for f in files])
    if text is not None:
        mime.setText(text)
    if html is not None:
        mime.setHtml(html)
    if image is not None:
        mime.setImageData(image)
    fake.next_owner = OTHER_PID
    QGuiApplication.clipboard().setMimeData(mime)
    ctrl._capture_clipboard()


def make_image(w, h, color="#3366cc"):
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    return image


def row_of(ctrl, item_id):
    return ctrl.panel.model.index_of(("item", item_id))


def click_row(ctrl, row, star=False, button=Qt.MouseButton.LeftButton):
    view = ctrl.panel.view
    rect = view.visualRect(ctrl.panel.model.index(row, 0))
    point = ctrl.panel.delegate.star_rect(rect).center().toPoint() if star else rect.center()
    QTest.mouseClick(view.viewport(), button, Qt.KeyboardModifier.NoModifier, point)


def type_text(widget, text):
    """逐字发送带文字的按键事件（QTest.keyClicks 不支持中文）。"""
    for ch in text:
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key.Key_unknown, Qt.KeyboardModifier.NoModifier, ch))


def rows(ctrl):
    model = ctrl.panel.model
    return [model.row(i) for i in range(model.rowCount())]


# ---------------------------------------------------------------------------
# 复制：识别类型 + 成功提示
# ---------------------------------------------------------------------------
def test_copy_text_records_and_shows_toast(env):
    ctrl, fake, store = env
    copy_from_app(ctrl, fake, text="张三")
    item = store.history()[0]
    assert item.preview == "张三" and item.source == "微信" and item.kind == "text"
    assert ctrl.toast.isVisible() and ctrl.toast.text == "已复制「张三」"
    QTest.qWait(ctrl.toast.SHOW_MS + 450)
    assert not ctrl.toast.isVisible()  # 一秒多后自己淡出
    # 带格式的文字：存下 HTML，卡片上注明“带格式”
    copy_from_app(ctrl, fake, text="加粗", html="<b>加粗</b>")
    rich = store.history()[0]
    assert rich.meta == {"rich": True} and b"<b>" in store.formats_of(rich.id)[clipdata.HTML]
    row = ctrl.panel.model.row(row_of(ctrl, rich.id))
    assert "带格式" in ctrl.panel.delegate.meta_text(row)


def test_copy_image_and_files(env, tmp_path):
    ctrl, fake, store = env
    copy_from_app(ctrl, fake, image=make_image(1200, 600))
    image_item = store.history()[0]
    assert image_item.kind == "image" and image_item.meta == {"w": 1200, "h": 600}
    assert "图片 1200×600" in ctrl.toast.text
    assert store.format_of(image_item.id, clipdata.THUMB)
    doc = tmp_path / "报告.docx"
    doc.write_text("x")
    copy_from_app(ctrl, fake, files=[doc])
    file_item = store.history()[0]
    assert file_item.kind == "files" and file_item.paths == [str(doc)]
    assert ctrl.toast.text == "已复制 报告.docx"
    ctrl.panel.grab()  # 缩略图、文件图标都能画出来
    doc.unlink()
    ctrl.hide_panel()
    ctrl.show_panel(from_user=True)  # 再次显示面板时重新检查文件还在不在
    assert ctrl.panel.model.row(row_of(ctrl, file_item.id)).missing


def test_private_paused_and_toggle_feedback(env):
    ctrl, fake, store = env
    fake.private = True
    copy_from_app(ctrl, fake, text="password123")
    assert store.counts() == (0, 0) and "隐私" in ctrl.toast.text
    fake.private = False
    ctrl._set_paused(True)
    copy_from_app(ctrl, fake, text="暂停期间")
    assert store.counts() == (0, 0) and "暂停记录中" in ctrl.toast.text
    ctrl._set_paused(False)
    ctrl.toast.hide()
    ctrl._set_copy_feedback(False)
    copy_from_app(ctrl, fake, text="不提示")
    assert store.counts() == (1, 0) and not ctrl.toast.isVisible()
    # 剪贴板被清空（不是复制）：不提示也不记录
    ctrl._set_copy_feedback(True)
    fake.next_owner = OTHER_PID
    QGuiApplication.clipboard().setMimeData(QMimeData())
    ctrl._capture_clipboard()
    assert not ctrl.toast.isVisible()
    # 全屏程序在前台：照常记录，但不弹提示
    fake.fullscreen = 555
    copy_from_app(ctrl, fake, text="全屏时复制")
    assert store.history()[0].preview == "全屏时复制" and not ctrl.toast.isVisible()


def test_busy_clipboard_is_retried(env, monkeypatch):
    ctrl, fake, store = env
    clipboard = QGuiApplication.clipboard()
    real = clipboard.mimeData
    state = {"busy": 2}

    def flaky():
        if state["busy"]:
            state["busy"] -= 1
            return QMimeData()  # 读的那一刻剪贴板被别的程序占着
        return real()

    monkeypatch.setattr(clipboard, "mimeData", flaky)
    fake.format_count = 3
    copy_from_app(ctrl, fake, text="稍后才读到")
    assert store.counts() == (0, 0)
    QTest.qWait(app_module.CLIPBOARD_RETRY_MS * 3 + 80)
    assert store.history()[0].preview == "稍后才读到" and "稍后才读到" in ctrl.toast.text


# ---------------------------------------------------------------------------
# 填入
# ---------------------------------------------------------------------------
def test_click_fills_and_own_paste_is_not_recorded_again(env, tmp_path):
    ctrl, fake, store = env
    copy_from_app(ctrl, fake, text="加粗", html="<b>加粗</b>")
    copy_from_app(ctrl, fake, image=make_image(40, 30))
    doc = tmp_path / "a.txt"
    doc.write_text("x")
    copy_from_app(ctrl, fake, files=[doc])
    ctrl.toast.hide()
    text_item, image_item, file_item = store.history()[::-1]
    for item in (text_item, image_item, file_item):
        click_row(ctrl, row_of(ctrl, item.id))
        QTest.qWait(30)
        ctrl._capture_clipboard()  # 自己放进剪贴板的内容不会再被记录一次
    assert fake.pastes == [(TARGET, "加粗"), (TARGET, "image 40x30"), (TARGET, [str(doc)])]
    assert store.counts() == (3, 0) and not ctrl.toast.isVisible()
    assert fake.focus_calls == []  # 普通点击不需要切换焦点
    # 文件已经不在了：不填入，给出提示
    doc.unlink()
    click_row(ctrl, row_of(ctrl, file_item.id))
    QTest.qWait(30)
    assert len(fake.pastes) == 3 and "不在原来的位置" in ctrl.panel.status.text()


def test_elevated_target_only_copies(env):
    ctrl, fake, store = env
    ctrl.record_text("x")
    fake.elevated = True
    click_row(ctrl, row_of(ctrl, store.history()[0].id))
    QTest.qWait(30)
    assert QGuiApplication.clipboard().text() == "x" and fake.pastes == []
    assert "管理员" in ctrl.panel.status.text()


# ---------------------------------------------------------------------------
# 最近：连续复制分组
# ---------------------------------------------------------------------------
def test_recent_is_grouped_by_bursts(env):
    ctrl, fake, store = env
    now = time.time()
    for text, minutes_ago in (("a", 0), ("b", 2), ("c", 30), ("d", 33), ("e", 36)):
        store.add(text, "Chrome", now=now - minutes_ago * 60)
    ctrl.reload()
    kinds = [type(r).__name__ for r in rows(ctrl)]
    assert kinds == ["HeaderRow", "ItemRow", "ItemRow", "HeaderRow", "ItemRow", "ItemRow", "ItemRow"]
    headers = [r.label for r in rows(ctrl) if isinstance(r, HeaderRow)]
    assert headers[0].endswith("· 2 条") and headers[1].endswith("· 3 条")
    # 分组标题点了没反应
    click_row(ctrl, 0)
    QTest.qWait(20)
    assert fake.pastes == []


# ---------------------------------------------------------------------------
# 收藏与项目
# ---------------------------------------------------------------------------
def test_star_toggles_favorite_and_favorites_tab(env):
    ctrl, fake, store = env
    for text in ("学号 2023", "邮箱 a@b.com"):
        ctrl.record_text(text, "Chrome")
    target = store.history()[1]
    click_row(ctrl, row_of(ctrl, target.id), star=True)
    QTest.qWait(20)
    assert store.get(target.id).favorite and fake.pastes == []  # 点星标只收藏，不填入
    assert ctrl.panel.header.tab_favorites.text() == "收藏 1"
    QTest.mouseClick(ctrl.panel.header.tab_favorites, Qt.MouseButton.LeftButton)
    assert ctrl._tab == "favorites"
    assert [type(r).__name__ for r in rows(ctrl)] == ["NewProjectRow", "HeaderRow", "ItemRow"]
    click_row(ctrl, 2)  # 收藏页里点一下照样填入
    QTest.qWait(30)
    assert fake.pastes == [(TARGET, "学号 2023")]
    click_row(ctrl, 2, star=True)  # 再点星标取消收藏
    QTest.qWait(20)
    assert store.counts()[1] == 0 and ctrl.panel.stack.currentWidget() is ctrl.panel.empty


def test_projects_move_enter_back_and_keyboard(env, monkeypatch):
    ctrl, fake, store = env
    for text in ("作业要求", "提交地址", "随手记"):
        ctrl.record_text(text)
    ids = {i.preview: i.id for i in store.history()}
    monkeypatch.setattr(ctrl, "_ask_text", lambda *a, **k: "课程作业")
    ctrl._new_project(store.get(ids["作业要求"]))   # 右键 → 移到项目 → 新建项目…
    project = store.projects()[0]
    ctrl.move_to_project(store.get(ids["提交地址"]), project.id, project.name)
    ctrl.toggle_favorite(store.get(ids["随手记"]))
    ctrl.switch_tab("favorites")
    assert [type(r).__name__ for r in rows(ctrl)] == ["ProjectRow", "NewProjectRow", "HeaderRow", "ItemRow"]
    assert rows(ctrl)[0].project.count == 2 and rows(ctrl)[2].label == "未分类 · 1 条"
    # 点项目那一行进入
    click_row(ctrl, 0)
    QTest.qWait(20)
    assert ctrl._project_id == project.id and ctrl.panel.crumb.isVisible()
    assert {r.item.preview for r in rows(ctrl)} == {"作业要求", "提交地址"}
    QTest.mouseClick(ctrl.panel.crumb.back, Qt.MouseButton.LeftButton)
    assert ctrl._project_id is None and not ctrl.panel.crumb.isVisible()
    # 键盘：搜索框里 → 进项目，← 返回，Tab 切页签
    ctrl.begin_search()
    fake.foreground = 999
    assert ctrl.panel.model.highlight_key == ("project", project.id)
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Right)
    assert ctrl._project_id == project.id
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Down)
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Return)
    QTest.qWait(app_module.PASTE_AFTER_FOCUS_MS + 60)
    assert fake.pastes[-1][0] == TARGET and fake.pastes[-1][1] in ("作业要求", "提交地址")
    ctrl.begin_search()
    fake.foreground = 999
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Left)
    assert ctrl._project_id is None and ctrl.panel.model.highlight_key == ("project", project.id)
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Tab)
    assert ctrl._tab == "recent"
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Escape)
    assert not ctrl.popup.isVisible()


def test_search_all_favorites_shows_project_and_rename_delete(env, monkeypatch):
    ctrl, fake, store = env
    ctrl.record_text("简历 PDF 链接")
    ctrl.record_text("其他")
    item = store.history()[1]
    project = store.create_project("求职")
    ctrl.move_to_project(item, project.id, project.name)
    ctrl.switch_tab("favorites")
    ctrl.begin_search()
    type_text(ctrl.popup.edit, "简历")
    found = [r for r in rows(ctrl) if isinstance(r, ItemRow)]
    assert len(found) == 1 and "项目：求职" in ctrl.panel.delegate.meta_text(found[0])
    ctrl._cancel_search()
    monkeypatch.setattr(ctrl, "_ask_text", lambda *a, **k: "找工作")
    ctrl._rename_project(store.project(project.id))
    assert store.project(project.id).name == "找工作"
    monkeypatch.setattr(ctrl, "_confirm", lambda *a, **k: True)
    ctrl.enter_project(store.project(project.id))
    ctrl._delete_project(store.project(project.id))
    assert store.projects() == [] and ctrl._project_id is None
    assert store.get(item.id).favorite  # 项目删了，收藏还在
    assert any(isinstance(r, NewProjectRow) for r in rows(ctrl))
    assert not any(isinstance(r, ProjectRow) for r in rows(ctrl))


def test_copy_paths_for_files(env, tmp_path):
    ctrl, fake, store = env
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("1")
    b.write_text("2")
    copy_from_app(ctrl, fake, files=[a, b])
    ctrl._copy_paths(store.history()[0])
    assert QGuiApplication.clipboard().text() == f"{a}\r\n{b}"


# ---------------------------------------------------------------------------
# 搜索、Esc、全屏、背景（之前的功能）
# ---------------------------------------------------------------------------
def test_search_flow_returns_focus_then_pastes(env):
    ctrl, fake, store = env
    for text in ["学号 20230001", "邮箱 a@b.com", "手机 13800000000", "学院 计算机学院"]:
        ctrl.record_text(text, "Word")
    QTest.mouseClick(ctrl.panel.search, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 10))
    assert ctrl.popup.isVisible() and ctrl._search_target == TARGET
    fake.foreground = 999  # 搜索框临时拿到了焦点
    type_text(ctrl.popup.edit, "学")
    found = [r.item for r in rows(ctrl) if isinstance(r, ItemRow)]
    assert len(found) == 2 and ctrl.panel.status.text().startswith("找到 2 条")
    assert ctrl.panel.model.highlight_key == ("item", found[0].id)
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Down)
    assert ctrl.panel.model.highlight_key == ("item", found[1].id)
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Return)
    QTest.qWait(app_module.PASTE_AFTER_FOCUS_MS + 60)
    assert not ctrl.popup.isVisible()
    assert fake.focus_calls[-1] == TARGET and fake.pastes[-1] == (TARGET, found[1].preview)


def test_escape_hides_only_while_hovering(env, monkeypatch):
    ctrl, fake, store = env
    ctrl._on_panel_hover(True)
    assert ESC in fake.hotkeys
    ctrl.begin_search()
    assert ESC not in fake.hotkeys
    QTest.keyClick(ctrl.popup.edit, Qt.Key.Key_Escape)
    assert not ctrl.popup.isVisible() and ESC in fake.hotkeys
    monkeypatch.setattr(ctrl, "_cursor_over_panel", lambda: True)
    ctrl._on_hotkey(ESC)
    assert not ctrl.panel.isVisible() and ESC not in fake.hotkeys
    ctrl.show_panel(from_user=True)
    ctrl._on_panel_hover(True)
    monkeypatch.setattr(ctrl, "_cursor_over_panel", lambda: False)
    ctrl._on_hotkey(ESC)
    assert ctrl.panel.isVisible() and ESC not in fake.hotkeys and fake.escapes == 1


def test_fullscreen_hides_and_hotkey_override(env):
    ctrl, fake, store = env
    fake.fullscreen = 555
    ctrl._check_fullscreen()
    assert not ctrl.panel.isVisible()
    fake.fullscreen = 0
    ctrl._check_fullscreen()
    assert ctrl.panel.isVisible()
    fake.fullscreen = 555
    ctrl._check_fullscreen()
    ctrl._on_hotkey(app_module.HOTKEY_ID)
    assert ctrl.panel.isVisible()
    ctrl._check_fullscreen()
    assert ctrl.panel.isVisible()


# ---------------------------------------------------------------------------
# 快捷键呼出：输入光标旁边 / 上次的位置
# ---------------------------------------------------------------------------
HOTKEY = app_module.HOTKEY_ID


def caret_zone(ctrl, fake):
    x, y, w, h = fake.caret
    return placement.protected_zone(QRect(x, y, w, h), QRect(0, 0, 800, 800))


def off_the_line(ctrl, fake):
    box, zone = placement.Box.of(ctrl.panel.geometry()), caret_zone(ctrl, fake)
    return box.bottom <= zone.top or box.top >= zone.bottom


def test_hotkey_puts_panel_near_caret_but_off_its_line(env):
    ctrl, fake, store = env
    ctrl.hide_panel()
    ctrl._on_hotkey(HOTKEY)
    first = ctrl.panel.geometry()
    assert ctrl.panel.isVisible() and off_the_line(ctrl, fake)
    # 光标上面只有 276 高，下面有 400：放到下面（让开输入法候选框），和光标左对齐，面板临时变矮
    assert first.x() == 60 - 20 and first.y() == 300 + 18 + placement.IME_ROOM + placement.GAP
    assert first.height() == 400
    assert ctrl.panel.root.glow > 0                                       # 边框亮一下
    # 再按一次：不再隐藏；光标没怎么动，面板也不动
    fake.caret = (90, 305, 2, 18)
    ctrl._on_hotkey(HOTKEY)
    assert ctrl.panel.isVisible() and ctrl.panel.geometry() == first
    # 换到下面的输入框再按：面板跟过去，这回上面宽敞，放到上面
    fake.caret = (420, 700, 2, 18)
    ctrl._on_hotkey(HOTKEY)
    moved = ctrl.panel.geometry()
    assert moved != first and off_the_line(ctrl, fake) and moved.bottom() < 700
    assert moved.height() == 580
    ctrl._sync_menu()
    assert ctrl.act_toggle.text() == "隐藏面板"
    assert ctrl.act_position_caret.isChecked() and not ctrl.act_position_last.isChecked()


def test_preferred_size_is_kept(env):
    ctrl, fake, store = env
    preferred = ctrl.panel.size()
    ctrl.hide_panel()
    ctrl._on_hotkey(HOTKEY)                     # 为了放得下临时变矮
    assert ctrl.panel.height() < preferred.height()
    ctrl._save_geometry()
    assert ctrl.cfg.geometry[2:] == [preferred.width(), preferred.height()]  # 记住的还是你拖出来的大小
    ctrl._set_popup_position("last")
    ctrl.hide_panel()
    ctrl._on_hotkey(HOTKEY)                     # “上次的位置”：大小恢复，整个面板在屏幕里
    assert ctrl.panel.size() == preferred
    assert placement.Box.of(ctrl.panel.geometry()).inside(placement.Box(0, 0, 800, 800))
    ctrl.panel.resize(360, 500)                 # 拖右下角改了大小：以后按这个来
    ctrl.panel.sizeEdited.emit()
    ctrl._save_geometry()
    assert ctrl.cfg.geometry[2:] == [360, 500]


def test_last_position_mode(env, tmp_path):
    ctrl, fake, store = env
    ctrl._set_popup_position("last")
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["popup_position"] == "last"
    ctrl._sync_menu()
    assert ctrl.act_position_last.isChecked() and not ctrl.act_position_caret.isChecked()
    before = ctrl.panel.geometry()
    ctrl.hide_panel()
    ctrl._on_hotkey(HOTKEY)
    assert ctrl.panel.isVisible() and ctrl.panel.geometry() == before    # 原地出现
    ctrl._on_hotkey(HOTKEY)
    assert ctrl.panel.isVisible() and ctrl.panel.geometry() == before    # 快捷键也不再隐藏


def test_beside_small_window_and_mouse_fallback(env):
    ctrl, fake, store = env
    fake.maximized = False
    fake.window = (40, 100, 400, 500)      # 一个没最大化的小窗口
    fake.caret = (100, 300, 2, 18)
    ctrl.hide_panel()
    ctrl._on_hotkey(HOTKEY)
    assert ctrl.panel.geometry().x() == 400 + placement.GAP          # 贴在窗口右侧，不挡这个程序
    # 问不到光标：稍等再问一次，还是没有就放在鼠标旁边
    fake.caret = None
    ctrl.hide_panel()
    QCursor.setPos(150, 200)
    ctrl._on_hotkey(HOTKEY)
    assert not ctrl.panel.isVisible()
    QTest.qWait(app_module.CARET_RETRY_MS + 80)
    assert ctrl.panel.isVisible() and "鼠标旁边" in ctrl.panel.status.text()


def test_background_is_translucent_but_cards_are_not(env, tmp_path):
    ctrl, fake, store = env
    ctrl.record_text("一条记录")
    ctrl._set_opacity(55)
    QTest.qWait(20)
    image = ctrl.panel.grab().toImage()
    w, h = image.width(), image.height()
    assert image.pixelColor(1, 1).alpha() == 0
    assert 120 <= image.pixelColor(w // 2, h - 80).alpha() <= 160
    rect = ctrl.panel.view.visualRect(ctrl.panel.model.index(row_of(ctrl, store.history()[0].id), 0))
    assert image.pixelColor(ctrl.panel.view.viewport().mapTo(ctrl.panel, rect.center())).alpha() == 255


# ---------------------------------------------------------------------------
# 关机清除
# ---------------------------------------------------------------------------
def test_clear_on_shutdown_keeps_favorites(env):
    ctrl, fake, store = env
    for text in ("a", "b", "c"):
        ctrl.record_text(text)
    ctrl.toggle_favorite(store.history()[0])
    ctrl._on_session_ending()  # 默认关闭：什么都不删
    assert store.counts() == (3, 1)
    ctrl._set_clear_on_shutdown(True)
    ctrl._on_session_ending()
    assert store.counts() == (1, 1) and store.history()[0].preview == "c"


def test_reboot_detected_at_startup(qapp, tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "h.db")
    store.add("关机前的历史", now=1)
    favorite = store.add("收藏", now=2)
    store.set_favorite(favorite.id, True)
    store.set_setting("boot_time", "1700000000")
    monkeypatch.setattr(win32, "boot_time", lambda: 1700000000.0 + 30)  # 同一次开机：不清
    cfg = Config(show_on_start=False, clear_on_shutdown=True)
    ctrl = app_module.Controller(qapp, cfg, tmp_path / "c.json", store, tmp_path)
    assert store.counts() == (2, 1)
    ctrl.shutdown()
    ctrl.server.close()
    store = HistoryStore(tmp_path / "h.db")
    monkeypatch.setattr(win32, "boot_time", lambda: 1700090000.0)  # 重启过了：清掉历史，保留收藏
    ctrl = app_module.Controller(qapp, cfg, tmp_path / "c.json", store, tmp_path)
    assert store.counts() == (1, 1)
    assert "清除" in ctrl.panel.status.text()
    ctrl.shutdown()
    ctrl.server.close()


def test_shutdown_is_idempotent_and_helpers(env):
    ctrl, fake, store = env
    ctrl.shutdown()
    ctrl.shutdown()  # aboutToQuit 和 main 的 finally 都会调用
    assert app_module.friendly_app_name("WeChat.exe") == "微信"
    assert app_module.paths_missing(["\\\\server\\share\\a.txt"]) is False  # 网络路径不检查


# ---------------------------------------------------------------------------
# 菜单整理、存储上限、列表只列前 500 条
# ---------------------------------------------------------------------------
def menu_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_menu_is_grouped(env):
    ctrl, fake, store = env
    ctrl.record_text("一条")
    ctrl._sync_menu()
    assert menu_texts(ctrl.menu) == [
        "隐藏面板", "暂停记录",
        "呼出位置（Win+Alt+V）", "背景透明度", "存储",
        "复制成功提示", "开机自动启动",
        "退出 ClipDock",
    ]
    assert menu_texts(ctrl.position_menu) == ["输入光标旁边", "上次的位置"]
    assert menu_texts(ctrl.storage_menu) == [
        "按条数上限：最多 500 条…", "按占用上限：最多 300 MB…", "现在：1 条，约 0 KB（收藏不算）",
        "关机时清除历史（保留收藏）", "清空历史（保留收藏）…", "打开数据文件夹",
    ]
    assert not ctrl.act_usage.isEnabled()
    ctrl.position_menu.actions()[1].trigger()          # 二级菜单里点“上次的位置”
    assert ctrl.cfg.popup_position == "last"
    ctrl._sync_menu()
    assert [a.isChecked() for a in ctrl.position_menu.actions()] == [False, True]


def test_change_limits_asks_before_deleting(env, monkeypatch, tmp_path):
    ctrl, fake, store = env
    for i in range(30):
        store.add(f"第{i:04d}条", now=1000 + i)
    asked = []
    monkeypatch.setattr(ctrl, "_confirm", lambda title, text: asked.append(text) or False)
    assert not ctrl.apply_limits(20, 300)                # 会删 10 条：先问，用户点了取消
    assert store.usage()[0] == 30 and ctrl.cfg.max_items == 500 and "10 条" in asked[0]
    monkeypatch.setattr(ctrl, "_confirm", lambda title, text: True)
    assert ctrl.apply_limits(20, 300)
    assert store.usage()[0] == 20 and "删掉了 10 条" in ctrl.panel.status.text()
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert (saved["max_items"], saved["max_storage_mb"]) == (20, 300)
    assert not ctrl.apply_limits(0, 0)                   # 两个都不限：不行
    assert ctrl.apply_limits(0, 2048)                    # 只按占用：不删东西，不用问
    ctrl._sync_menu()
    assert ctrl.act_limit_items.text() == "按条数上限：不限…"
    assert ctrl.act_limit_size.text() == "按占用上限：最多 2 GB…"


def test_limit_dialog_from_menu(env, monkeypatch):
    ctrl, fake, store = env
    opened = []

    class FakeDialog:
        def __init__(self, title, text, unit, value, default, value_range, step, can_be_unlimited):
            opened.append((title, unit, value, can_be_unlimited))

        def exec(self):
            return 1

        def value(self):
            return 0  # 选了“不限”

    monkeypatch.setattr(app_module, "LimitDialog", FakeDialog)
    ctrl.act_limit_items.trigger()
    assert opened[-1] == ("按条数上限", "条", 500, True) and ctrl.cfg.max_items == 0
    ctrl.act_limit_size.trigger()                        # 条数已经不限了：占用不能再选不限
    assert opened[-1] == ("按占用上限", "MB", 300, False) and ctrl.cfg.max_storage_mb == 300


def test_long_history_lists_only_the_latest(env):
    ctrl, fake, store = env
    for i in range(app_module.LIST_LIMIT + 20):
        store.add(f"记录 {i}", now=1000 + i * 600)        # 间隔 10 分钟，每条一组
    ctrl.reload()
    items = [r for r in rows(ctrl) if isinstance(r, ItemRow)]
    assert len(items) == app_module.LIST_LIMIT and items[0].item.preview == f"记录 {app_module.LIST_LIMIT + 19}"
    assert rows(ctrl)[-1].label == "更早的 20 条没有列出来，搜索可以找到"
    ctrl._set_query("记录")
    assert ctrl.panel.status.text().startswith(f"找到 {app_module.LIST_LIMIT} 多条")
    assert rows(ctrl)[-1].label.startswith("只列出了前")
    ctrl._set_query("记录 7")                              # 关键词更具体：全列出来
    assert not ctrl._truncated
