"""ClipDock 主程序：把存储、Windows 接口和界面串起来。

几条关键流程：
1. 记录：系统发 WM_CLIPBOARDUPDATE → 识别是文字/图片/文件（跳过带隐私标记的）
         → 存进历史 → 在鼠标旁边提示“✓ 已复制”
2. 填入：点击一条 → 当前前台窗口就是你正在输入的程序（面板不抢焦点）
         → 把内容（连同格式）放进剪贴板 → 模拟 Ctrl+V
3. 搜索：点搜索框 → 弹出可输入的小窗口（临时拿到焦点）→ 选中后先把焦点还回去，再填入
4. 收藏：收藏的内容长期保留，可以放进项目；项目点 → 进入，← 返回
5. 全屏：每半秒看一次前台窗口，是全屏程序就把面板藏起来，退出全屏后自动回来
6. 关机清除（默认关）：关机/注销时清空历史，收藏保留
"""
from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QCursor, QDesktopServices, QFont, QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__, clipdata, placement, win32
from .config import Config, data_dir
from .hotkey import format_hotkey, parse_hotkey
from .store import ALL_FAVORITES, HistoryStore, Item, Project
from .ui import (
    CopyToast,
    HeaderRow,
    ItemRow,
    NewProjectRow,
    Panel,
    ProjectRow,
    SearchPopup,
    Theme,
    burst_groups,
    burst_label,
    detect_theme,
    make_app_icon,
)

log = logging.getLogger("clipdock")

HOTKEY_ID = 1       # 显示/隐藏面板
ESC_HOTKEY_ID = 2   # 鼠标停在面板上时的 Esc
FULLSCREEN_CHECK_MS = 500
CLIPBOARD_DEBOUNCE_MS = 40   # 有些程序一次复制会连续改好几次剪贴板，等它改完再读
CLIPBOARD_RETRY_MS = 60      # 剪贴板暂时被别的程序占着时，隔多久再读
CLIPBOARD_RETRIES = 6
PASTE_AFTER_FOCUS_MS = 80    # 切回目标窗口后稍等一下再按 Ctrl+V
CARET_RETRY_MS = 120         # 第一次没问到光标位置时，隔多久再问一次（Chrome 第一次被问时要先准备）
MAX_IMAGE_BYTES = 30_000_000  # 单张图片（PNG）超过这个大小就不记录
# 菜单里的“背景”选项：（显示名, 不透明度%）
OPACITY_LEVELS = [("不透明", 100), ("微透明", 85), ("半透明", 70), ("较透明", 55), ("很透明", 40)]

# exe 名 -> 显示名（没列出来的就显示 exe 名本身）
_APP_NAMES = {
    "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "code": "VS Code",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "onenote": "OneNote",
    "outlook": "Outlook", "olk": "Outlook", "notepad": "记事本", "explorer": "资源管理器",
    "windowsterminal": "终端", "cmd": "命令提示符", "powershell": "PowerShell", "pwsh": "PowerShell",
    "wechat": "微信", "weixin": "微信", "qq": "QQ", "tim": "TIM", "dingtalk": "钉钉",
    "feishu": "飞书", "lark": "飞书", "wps": "WPS", "et": "WPS 表格", "wpp": "WPS 演示",
    "pycharm64": "PyCharm", "idea64": "IntelliJ IDEA", "clion64": "CLion", "webstorm64": "WebStorm",
    "devenv": "Visual Studio", "obsidian": "Obsidian", "notion": "Notion", "typora": "Typora",
    "acrobat": "Acrobat", "acrord32": "Acrobat", "snippingtool": "截图工具", "mspaint": "画图",
}


def friendly_app_name(exe: str) -> str:
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    return _APP_NAMES.get(stem.lower(), stem)


def _copied(summary: str, suffix: str = "") -> str:
    """“已复制”提示文字：已复制「张三」、已复制 图片 1920×1080。"""
    space = "" if summary.startswith("「") else " "
    return f"已复制{space}{summary}{suffix}"


def paths_missing(paths: list[str]) -> bool:
    """文件是否都已经不在了。网络路径不检查，免得断网时卡住。"""
    if not paths:
        return False
    for path in paths[:3]:
        if win32.is_remote_path(path) or os.path.exists(path):
            return False
    return True


# ---------------------------------------------------------------------------
# 开机自启（写 HKCU\...\Run）
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后的 exe
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # 用 pythonw 启动，不弹黑窗口
    interpreter = pythonw if pythonw.exists() else exe
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{interpreter}" "{main_py}"'


def is_autostart_enabled() -> bool:
    if not win32.IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if not win32.IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.warning("设置开机启动失败：%s", exc)
        return False


# ---------------------------------------------------------------------------
# 单实例：第二次启动时，只是让已经在运行的那个显示面板
# ---------------------------------------------------------------------------
def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"{APP_NAME}-{hashlib.md5(user.encode('utf-8')).hexdigest()[:10]}"


def notify_running_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(_server_name())
    if not socket.waitForConnected(300):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------
class Controller(QObject):
    def __init__(self, app: QApplication, cfg: Config, cfg_path: Path, store: HistoryStore,
                 data_path: Path) -> None:
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.store = store
        self.data_path = data_path
        self.theme = detect_theme()
        self._pid = os.getpid()
        self._hwnd = 0
        self._popup_hwnd = 0
        self._tab = "recent"             # "recent" 最近 / "favorites" 收藏
        self._project_id: int | None = None  # 收藏页里进入的项目
        self._query = ""
        self._search_target = 0          # 搜索时，填入的目标窗口
        self._wanted_visible = False     # 用户希望面板显示着
        self._hidden_for_fullscreen = False
        self._fullscreen_override = 0    # 全屏时用户手动呼出面板：对这个窗口不再自动隐藏
        self._last_seq = -1
        self._own_seq = -1
        self._panel_hovered = False      # 鼠标是否停在面板上
        self._esc_registered = False     # 是否临时注册了 Esc 快捷键
        self._session_ending = False     # Windows 正在关机/注销
        self._missing: dict[int, bool] = {}  # 文件条目：文件是否已经不在了（每次显示面板时重新检查）
        self._preferred_size = QSize(340, 580)  # 用户拖出来的面板大小；放到光标旁边时为了放得下可能临时变矮
        self._shut_down = False
        self._self_elevated = win32.is_current_process_elevated()

        self.panel = Panel(self.theme, thumb_loader=lambda item_id: self.store.format_of(item_id, clipdata.THUMB))
        self.panel.hotkey_text = format_hotkey(cfg.hotkey)
        self.popup = SearchPopup(self.theme, self.panel.icon_font, self.panel.has_icon_font)
        self.toast = CopyToast()
        self.icon = make_app_icon(self.theme.accent)
        self.app.setWindowIcon(self.icon)
        self.menu = self._build_menu()
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(f"ClipDock · {format_hotkey(cfg.hotkey)} 呼出面板")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        p = self.panel
        p.itemClicked.connect(self.activate_item)
        p.favoriteClicked.connect(self.toggle_favorite)
        p.itemMenuRequested.connect(self._show_item_menu)
        p.projectClicked.connect(self.enter_project)
        p.projectMenuRequested.connect(self._show_project_menu)
        p.newProjectClicked.connect(lambda: self._new_project())
        p.tabClicked.connect(self.switch_tab)
        p.backClicked.connect(self.go_back)
        p.searchClicked.connect(self.begin_search)
        p.searchCleared.connect(self._clear_query)
        p.pauseClicked.connect(lambda: self._set_paused(not self.cfg.paused))
        p.menuClicked.connect(self._show_main_menu)
        p.hideClicked.connect(self.hide_panel)
        # 这两个信号在 Windows 消息处理函数里发出，排队处理更稳妥
        p.hotkeyPressed.connect(self._on_hotkey, Qt.ConnectionType.QueuedConnection)
        p.clipboardUpdated.connect(self._on_clipboard_changed, Qt.ConnectionType.QueuedConnection)
        p.nativeHandleChanged.connect(self._on_native_changed)
        p.geometryEdited.connect(lambda: self._geometry_timer.start(300))
        p.sizeEdited.connect(self._on_size_edited)
        p.geometryChanged.connect(self._follow_panel)
        p.hoverChanged.connect(self._on_panel_hover)

        self.popup.textEdited.connect(self._on_query_edited)
        self.popup.accepted.connect(self._accept_search)
        self.popup.cancelled.connect(self._cancel_search)
        self.popup.navigate.connect(self._navigate)
        self.popup.forward.connect(self._search_forward)
        self.popup.backward.connect(self._search_backward)
        self.popup.switchTab.connect(lambda: self.switch_tab("favorites" if self._tab == "recent" else "recent"))
        self.popup.dismissed.connect(self._on_popup_dismissed)

        self._clip_timer = self._timer(CLIPBOARD_DEBOUNCE_MS, self._capture_clipboard, single=True)
        self._geometry_timer = self._timer(300, self._save_geometry, single=True)
        self._fullscreen_timer = self._timer(FULLSCREEN_CHECK_MS, self._tick)
        self._clock_timer = self._timer(30_000, self._refresh_times)
        self._fullscreen_timer.start()
        self._clock_timer.start()

        self.server = QLocalServer(self)
        QLocalServer.removeServer(_server_name())
        if self.server.listen(_server_name()):
            self.server.newConnection.connect(self._on_instance_ping)
        else:
            log.warning("单实例服务启动失败：%s", self.server.errorString())

        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self._apply_theme(detect_theme()))
        # Windows 关机/注销时 Qt 会先发 commitDataRequest，再发 aboutToQuit，之后进程随时会被结束
        if hasattr(self.app, "commitDataRequest"):  # Qt 编译时可能没带会话管理
            self.app.commitDataRequest.connect(self._on_session_ending)
        self.app.aboutToQuit.connect(self.shutdown)

        self._attach_native()
        win32.make_no_activate(int(self.toast.winId()))
        self._apply_theme(self.theme)
        self.panel.set_background_opacity(cfg.background_opacity)
        self._restore_geometry()
        self._preferred_size = self.panel.size()
        self.panel.set_paused(cfg.paused)
        self.panel.set_tab(self._tab)
        self._check_reboot()
        self.reload()
        if cfg.show_on_start:
            self.show_panel()
        log.info("ClipDock %s 已启动，快捷键 %s", __version__, format_hotkey(cfg.hotkey))

    def _timer(self, ms: int, slot, single: bool = False) -> QTimer:
        timer = QTimer(self)
        timer.setInterval(ms)
        timer.setSingleShot(single)
        timer.timeout.connect(slot)
        return timer

    # ---- 原生窗口：样式、快捷键、剪贴板监听都挂在面板窗口上 ----
    def _attach_native(self) -> None:
        hwnd = int(self.panel.winId())
        self._popup_hwnd = int(self.popup.winId())
        if hwnd == self._hwnd:
            return
        if self._hwnd:
            win32.unregister_hotkey(self._hwnd, HOTKEY_ID)
            win32.unregister_hotkey(self._hwnd, ESC_HOTKEY_ID)
            win32.remove_clipboard_listener(self._hwnd)
            self._esc_registered = False
        self._hwnd = hwnd
        win32.make_no_activate(hwnd)
        if win32.IS_WINDOWS and not win32.add_clipboard_listener(hwnd):
            self._notify("无法监听剪贴板", "复制的内容暂时不会被记录，可以重启 ClipDock 再试。")
        self._register_hotkey()

    def _on_native_changed(self) -> None:
        self._attach_native()
        self._apply_theme(self.theme)

    def _register_hotkey(self) -> None:
        try:
            modifiers, vk = parse_hotkey(self.cfg.hotkey)
        except ValueError as exc:
            self._notify("快捷键配置有误", f"{exc}\n可以在 config.json 里修改 hotkey。")
            return
        ok = win32.register_hotkey(self._hwnd, HOTKEY_ID, modifiers, vk)
        if win32.IS_WINDOWS and not ok:
            self._notify(
                "快捷键被占用",
                f"{format_hotkey(self.cfg.hotkey)} 已被别的程序占用。"
                "可以点托盘图标显示面板，或者在 config.json 里换一个快捷键。",
            )

    def _apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.panel.apply_theme(theme)
        self.popup.apply_theme(theme)

    def _notify(self, title: str, text: str) -> None:
        log.warning("%s：%s", title, text)
        if self.tray.isVisible():
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Warning, 8000)
        self.panel.show_toast(title, 6000)

    # ---- 显示与隐藏 ----
    def show_panel(self, from_user: bool = False) -> None:
        self._wanted_visible = True
        if from_user:
            self._hidden_for_fullscreen = False
            self._fullscreen_override = self._current_fullscreen()
        if self._missing:  # 隐藏期间文件可能被移走了，重新检查
            self._missing.clear()
            self.reload()
        self._show_native()

    def _show_native(self) -> None:
        previous = win32.get_foreground_window()
        self.panel.show()
        win32.make_no_activate(self._hwnd)  # 保险：确保样式还在
        win32.set_topmost(self._hwnd)
        self._panel_hovered = self._cursor_over_panel()
        self._sync_esc_hotkey()
        QTimer.singleShot(0, lambda: self._undo_focus_steal(previous))

    def _undo_focus_steal(self, previous: int) -> None:
        # 万一显示时还是拿走了焦点，就还给原来的窗口
        if previous and previous != self._hwnd and win32.get_foreground_window() == self._hwnd:
            log.info("面板显示时意外拿到了焦点，已还给原窗口")
            win32.force_foreground(previous)

    def hide_panel(self) -> None:
        self._wanted_visible = False
        self._hidden_for_fullscreen = False
        self._close_search(restore_focus=True)  # 正在搜索时按了隐藏：把焦点还给原来的程序
        self.panel.hide()
        self._sync_esc_hotkey()

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.hide_panel()
        else:
            self.show_panel(from_user=True)

    def _on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == HOTKEY_ID:
            win32.suppress_alt_win_menu()
            self.summon()
        elif hotkey_id == ESC_HOTKEY_ID:
            self._on_escape()

    # ---- 快捷键呼出：只负责显示，不再用来隐藏（隐藏用 Esc 或面板上的 —） ----
    def summon(self) -> None:
        if self.cfg.popup_position == "caret" and not self._is_own(win32.get_foreground_window()):
            self._summon_to_caret(retry=True)
            return
        if not self.panel.isVisible():  # “上次的位置”：原地出现，大小恢复成你拖出来的大小
            self._restore_preferred_size()
        self._finish_summon()

    def _summon_to_caret(self, retry: bool) -> None:
        caret = self._caret_logical()
        if caret is None and retry:
            # Chrome、Edge 等程序第一次被问光标位置时要先准备一下，稍等再问一次
            QTimer.singleShot(CARET_RETRY_MS, lambda: self._summon_to_caret(retry=False))
            return
        note = ""
        if caret is None:  # 少数程序不报告光标位置：放在鼠标旁边
            pos = QCursor.pos()
            caret = QRect(pos.x(), pos.y() - 8, 2, 16)
            note = "没找到输入光标，放在了鼠标旁边"
        self._move_near_caret(caret)
        self._finish_summon(note)

    def _finish_summon(self, note: str = "") -> None:
        if self.panel.isVisible():
            self._wanted_visible = True
            win32.set_topmost(self._hwnd)  # 万一被别的置顶窗口盖住了
            self._on_panel_hover(self._cursor_over_panel())
        else:
            self.show_panel(from_user=True)
        self.panel.pulse()  # 边框亮一下，一眼就能看到面板在哪
        if note:
            self.panel.show_toast(note, 3000)

    @staticmethod
    def _screens() -> list[tuple[QRect, float]]:
        return [(s.geometry(), s.devicePixelRatio()) for s in QGuiApplication.screens()]

    def _caret_logical(self) -> QRect | None:
        rect = win32.caret_rect()
        if rect is None:
            return None
        x, y, width, height = rect
        return placement.native_rect_to_logical(x, y, x + width, y + height, self._screens())

    def _foreground_window(self) -> tuple[QRect | None, bool]:
        """当前窗口的位置（逻辑坐标），以及能不能把面板贴在它外侧（没最大化才行）。"""
        foreground = win32.get_foreground_window()
        if not foreground or self._is_own(foreground):
            return None, False
        bounds = win32.window_rect(foreground)
        if bounds is None:
            return None, False
        return placement.native_rect_to_logical(*bounds, self._screens()), not win32.is_maximized(foreground)

    def _move_near_caret(self, caret: QRect) -> None:
        screen = QGuiApplication.screenAt(caret.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        window, outside_ok = self._foreground_window()
        if self.panel.isVisible() and placement.still_good(self.panel.frameGeometry(), caret, area, window):
            return  # 已经在光标那一行上方或下方不远处了，不挪
        list_top = self.panel.stack.mapTo(self.panel, QPoint(0, 0)).y() + 12
        min_height = max(placement.MIN_HEIGHT, self.panel.minimumHeight())
        rect, rule = placement.place_near_caret(caret, self._preferred_size, area, window, outside_ok,
                                                list_top, min_height)
        log.info("面板放在%s", rule)
        self.panel.setGeometry(rect)

    def _on_size_edited(self) -> None:
        self._preferred_size = self.panel.size()

    def _restore_preferred_size(self) -> None:
        if self.panel.size() == self._preferred_size:
            return
        screen = QGuiApplication.screenAt(self.panel.geometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            rect = QRect(self.panel.pos(), self._preferred_size)
            self.panel.setGeometry(self._fit_into(rect, screen.availableGeometry()))

    # ---- Esc 隐藏 ----
    # 面板不拿键盘焦点，没法直接收到 Esc；把 Esc 注册成全局快捷键又会抢走所有程序的 Esc。
    # 折中：只在鼠标停在面板上时临时注册 Esc，鼠标一离开就注销。
    def _cursor_over_panel(self) -> bool:
        return self.panel.isVisible() and self.panel.frameGeometry().contains(QCursor.pos())

    def _on_panel_hover(self, hovered: bool) -> None:
        self._panel_hovered = hovered
        self._sync_esc_hotkey()

    def _sync_esc_hotkey(self) -> None:
        # 搜索框打开时它自己有焦点，Esc 留给它“取消搜索”
        want = self._panel_hovered and self.panel.isVisible() and not self.popup.isVisible()
        if want and not self._esc_registered:
            self._esc_registered = win32.register_hotkey(self._hwnd, ESC_HOTKEY_ID, 0, win32.VK_ESCAPE)
        elif not want and self._esc_registered:
            win32.unregister_hotkey(self._hwnd, ESC_HOTKEY_ID)
            self._esc_registered = False

    def _on_escape(self) -> None:
        if self._cursor_over_panel() and not self.popup.isVisible():
            self.hide_panel()
            return
        # 鼠标其实已经离开面板（状态没来得及更新）：先注销，再把这次 Esc 还给前台程序
        self._panel_hovered = False
        self._sync_esc_hotkey()
        win32.send_escape()

    def _tick(self) -> None:
        hovered = self._cursor_over_panel()
        if hovered != self._panel_hovered:  # 兜底：万一漏掉了鼠标离开事件
            self._on_panel_hover(hovered)
        self._check_fullscreen()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_panel()

    def _on_instance_ping(self) -> None:
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.disconnected.connect(connection.deleteLater)
        self.show_panel(from_user=True)

    # ---- 全屏让位 ----
    def _current_fullscreen(self) -> int:
        if not self.cfg.hide_when_fullscreen:
            return 0
        return win32.fullscreen_foreground(self._pid, win32.monitor_of(self._hwnd))

    def _check_fullscreen(self) -> None:
        if not self._wanted_visible or not self.cfg.hide_when_fullscreen:
            return
        fullscreen = self._current_fullscreen()
        if fullscreen and fullscreen != self._fullscreen_override:
            if self.panel.isVisible():
                self._close_search(restore_focus=False)
                self.panel.hide()
                self._hidden_for_fullscreen = True
                self._sync_esc_hotkey()
            return
        if not fullscreen:
            self._fullscreen_override = 0
        if self._hidden_for_fullscreen and not self.panel.isVisible():
            self._hidden_for_fullscreen = False
            self._show_native()

    # ---- 记录复制 + 复制成功提示 ----
    def _on_clipboard_changed(self) -> None:
        self._clip_timer.start()

    def _capture_clipboard(self, attempt: int = 0) -> None:
        if self._shut_down:
            return
        seq = win32.clipboard_sequence()
        if seq == self._last_seq:
            return
        if seq == self._own_seq:  # 是我们自己填入时放进去的
            self._last_seq = seq
            return
        owner_pid = win32.clipboard_owner_pid()
        if owner_pid == self._pid:
            self._last_seq = seq
            return
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.formats():
            if win32.clipboard_format_count() > 0 and attempt < CLIPBOARD_RETRIES:
                # 剪贴板里有东西但读不到：多半是别的程序正占着剪贴板，稍后再读
                QTimer.singleShot(CLIPBOARD_RETRY_MS, lambda: self._capture_clipboard(attempt + 1))
            else:
                self._last_seq = seq  # 剪贴板被清空了，不是复制
            return
        self._last_seq = seq
        near = QCursor.pos()
        if win32.clipboard_is_private():
            log.info("跳过一条带“不要记录”标记的内容（通常来自密码管理器）")
            self._feedback("已复制（隐私内容，不记录）", near)
            return
        cap = clipdata.capture(mime, self.cfg.max_text_chars, MAX_IMAGE_BYTES)
        if cap is None:  # 复制成功了，但这种内容（比如某个软件的私有格式）没法记录
            self._feedback("已复制", near)
            return
        if cap.too_large:
            self._feedback(_copied(cap.summary, "（太大，没有记录）"), near)
            return
        if self.cfg.paused:
            self._feedback(_copied(cap.summary, "（暂停记录中）"), near)
            return
        self.store.add(cap.text, self._source_name(owner_pid), kind=cap.kind, formats=cap.formats,
                       meta=cap.meta, digest=cap.digest)
        self.store.trim(self.cfg.max_items, self.cfg.max_image_mb * 1_000_000)
        self.reload()
        self._feedback(_copied(cap.summary), near)

    def _feedback(self, text: str, near: QPoint) -> None:
        if not self.cfg.copy_feedback:
            return
        if self.cfg.hide_when_fullscreen:
            fullscreen = win32.fullscreen_foreground(self._pid)
            if fullscreen and fullscreen != self._fullscreen_override:
                return  # 全屏看视频、玩游戏时不打扰
        self.toast.show_message(text, near)

    def record_text(self, text: str, source: str = "") -> None:
        """直接记录一段文字（测试和调试用）。"""
        if not text.strip():
            return
        self.store.add(text, source)
        self.store.trim(self.cfg.max_items)
        self.reload()

    @staticmethod
    def _source_name(owner_pid: int) -> str:
        exe = win32.process_name(owner_pid) if owner_pid else ""
        if not exe:
            exe = win32.process_name(win32.window_pid(win32.get_foreground_window()))
        return friendly_app_name(exe)

    # ---- 列表内容 ----
    def reload(self) -> None:
        query = self._query.strip()
        total, favorites = self.store.counts()
        self.panel.header.set_favorite_count(favorites)
        project = self.store.project(self._project_id) if self._project_id is not None else None
        if self._project_id is not None and project is None:  # 项目刚被删掉
            self._project_id = None
        if self._tab == "recent":
            rows, empty, style = self._recent_rows(query)
        elif project is None:
            rows, empty, style = self._favorite_rows(query)
        else:
            rows = [self._item_row(item) for item in self.store.favorites(project.id, query)]
            empty = (f"项目里没有包含“{query}”的收藏" if query
                     else "这个项目还是空的\n在条目上右键 → 移到项目")
            style = "relative"
        self.panel.set_project(project)
        self.panel.set_rows(rows, empty, style)
        shown = sum(isinstance(row, ItemRow) for row in rows)
        if query:
            status = f"找到 {shown} 条"
        elif self._tab == "recent":
            status = f"共 {total} 条" + (f"，收藏 {favorites} 条" if favorites else "")
            if total:
                status += " · 点击填入，右键更多"
        elif favorites:
            status = f"收藏 {favorites} 条 · 右键条目可以移到项目"
        else:
            status = "收藏的内容会一直保留，不会被自动清理"
        self.panel.set_status(status)

    def _item_row(self, item: Item, project_name: str = "") -> ItemRow:
        missing = False
        if item.kind == "files":
            if item.id not in self._missing:
                self._missing[item.id] = paths_missing(item.paths)
            missing = self._missing[item.id]
        return ItemRow(item, missing, project_name)

    def _recent_rows(self, query: str):
        rows = []
        now = time.time()
        for group in burst_groups(self.store.history(query), self.cfg.group_gap_minutes * 60):
            rows.append(HeaderRow(burst_label(group, now)))
            rows.extend(self._item_row(item) for item in group)
        if query:
            empty = f"没有包含“{query}”的记录"
        else:
            hint = f"\n\n{self.panel.hotkey_text} 随时呼出面板" if self.panel.hotkey_text else ""
            empty = "还没有记录\n复制文字、图片或文件后，会出现在这里" + hint
        return rows, empty, "clock"

    def _favorite_rows(self, query: str):
        if query:  # 在全部收藏里搜，每条注明属于哪个项目
            names = {p.id: p.name for p in self.store.projects()}
            items = self.store.favorites(ALL_FAVORITES, query)
            rows = [self._item_row(item, names.get(item.project_id, "")) for item in items]
            return rows, f"收藏里没有包含“{query}”的内容", "relative"
        projects = self.store.projects()
        loose = self.store.favorites(None)
        if not projects and not loose:
            return [], ("还没有收藏\n点条目右上角的 ☆，常用的内容就不用再找了\n"
                        "收藏多了可以建项目分类"), "relative"
        rows = [ProjectRow(p) for p in projects] + [NewProjectRow()]
        if loose:
            rows.append(HeaderRow(f"{'未分类' if projects else '收藏'} · {len(loose)} 条"))
            rows.extend(self._item_row(item) for item in loose)
        return rows, "", "relative"

    def _refresh_times(self) -> None:
        if self.panel.isVisible():
            self.panel.view.viewport().update()

    # ---- 页签与项目 ----
    def switch_tab(self, tab: str) -> None:
        self._tab = tab
        self._project_id = None
        self.panel.set_tab(tab)
        self._reset_query()
        self.reload()
        self._highlight_first()

    def enter_project(self, project: Project) -> None:
        self._tab = "favorites"
        self._project_id = project.id
        self.panel.set_tab("favorites")
        self._reset_query()
        self.reload()
        self._highlight_first()

    def go_back(self) -> None:
        if self._project_id is None:
            return
        came_from = ("project", self._project_id)
        self._project_id = None
        self._reset_query()
        self.reload()
        if self.popup.isVisible():
            self.panel.set_highlight(came_from)  # 返回后选中刚才那个项目

    def toggle_favorite(self, item: Item) -> None:
        self.store.set_favorite(item.id, not item.favorite)
        self.reload()
        self.panel.show_toast("已取消收藏" if item.favorite else "已收藏，可以在「收藏」页找到")

    def move_to_project(self, item: Item, project_id: int | None, name: str = "") -> None:
        self.store.move_to_project(item.id, project_id)
        self.reload()
        self.panel.show_toast(f"已放进「{name}」" if project_id is not None else "已移到未分类")

    def _new_project(self, item: Item | None = None) -> None:
        name = self._ask_text("新建项目", "项目名称（比如：课程作业、求职材料）：")
        if not name:
            return
        project = self.store.create_project(name)
        if item is not None:
            self.store.move_to_project(item.id, project.id)
            self.panel.show_toast(f"已新建「{project.name}」并放进去")
        else:
            self.panel.show_toast(f"已新建项目「{project.name}」")
        self.reload()

    def _rename_project(self, project: Project) -> None:
        name = self._ask_text("重命名项目", "新的名称：", project.name)
        if name:
            self.store.rename_project(project.id, name)
            self.reload()

    def _delete_project(self, project: Project) -> None:
        if not self._confirm("删除项目", f"删除项目「{project.name}」？\n"
                                       f"里面的 {project.count} 条收藏不会删除，会移到“未分类”。"):
            return
        self.store.delete_project(project.id)
        if self._project_id == project.id:
            self._project_id = None
        self.reload()
        self.panel.show_toast(f"已删除项目「{project.name}」")

    # ---- 填入 ----
    def _is_own(self, hwnd: int) -> bool:
        return bool(hwnd) and (hwnd in (self._hwnd, self._popup_hwnd) or win32.window_pid(hwnd) == self._pid)

    def _focus_target(self) -> int:
        """现在“正在输入的那个窗口”：搜索时是打开搜索前的窗口，否则是前台窗口。"""
        return self._search_target if self.popup.isVisible() else win32.get_foreground_window()

    def _put_on_clipboard(self, mime: QMimeData) -> bool:
        QGuiApplication.clipboard().setMimeData(mime)
        if not win32.flush_clipboard():
            log.info("剪贴板内容没能立即生成，改为粘贴时再生成")
        self._own_seq = win32.clipboard_sequence()
        if win32.IS_WINDOWS and win32.clipboard_owner_pid() != self._pid:
            log.warning("放进剪贴板失败，可能正被别的程序占用")
            return False
        return True

    def activate_item(self, item: Item, copy_only: bool = False, target: int | None = None) -> None:
        searching = self.popup.isVisible()
        if target is None:
            # 面板不抢焦点，所以此刻的前台窗口就是你正在输入的程序
            target = self._search_target if searching else win32.get_foreground_window()
        if self._is_own(target):
            target = 0
        text = self.store.text_of(item.id)
        if text is None:
            self.panel.show_toast("这条记录已经不存在了")
            self.reload()
            return
        mime, note = clipdata.build_mime(item.kind, text, self.store.formats_of(item.id))
        if mime is None:
            self.panel.show_toast(note, 4000)
            return
        if not self._put_on_clipboard(mime):
            self.panel.show_toast("剪贴板正被别的程序占用，请再点一次")
            return
        self.store.mark_used(item.id)
        self.panel.flash(("item", item.id))
        if searching:
            self._close_search(restore_focus=bool(target))
        if copy_only or not target:
            self.panel.show_toast(note or "已复制到剪贴板")
            return
        if not self._self_elevated and win32.is_window_elevated(target):
            self.panel.show_toast("目标程序以管理员身份运行，没法自动填入；已复制，请按 Ctrl+V", 5000)
            return
        # 刚把焦点还给目标窗口时，稍等一下让它恢复光标位置，再按 Ctrl+V
        delay = PASTE_AFTER_FOCUS_MS if searching else 0
        if win32.get_foreground_window() != target:
            win32.force_foreground(target)
            delay = PASTE_AFTER_FOCUS_MS
        QTimer.singleShot(delay, lambda: self._send_paste(target, note))

    def _send_paste(self, target: int, note: str = "") -> None:
        if win32.get_foreground_window() != target:
            self.panel.show_toast("焦点已经不在原来的窗口，内容已复制，请手动 Ctrl+V", 4000)
            return
        if win32.send_ctrl_v():
            self.panel.show_toast(note or "已填入", 4000 if note else 2600)
        else:
            self.panel.show_toast("模拟按键失败，内容已复制，请手动 Ctrl+V", 4000)

    def _copy_paths(self, item: Item) -> None:
        text = self.store.text_of(item.id) or ""
        if self._put_on_clipboard(clipdata.text_mime(text.replace("\n", "\r\n"))):
            self.panel.show_toast("已复制文件路径")

    # ---- 搜索 ----
    def begin_search(self) -> None:
        if self.popup.isVisible():
            return
        foreground = win32.get_foreground_window()
        self._search_target = 0 if self._is_own(foreground) else foreground
        self.popup.open_at(self.panel.search_rect_global(), self._query)
        self._sync_esc_hotkey()
        self._highlight_first()

    def _on_query_edited(self, text: str) -> None:
        self._query = text
        self.panel.set_query(text)
        self.reload()
        self._highlight_first()

    def _highlight_first(self) -> None:
        selectable = self.panel.model.selectable()
        if selectable and self.popup.isVisible():
            self.panel.set_highlight(self.panel.model.row(selectable[0]).key)
        else:
            self.panel.set_highlight(None)

    def _highlighted_row(self):
        model = self.panel.model
        index = model.index_of(model.highlight_key)
        if index < 0:
            selectable = model.selectable()
            index = selectable[0] if selectable else -1
        return model.row(index)

    def _navigate(self, delta: int) -> None:
        model = self.panel.model
        selectable = model.selectable()
        if not selectable:
            return
        current = model.index_of(model.highlight_key)
        position = selectable.index(current) if current in selectable else -1
        position = 0 if position < 0 else max(0, min(len(selectable) - 1, position + delta))
        self.panel.set_highlight(model.row(selectable[position]).key)

    def _accept_search(self) -> None:
        row = self._highlighted_row()
        if isinstance(row, ItemRow):
            self.activate_item(row.item)
        elif isinstance(row, ProjectRow):
            self.enter_project(row.project)
        elif isinstance(row, NewProjectRow):
            self._new_project()
        else:
            self.panel.show_toast("没有匹配的记录")

    def _search_forward(self) -> None:
        row = self._highlighted_row()
        if isinstance(row, ProjectRow):
            self.enter_project(row.project)

    def _search_backward(self) -> None:
        if self._project_id is not None:
            self.go_back()

    def _cancel_search(self) -> None:
        self._close_search(restore_focus=True)
        if self._query:
            self._set_query("")

    def _clear_query(self) -> None:
        self._set_query("")
        if self.popup.isVisible():
            self.popup.edit.clear()

    def _set_query(self, text: str) -> None:
        self._query = text
        self.panel.set_query(text)
        self.reload()

    def _reset_query(self) -> None:
        self._query = ""
        self.panel.set_query("")
        if self.popup.isVisible():
            self.popup.edit.clear()

    def _close_search(self, restore_focus: bool) -> None:
        target = self._search_target
        self._search_target = 0
        self.panel.set_highlight(None)
        if not self.popup.isVisible():
            return
        self.popup.suppress_dismiss = True
        try:
            # 先把焦点还给目标窗口，再收起搜索框（反过来的话系统可能把焦点交给别的窗口）
            if restore_focus and target:
                win32.force_foreground(target)
            self.popup.hide()
        finally:
            self.popup.suppress_dismiss = False
        self._sync_esc_hotkey()

    def _follow_panel(self) -> None:
        # 搜索时拖动或缩放面板：搜索框跟着走
        if self.popup.isVisible():
            self.popup.setGeometry(self.panel.search_rect_global())

    def _on_popup_dismissed(self) -> None:
        # 搜索时用户点了别的程序：收起搜索框，保留关键词
        if self.popup.isVisible():
            self._close_search(restore_focus=False)

    # ---- 菜单与对话框 ----
    def _build_menu(self) -> QMenu:
        menu = QMenu()
        self.act_toggle = QAction("显示面板", menu)
        self.act_toggle.triggered.connect(self.toggle_panel)
        # 按快捷键时面板出现在哪：两个选项二选一
        self.act_position_title = QAction("按快捷键时：", menu)
        self.act_position_title.setEnabled(False)
        self.act_position_caret = QAction("出现在输入光标旁边", menu)
        self.act_position_last = QAction("出现在上次的位置", menu)
        position_group = QActionGroup(menu)
        position_group.setExclusive(True)
        for action, mode in ((self.act_position_caret, "caret"), (self.act_position_last, "last")):
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=mode: self._set_popup_position(value))
            position_group.addAction(action)
        self.act_pause = QAction("暂停记录", menu)
        self.act_pause.setCheckable(True)
        self.act_pause.triggered.connect(self._set_paused)
        self.act_feedback = QAction("复制成功提示", menu)
        self.act_feedback.setCheckable(True)
        self.act_feedback.triggered.connect(self._set_copy_feedback)
        self.act_new_project = QAction("新建项目…", menu)
        self.act_new_project.triggered.connect(lambda: self._new_project())
        self.act_clear = QAction("清空历史（保留收藏）…", menu)
        self.act_clear.triggered.connect(self._clear_history)
        self.act_clear_on_shutdown = QAction("关机时清除历史（保留收藏）", menu)
        self.act_clear_on_shutdown.setCheckable(True)
        self.act_clear_on_shutdown.triggered.connect(self._set_clear_on_shutdown)
        self.opacity_menu = QMenu("背景", menu)
        group = QActionGroup(self.opacity_menu)
        for label, percent in OPACITY_LEVELS:
            action = QAction(label, self.opacity_menu)
            action.setCheckable(True)
            action.setData(percent)
            action.triggered.connect(lambda _checked=False, value=percent: self._set_opacity(value))
            group.addAction(action)
            self.opacity_menu.addAction(action)
        self.act_autostart = QAction("开机自动启动", menu)
        self.act_autostart.setCheckable(True)
        self.act_autostart.triggered.connect(self._set_autostart)
        self.act_folder = QAction("打开数据文件夹", menu)
        self.act_folder.triggered.connect(self._open_data_folder)
        self.act_quit = QAction("退出 ClipDock", menu)
        self.act_quit.triggered.connect(self.app.quit)
        for entry in (
            self.act_toggle, None,
            self.act_position_title, self.act_position_caret, self.act_position_last, None,
            self.act_pause, self.act_feedback, None,
            self.act_new_project, self.act_clear, self.act_clear_on_shutdown, None,
            self.opacity_menu, self.act_autostart, self.act_folder, None,
            self.act_quit,
        ):
            if entry is None:
                menu.addSeparator()
            elif isinstance(entry, QMenu):
                menu.addMenu(entry)
            else:
                menu.addAction(entry)
        menu.aboutToShow.connect(self._sync_menu)
        return menu

    def _sync_menu(self) -> None:
        hotkey = format_hotkey(self.cfg.hotkey)
        # 快捷键只负责呼出，所以“隐藏面板”后面不写快捷键
        self.act_toggle.setText("隐藏面板" if self.panel.isVisible() else f"显示面板\t{hotkey}")
        self.act_position_title.setText(f"按 {hotkey} 时：")
        self.act_position_caret.setChecked(self.cfg.popup_position == "caret")
        self.act_position_last.setChecked(self.cfg.popup_position != "caret")
        self.act_pause.setChecked(self.cfg.paused)
        self.act_feedback.setChecked(self.cfg.copy_feedback)
        self.act_clear_on_shutdown.setChecked(self.cfg.clear_on_shutdown)
        self.act_autostart.setEnabled(win32.IS_WINDOWS)
        self.act_autostart.setChecked(is_autostart_enabled())
        for action in self.opacity_menu.actions():
            action.setChecked(action.data() == self.cfg.background_opacity)

    def _show_main_menu(self, pos: QPoint) -> None:
        target = self._focus_target()
        self.menu.exec(pos)
        self._restore_focus_after_menu(target)

    def _show_item_menu(self, item: Item, pos: QPoint) -> None:
        target = self._focus_target()  # 弹菜单前记下目标窗口
        menu = QMenu()
        menu.addAction("填入").triggered.connect(lambda: self.activate_item(item, target=target))
        menu.addAction("仅复制（不填入）").triggered.connect(
            lambda: self.activate_item(item, copy_only=True, target=target))
        menu.addSeparator()
        menu.addAction("取消收藏" if item.favorite else "收藏").triggered.connect(
            lambda: self.toggle_favorite(item))
        move = menu.addMenu("移到项目")
        for project in self.store.projects():
            action = move.addAction(project.name)
            action.setCheckable(True)
            action.setChecked(item.favorite and item.project_id == project.id)
            action.triggered.connect(lambda _checked=False, p=project: self.move_to_project(item, p.id, p.name))
        if item.favorite and item.project_id is not None:
            move.addAction("移出项目（放回未分类）").triggered.connect(
                lambda: self.move_to_project(item, None))
        if move.actions():
            move.addSeparator()
        move.addAction("新建项目…").triggered.connect(lambda: self._new_project(item))
        if item.kind == "files":
            menu.addAction("复制文件路径").triggered.connect(lambda: self._copy_paths(item))
        menu.addSeparator()
        menu.addAction("删除").triggered.connect(lambda: self._delete_item(item))
        menu.exec(pos)
        self._restore_focus_after_menu(target)

    def _show_project_menu(self, project: Project, pos: QPoint) -> None:
        target = self._focus_target()
        menu = QMenu()
        menu.addAction("打开").triggered.connect(lambda: self.enter_project(project))
        menu.addAction("重命名…").triggered.connect(lambda: self._rename_project(project))
        menu.addSeparator()
        menu.addAction("删除项目…").triggered.connect(lambda: self._delete_project(project))
        menu.exec(pos)
        self._restore_focus_after_menu(target)

    def _restore_focus_after_menu(self, target: int) -> None:
        # 菜单或对话框可能把焦点拿到了本程序，用完还给原来的窗口
        if target and not self._is_own(target) and self._is_own(win32.get_foreground_window()):
            win32.force_foreground(target)

    def _ask_text(self, title: str, label: str, text: str = "") -> str | None:
        target = self._focus_target()
        dialog = QInputDialog()
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)  # 别被置顶的面板挡住
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(text)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        accepted = bool(dialog.exec())
        value = dialog.textValue().strip()
        self._restore_focus_after_menu(target)
        return value if accepted and value else None

    def _confirm(self, title: str, text: str) -> bool:
        target = self._focus_target()
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.button(QMessageBox.StandardButton.Yes).setText("确定")
        box.button(QMessageBox.StandardButton.No).setText("取消")
        box.exec()
        confirmed = box.clickedButton() is box.button(QMessageBox.StandardButton.Yes)
        self._restore_focus_after_menu(target)
        return confirmed

    def _delete_item(self, item: Item) -> None:
        if item.favorite and not self._confirm("删除收藏", "这条是收藏的内容，确定删除吗？"):
            return
        self.store.delete(item.id)
        self.reload()
        self.panel.show_toast("已删除")

    def _clear_history(self) -> None:
        total, favorites = self.store.counts()
        if total == favorites:
            self.panel.show_toast("没有可以清空的历史")
            return
        if self._confirm("清空历史", f"确定清空 {total - favorites} 条历史吗？收藏的内容会保留。"):
            removed = self.store.clear(keep_favorites=True)
            self.reload()
            self.panel.show_toast(f"已清空 {removed} 条历史")

    def _set_popup_position(self, mode: str) -> None:
        self.cfg.popup_position = mode
        self._save_config()
        hotkey = format_hotkey(self.cfg.hotkey)
        where = "输入光标旁边" if mode == "caret" else "上次的位置"
        self.panel.show_toast(f"按 {hotkey} 时，面板会出现在{where}", 4000)

    def _set_paused(self, paused: bool) -> None:
        self.cfg.paused = bool(paused)
        self._save_config()
        self.panel.set_paused(self.cfg.paused)
        self.panel.show_toast("已暂停记录，新复制的内容不会保存" if paused else "已恢复记录")

    def _set_copy_feedback(self, enabled: bool) -> None:
        self.cfg.copy_feedback = bool(enabled)
        self._save_config()
        self.panel.show_toast("复制成功时会在鼠标旁边提示" if enabled else "已关闭复制成功提示")

    def _set_clear_on_shutdown(self, enabled: bool) -> None:
        self.cfg.clear_on_shutdown = bool(enabled)
        self._save_config()
        self.panel.show_toast("关机或重启时会清空历史，收藏的内容保留" if enabled else "已关闭关机清除", 4000)

    def _set_opacity(self, percent: int) -> None:
        self.cfg.background_opacity = percent
        self._save_config()
        self.panel.set_background_opacity(percent)

    def _set_autostart(self, enabled: bool) -> None:
        if set_autostart(enabled):
            self.panel.show_toast("已设置开机自动启动" if enabled else "已取消开机自动启动")
        else:
            self.panel.show_toast("设置开机启动失败，详情见日志")

    def _open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_path)))

    # ---- 关机清除 ----
    def _check_reboot(self) -> None:
        """开机后第一次运行时，如果开启了“关机清除”而上次没来得及清（比如直接断电），这里补上。"""
        boot = win32.boot_time()
        last = self.store.get_setting("boot_time")
        self.store.set_setting("boot_time", f"{boot:.0f}")
        if not self.cfg.clear_on_shutdown or last is None:
            return
        try:
            rebooted = abs(boot - float(last)) > 120
        except ValueError:
            return
        if rebooted:
            removed = self.store.clear(keep_favorites=True)
            if removed:
                log.info("开机后按设置清除了 %s 条历史", removed)
                self.panel.show_toast(f"已按设置清除关机前的 {removed} 条历史（收藏保留）", 5000)

    def _on_session_ending(self, *_args) -> None:
        """Windows 正在关机或注销。之后进程随时可能被结束，要做的事现在就做。"""
        self._session_ending = True
        log.info("Windows 正在关机或注销")
        if self.cfg.clear_on_shutdown:
            removed = self.store.clear(keep_favorites=True)
            log.info("关机清除：删除了 %s 条历史", removed)
        self._save_geometry()

    # ---- 位置与配置 ----
    def _restore_geometry(self) -> None:
        geo = self.cfg.geometry
        if geo:
            x, y, w, h = geo
            rect = QRect(x, y, max(w, self.panel.minimumWidth()), max(h, self.panel.minimumHeight()))
            title_strip = QRect(x + 20, y, max(40, w - 40), 24)
            for screen in QGuiApplication.screens():
                if screen.availableGeometry().intersects(title_strip):
                    self.panel.setGeometry(self._fit_into(rect, screen.availableGeometry()))
                    return
        # 第一次运行，或者原来的显示器已经拔掉了：放到主屏幕右侧
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        width, height = 340, max(self.panel.minimumHeight(), min(580, avail.height() - 120))
        self.panel.setGeometry(avail.right() - width - 24, avail.top() + 60, width, height)

    @staticmethod
    def _fit_into(rect: QRect, area: QRect) -> QRect:
        """把面板整个挪进屏幕的可用区域（比屏幕还大就缩小），不让一部分跑到屏幕外面。"""
        width, height = min(rect.width(), area.width()), min(rect.height(), area.height())
        x = max(area.x(), min(rect.x(), area.x() + area.width() - width))
        y = max(area.y(), min(rect.y(), area.y() + area.height() - height))
        return QRect(x, y, width, height)

    def _save_geometry(self) -> None:
        # 记住位置和你拖出来的大小（放到光标旁边时临时变矮的不算）
        g = self.panel.geometry()
        self.cfg.geometry = [g.x(), g.y(), self._preferred_size.width(), self._preferred_size.height()]
        self._save_config()

    def _save_config(self) -> None:
        try:
            self.cfg.save(self.cfg_path)
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)

    def shutdown(self) -> None:
        """退出时收尾。正常退出和关机都会走到这里，可能被调用两次，所以要能重复调用。"""
        if self._shut_down:
            return
        self._shut_down = True
        for timer in (self._fullscreen_timer, self._clock_timer, self._clip_timer, self._geometry_timer):
            timer.stop()
        self._save_geometry()
        win32.unregister_hotkey(self._hwnd, HOTKEY_ID)
        win32.unregister_hotkey(self._hwnd, ESC_HOTKEY_ID)
        win32.remove_clipboard_listener(self._hwnd)
        self.toast.hide()
        self.tray.hide()
        self.store.close()
        log.info("ClipDock 已退出")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def setup_logging(folder: Path) -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(folder / "clipdock.log", maxBytes=512_000, backupCount=1, encoding="utf-8")
    ]
    if sys.stderr is not None:  # pythonw 启动时没有控制台
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers
    )

    def log_unhandled(exc_type, exc, tb) -> None:
        logging.getLogger("clipdock").error("未处理的异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_unhandled


def _set_app_font(app: QApplication) -> None:
    font = QFont(app.font())
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                      "Noto Sans CJK SC", "Noto Sans SC"])
    if win32.IS_WINDOWS:
        font.setPointSizeF(9.5)
    app.setFont(font)


def main(argv: list[str] | None = None) -> int:
    if not win32.IS_WINDOWS and not os.environ.get("CLIPDOCK_ALLOW_NON_WINDOWS"):
        print("ClipDock 目前只支持 Windows。")
        return 1
    folder = data_dir()
    setup_logging(folder)
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # 面板隐藏时程序继续在托盘里运行
    _set_app_font(app)
    if notify_running_instance():
        log.info("ClipDock 已经在运行，已让它显示面板")
        return 0
    cfg_path = folder / "config.json"
    cfg = Config.load(cfg_path)
    if not cfg_path.exists():
        cfg.save(cfg_path)  # 生成一份默认配置，方便修改
    store = HistoryStore(folder / "history.db")
    controller = Controller(app, cfg, cfg_path, store, folder)
    try:
        return app.exec()
    finally:
        controller.shutdown()
