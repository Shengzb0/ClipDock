"""Windows 底层接口（ctypes 实现，不依赖 pywin32）。

ClipDock 能“常驻又不抢焦点”“点一下就填进别的程序”，靠的是这里的几个 Win32 调用：

- make_no_activate()       给面板加 WS_EX_NOACTIVATE。点击面板时它不会被激活，
                           键盘焦点留在你原来的输入框里（屏幕软键盘也是这个思路）
- 剪贴板监听、clipboard_owner_pid()、clipboard_is_private()
                           剪贴板一变化就收到通知；判断是谁放的、有没有“不要记录”的隐私标记
                           （密码管理器复制密码时会带）。读写内容本身交给 Qt，它处理好了各种格式
- send_ctrl_v()            把内容放进剪贴板后模拟按一次 Ctrl+V，内容就进了原来的输入框
- fullscreen_foreground()  判断前台是不是全屏程序（看视频、玩游戏、放 PPT），是就让面板让开
- force_foreground()       搜索时临时激活了搜索框，用完要把焦点还给原来的窗口

非 Windows 平台上所有函数都返回“什么也没做”的默认值，方便在别的系统上预览界面、跑测试。
"""
from __future__ import annotations

import ctypes
import logging
import ntpath
import sys
import threading
import time
import uuid
from ctypes import POINTER, Structure, Union, byref, sizeof

from .hotkey import MOD_NOREPEAT

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# 基本类型：按 Windows 上的真实宽度定义。
# （ctypes.wintypes 在 Linux 上把 DWORD 定义成 8 字节，结构体大小会和 Windows 对不上）
# ---------------------------------------------------------------------------
BOOL = ctypes.c_int
INT = ctypes.c_int
UINT = ctypes.c_uint
SHORT = ctypes.c_int16
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
HANDLE = ctypes.c_void_p
HWND = HANDLE
HGLOBAL = HANDLE
HMONITOR = HANDLE
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
SIZE_T = ctypes.c_size_t
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.c_wchar_p


class POINT(Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MSG(Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
    ]


class GUITHREADINFO(Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("flags", DWORD),
        ("hwndActive", HWND),
        ("hwndFocus", HWND),
        ("hwndCapture", HWND),
        ("hwndMenuOwner", HWND),
        ("hwndMoveSize", HWND),
        ("hwndCaret", HWND),
        ("rcCaret", RECT),
    ]


class GUID(Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class VARIANT_I4(Structure):
    """只用到 vt 和 lVal 的 VARIANT（大小和真正的 VARIANT 一样：64 位 24 字节，32 位 16 字节）。"""
    _fields_ = [
        ("vt", WORD),
        ("reserved", WORD * 3),
        ("lVal", LONG),
        ("_pad", ctypes.c_ubyte * (2 * sizeof(ctypes.c_void_p) - 4)),
    ]


class MONITORINFO(Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HWND_TOPMOST = -1

WM_MOUSEACTIVATE = 0x0021
WM_HOTKEY = 0x0312
WM_CLIPBOARDUPDATE = 0x031D
MA_NOACTIVATE = 3


MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTONEAREST = 2

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
VK_ESCAPE = 0x1B
VK_MASK = 0xE8  # 未分配的虚拟键，用来“打断”单按 Alt/Win（AutoHotkey 也是这么做的）

OBJID_CARET = 0xFFFFFFF8  # -8
CHILDID_SELF = 0
VT_I4 = 3
COINIT_APARTMENTTHREADED = 0x2  # 辅助功能接口的调用方一般用单线程套间
IID_IACCESSIBLE = GUID.parse("618736E0-3C3D-11CF-810C-00AA00389B71")
_IACCESSIBLE_RELEASE = 2        # IUnknown::Release 在虚函数表里的位置
_IACCESSIBLE_ACC_LOCATION = 22  # IAccessible::accLocation 在虚函数表里的位置
# COM 方法用 stdcall（64 位上只有一种调用约定）；非 Windows 上跑测试时用 CFUNCTYPE 代替
_COMFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
ERROR_ACCESS_DENIED = 5

# 带这些格式的剪贴板内容表示“请不要记录”（Windows 自带的剪贴板历史也遵守）
_EXCLUDE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
_CAN_INCLUDE_FORMAT = "CanIncludeInClipboardHistory"  # 值为 0 表示不要记录

# 桌面、任务栏这些窗口铺满屏幕也不算“全屏程序”
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# ---------------------------------------------------------------------------
# 绑定 DLL 函数（只在 Windows 上执行）
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    _oleacc = ctypes.WinDLL("oleacc", use_last_error=True)

    def _bind(dll, name, restype, *argtypes):
        func = getattr(dll, name)
        func.restype = restype
        func.argtypes = argtypes
        return func

    # 窗口
    _GetForegroundWindow = _bind(_user32, "GetForegroundWindow", HWND)
    _SetForegroundWindow = _bind(_user32, "SetForegroundWindow", BOOL, HWND)
    _BringWindowToTop = _bind(_user32, "BringWindowToTop", BOOL, HWND)
    _IsWindow = _bind(_user32, "IsWindow", BOOL, HWND)
    _IsZoomed = _bind(_user32, "IsZoomed", BOOL, HWND)
    _GetWindowThreadProcessId = _bind(_user32, "GetWindowThreadProcessId", DWORD, HWND, POINTER(DWORD))
    _AttachThreadInput = _bind(_user32, "AttachThreadInput", BOOL, DWORD, DWORD, BOOL)
    _GetClassNameW = _bind(_user32, "GetClassNameW", INT, HWND, LPWSTR, INT)
    _GetWindowRect = _bind(_user32, "GetWindowRect", BOOL, HWND, POINTER(RECT))
    _MonitorFromWindow = _bind(_user32, "MonitorFromWindow", HMONITOR, HWND, DWORD)
    _GetMonitorInfoW = _bind(_user32, "GetMonitorInfoW", BOOL, HMONITOR, POINTER(MONITORINFO))
    _SetWindowPos = _bind(_user32, "SetWindowPos", BOOL, HWND, HWND, INT, INT, INT, INT, UINT)
    _GetGUIThreadInfo = _bind(_user32, "GetGUIThreadInfo", BOOL, DWORD, POINTER(GUITHREADINFO))
    _ClientToScreen = _bind(_user32, "ClientToScreen", BOOL, HWND, POINTER(POINT))
    _IsHungAppWindow = _bind(_user32, "IsHungAppWindow", BOOL, HWND)
    if hasattr(_user32, "GetWindowLongPtrW"):  # 64 位
        _GetWindowLongPtrW = _bind(_user32, "GetWindowLongPtrW", LONG_PTR, HWND, INT)
        _SetWindowLongPtrW = _bind(_user32, "SetWindowLongPtrW", LONG_PTR, HWND, INT, LONG_PTR)
    else:  # 32 位 Python 上只有 GetWindowLongW
        _GetWindowLongPtrW = _bind(_user32, "GetWindowLongW", LONG, HWND, INT)
        _SetWindowLongPtrW = _bind(_user32, "SetWindowLongW", LONG, HWND, INT, LONG)
    # 快捷键与键盘输入
    _RegisterHotKey = _bind(_user32, "RegisterHotKey", BOOL, HWND, INT, UINT, UINT)
    _UnregisterHotKey = _bind(_user32, "UnregisterHotKey", BOOL, HWND, INT)
    _SendInput = _bind(_user32, "SendInput", UINT, UINT, POINTER(INPUT), INT)
    _MapVirtualKeyW = _bind(_user32, "MapVirtualKeyW", UINT, UINT, UINT)
    _GetAsyncKeyState = _bind(_user32, "GetAsyncKeyState", SHORT, INT)
    # 剪贴板
    _AddClipboardFormatListener = _bind(_user32, "AddClipboardFormatListener", BOOL, HWND)
    _RemoveClipboardFormatListener = _bind(_user32, "RemoveClipboardFormatListener", BOOL, HWND)
    _OpenClipboard = _bind(_user32, "OpenClipboard", BOOL, HWND)
    _CloseClipboard = _bind(_user32, "CloseClipboard", BOOL)
    _GetClipboardData = _bind(_user32, "GetClipboardData", HANDLE, UINT)
    _IsClipboardFormatAvailable = _bind(_user32, "IsClipboardFormatAvailable", BOOL, UINT)
    _RegisterClipboardFormatW = _bind(_user32, "RegisterClipboardFormatW", UINT, LPCWSTR)
    _GetClipboardOwner = _bind(_user32, "GetClipboardOwner", HWND)
    _GetClipboardSequenceNumber = _bind(_user32, "GetClipboardSequenceNumber", DWORD)
    _CountClipboardFormats = _bind(_user32, "CountClipboardFormats", INT)
    _OleFlushClipboard = _bind(_ole32, "OleFlushClipboard", ctypes.c_long)
    _CoInitializeEx = _bind(_ole32, "CoInitializeEx", ctypes.c_long, ctypes.c_void_p, DWORD)
    _CoUninitialize = _bind(_ole32, "CoUninitialize", None)
    _AccessibleObjectFromWindow = _bind(
        _oleacc, "AccessibleObjectFromWindow", ctypes.c_long, HWND, DWORD, POINTER(GUID), POINTER(ctypes.c_void_p)
    )
    # 内存、进程、权限
    _GlobalLock = _bind(_kernel32, "GlobalLock", ctypes.c_void_p, HGLOBAL)
    _GlobalUnlock = _bind(_kernel32, "GlobalUnlock", BOOL, HGLOBAL)
    _GlobalSize = _bind(_kernel32, "GlobalSize", SIZE_T, HGLOBAL)
    _GetCurrentThreadId = _bind(_kernel32, "GetCurrentThreadId", DWORD)
    _GetTickCount64 = _bind(_kernel32, "GetTickCount64", ctypes.c_uint64)
    _GetDriveTypeW = _bind(_kernel32, "GetDriveTypeW", UINT, LPCWSTR)
    _GetCurrentProcess = _bind(_kernel32, "GetCurrentProcess", HANDLE)
    _OpenProcess = _bind(_kernel32, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
    _CloseHandle = _bind(_kernel32, "CloseHandle", BOOL, HANDLE)
    _QueryFullProcessImageNameW = _bind(
        _kernel32, "QueryFullProcessImageNameW", BOOL, HANDLE, DWORD, LPWSTR, POINTER(DWORD)
    )
    _OpenProcessToken = _bind(_advapi32, "OpenProcessToken", BOOL, HANDLE, DWORD, POINTER(HANDLE))
    _GetTokenInformation = _bind(
        _advapi32, "GetTokenInformation", BOOL, HANDLE, INT, ctypes.c_void_p, DWORD, POINTER(DWORD)
    )


def _h(value) -> int:
    """把 ctypes 返回的句柄（int 或 None）统一成 int。"""
    return int(value or 0)


# ---------------------------------------------------------------------------
# 窗口与进程
# ---------------------------------------------------------------------------
def get_foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    return _h(_GetForegroundWindow())


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = DWORD(0)
    _GetWindowThreadProcessId(hwnd, byref(pid))
    return pid.value


def class_name(hwnd: int) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if _GetClassNameW(hwnd, buf, len(buf)) else ""


def process_name(pid: int) -> str:
    """进程的 exe 文件名（例如 chrome.exe），拿不到时返回空字符串。"""
    if not IS_WINDOWS or not pid:
        return ""
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return ntpath.basename(buf.value)
        return ""
    finally:
        _CloseHandle(handle)


def make_no_activate(hwnd: int) -> bool:
    """让窗口被点击时不激活：键盘焦点留在原来的窗口。这是“点一下就填进别的程序”的关键。"""
    if not IS_WINDOWS or not hwnd:
        return False
    ex_style = _GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    wanted = (ex_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if wanted != ex_style:
        _SetWindowLongPtrW(hwnd, GWL_EXSTYLE, wanted)
        _SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    return True


def set_topmost(hwnd: int) -> None:
    """置顶（不激活）。"""
    if IS_WINDOWS and hwnd:
        _SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def force_foreground(hwnd: int) -> bool:
    """把某个窗口切到前台。只有本程序“有资格”时才会成功（例如用户刚点过本程序的窗口）。"""
    if not IS_WINDOWS or not hwnd or not _IsWindow(hwnd):
        return False
    if get_foreground_window() == hwnd:
        return True
    if _SetForegroundWindow(hwnd) and get_foreground_window() == hwnd:
        return True
    # 兜底：临时把输入队列和当前前台线程连起来再切换
    current = _GetCurrentThreadId()
    foreground = get_foreground_window()
    fg_thread = _GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = False
    if fg_thread and fg_thread != current:
        attached = bool(_AttachThreadInput(current, fg_thread, True))
    try:
        _BringWindowToTop(hwnd)
        _SetForegroundWindow(hwnd)
    finally:
        if attached:
            _AttachThreadInput(current, fg_thread, False)
    return get_foreground_window() == hwnd


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口在屏幕上的位置 (左, 上, 右, 下)，物理像素。"""
    if not IS_WINDOWS or not hwnd:
        return None
    rect = RECT()
    if not _GetWindowRect(hwnd, byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def is_maximized(hwnd: int) -> bool:
    return bool(IS_WINDOWS and hwnd and _IsZoomed(hwnd))


# ---------------------------------------------------------------------------
# 输入光标的位置
# ---------------------------------------------------------------------------
def caret_rect(timeout: float = 0.3) -> tuple[int, int, int, int] | None:
    """前台窗口里输入光标的位置 (x, y, 宽, 高)：屏幕坐标、物理像素。找不到时返回 None。

    先问系统光标（GetGUIThreadInfo）：记事本、Word、大部分传统程序都会报告。
    问不到再走辅助功能接口（MSAA 的 OBJID_CARET）：Chrome、Edge 和 Electron 应用（VS Code、飞书等）要靠它。
    """
    if not IS_WINDOWS:
        return None
    foreground = _h(_GetForegroundWindow())
    bounds = window_rect(foreground)
    if bounds is None:
        return None
    focus = foreground
    info = GUITHREADINFO()
    info.cbSize = sizeof(GUITHREADINFO)
    thread = _GetWindowThreadProcessId(foreground, None)
    if thread and _GetGUIThreadInfo(thread, byref(info)):
        focus = _h(info.hwndFocus) or foreground
        rect = _system_caret(info)
        if _plausible_caret(rect, bounds):
            return rect
    if _IsHungAppWindow(foreground):  # 程序没响应：别去问它，免得把自己也卡住
        return None
    rect = _accessible_caret(focus, timeout)
    return rect if _plausible_caret(rect, bounds) else None


def _system_caret(info: GUITHREADINFO) -> tuple[int, int, int, int] | None:
    hwnd = _h(info.hwndCaret)
    rc = info.rcCaret
    if not hwnd or rc.bottom <= rc.top:
        return None
    point = POINT(rc.left, rc.top)  # rcCaret 是相对 hwndCaret 客户区的坐标
    if not _ClientToScreen(hwnd, byref(point)):
        return None
    return point.x, point.y, max(1, rc.right - rc.left), rc.bottom - rc.top


def _plausible_caret(rect, bounds) -> bool:
    """排除明显不对的结果：没有光标时有的程序报 (0,0,0,0)，或者报到窗口外面去。"""
    if rect is None:
        return False
    x, y, width, height = rect
    left, top, right, bottom = bounds
    if not (2 <= height <= 400 and 0 <= width <= 400) or (x == 0 and y == 0):
        return False
    return left - 4 <= x <= right + 4 and top - 4 <= y <= bottom + 4


def _accessible_caret(hwnd: int, timeout: float) -> tuple[int, int, int, int] | None:
    """通过辅助功能接口问光标位置。放在后台线程里问，对方程序卡住时最多等 timeout 秒。"""
    result: list = []

    def work() -> None:
        initialized = _CoInitializeEx(None, COINIT_APARTMENTTHREADED) >= 0
        try:
            rect = _accessible_caret_now(hwnd)
            if rect is not None:
                result.append(rect)
        except Exception as exc:  # 接口出错就当没找到
            log.debug("读取光标位置失败：%s", exc)
        finally:
            if initialized:
                _CoUninitialize()

    thread = threading.Thread(target=work, name="ClipDock-caret", daemon=True)
    thread.start()
    thread.join(timeout)
    return result[0] if result else None


def _com_method(obj: int, index: int, restype, *argtypes):
    """取 COM 对象虚函数表里第 index 个方法。"""
    vtable = ctypes.cast(obj, POINTER(POINTER(ctypes.c_void_p))).contents
    return _COMFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _accessible_caret_now(hwnd: int) -> tuple[int, int, int, int] | None:
    acc = ctypes.c_void_p()
    hr = _AccessibleObjectFromWindow(hwnd, OBJID_CARET, byref(IID_IACCESSIBLE), byref(acc))
    if hr != 0 or not acc.value:
        return None
    try:
        x, y, width, height = LONG(), LONG(), LONG(), LONG()
        child = VARIANT_I4(vt=VT_I4, lVal=CHILDID_SELF)
        out = (POINTER(LONG),) * 4
        if sizeof(ctypes.c_void_p) == 8:
            # 64 位调用约定：超过 8 字节的结构体（VARIANT 是 24 字节）按指针传
            location = _com_method(acc.value, _IACCESSIBLE_ACC_LOCATION, ctypes.c_long, *out, ctypes.c_void_p)
            hr = location(acc.value, byref(x), byref(y), byref(width), byref(height), ctypes.addressof(child))
        else:
            location = _com_method(acc.value, _IACCESSIBLE_ACC_LOCATION, ctypes.c_long, *out, VARIANT_I4)
            hr = location(acc.value, byref(x), byref(y), byref(width), byref(height), child)
        if hr != 0:
            return None
        return x.value, y.value, width.value, height.value
    finally:
        _com_method(acc.value, _IACCESSIBLE_RELEASE, ctypes.c_ulong)(acc.value)


def monitor_of(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    return _h(_MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))


def fullscreen_foreground(ignore_pid: int, monitor: int = 0) -> int:
    """前台窗口是全屏程序时返回它的句柄，否则返回 0。

    monitor 不为 0 时，只看那块屏幕（多显示器时，别的屏幕全屏不影响面板）。
    """
    if not IS_WINDOWS:
        return 0
    fg = get_foreground_window()
    if not fg or window_pid(fg) == ignore_pid or class_name(fg) in _SHELL_CLASSES:
        return 0
    mon = _h(_MonitorFromWindow(fg, MONITOR_DEFAULTTONULL))
    if not mon or (monitor and mon != monitor):
        return 0
    info = MONITORINFO()
    info.cbSize = sizeof(MONITORINFO)
    rect = RECT()
    if not _GetMonitorInfoW(mon, byref(info)) or not _GetWindowRect(fg, byref(rect)):
        return 0
    screen = info.rcMonitor
    if (
        rect.left > screen.left
        or rect.top > screen.top
        or rect.right < screen.right
        or rect.bottom < screen.bottom
    ):
        return 0
    style = _GetWindowLongPtrW(fg, GWL_STYLE)
    if _IsZoomed(fg) and (style & WS_CAPTION) == WS_CAPTION:
        return 0  # 任务栏自动隐藏时，普通的最大化窗口也会铺满屏幕，这不算全屏
    return fg


def _token_elevated(process_handle) -> bool | None:
    token = HANDLE()
    if not _OpenProcessToken(process_handle, TOKEN_QUERY, byref(token)):
        # 普通权限打不开管理员进程的令牌，这本身就说明对方是提权运行的
        return True if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
    try:
        elevation = DWORD(0)
        returned = DWORD(0)
        if _GetTokenInformation(token, TOKEN_ELEVATION_CLASS, byref(elevation), sizeof(elevation), byref(returned)):
            return bool(elevation.value)
        return None
    finally:
        _CloseHandle(token)


def is_current_process_elevated() -> bool:
    if not IS_WINDOWS:
        return False
    return bool(_token_elevated(_GetCurrentProcess()))


def is_window_elevated(hwnd: int) -> bool | None:
    """窗口所属进程是否以管理员身份运行。True/False，判断不了时返回 None。"""
    if not IS_WINDOWS or not hwnd:
        return None
    pid = window_pid(hwnd)
    if not pid:
        return None
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return True if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
    try:
        return _token_elevated(handle)
    finally:
        _CloseHandle(handle)


# ---------------------------------------------------------------------------
# 全局快捷键与模拟按键
# ---------------------------------------------------------------------------
def register_hotkey(hwnd: int, hotkey_id: int, modifiers: int, vk: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    ok = bool(_RegisterHotKey(hwnd, hotkey_id, modifiers | MOD_NOREPEAT, vk))
    if not ok:
        log.warning("RegisterHotKey 失败，错误码 %s", ctypes.get_last_error())
    return ok


def unregister_hotkey(hwnd: int, hotkey_id: int) -> None:
    if IS_WINDOWS and hwnd:
        _UnregisterHotKey(hwnd, hotkey_id)


def _send_keys(keys: list[tuple[int, bool]]) -> bool:
    inputs = (INPUT * len(keys))()
    for i, (vk, key_up) in enumerate(keys):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].ki.wVk = vk
        inputs[i].ki.wScan = _MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFFFF
        inputs[i].ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    sent = _SendInput(len(keys), inputs, sizeof(INPUT))
    if sent != len(keys):
        log.warning("SendInput 只发出了 %s/%s 个按键，错误码 %s", sent, len(keys), ctypes.get_last_error())
    return sent == len(keys)


def send_ctrl_v() -> bool:
    """模拟按一次 Ctrl+V，发给当前前台窗口。"""
    if not IS_WINDOWS:
        return False
    return _send_keys([(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)])


def send_escape() -> bool:
    """模拟按一次 Esc（把被临时拦下的 Esc 还给前台程序）。"""
    if not IS_WINDOWS:
        return False
    return _send_keys([(VK_ESCAPE, False), (VK_ESCAPE, True)])


def suppress_alt_win_menu() -> None:
    """快捷键带 Alt/Win 时，补按一个无意义的键：
    避免松开 Alt 时激活前台程序的菜单栏、松开 Win 时弹出开始菜单。"""
    if not IS_WINDOWS:
        return
    if any(_GetAsyncKeyState(vk) & 0x8000 for vk in (VK_MENU, VK_LWIN, VK_RWIN)):
        _send_keys([(VK_MASK, False), (VK_MASK, True)])


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------
_FORMAT_IDS: dict[str, int] = {}


def _format_id(name: str) -> int:
    fmt = _FORMAT_IDS.get(name)
    if fmt is None:
        fmt = int(_RegisterClipboardFormatW(name))
        _FORMAT_IDS[name] = fmt
    return fmt


def add_clipboard_listener(hwnd: int) -> bool:
    """剪贴板一变化，系统就给这个窗口发 WM_CLIPBOARDUPDATE。"""
    if not IS_WINDOWS or not hwnd:
        return False
    ok = bool(_AddClipboardFormatListener(hwnd))
    if not ok:
        log.error("AddClipboardFormatListener 失败，错误码 %s", ctypes.get_last_error())
    return ok


def remove_clipboard_listener(hwnd: int) -> None:
    if IS_WINDOWS and hwnd:
        _RemoveClipboardFormatListener(hwnd)


def clipboard_sequence() -> int:
    """剪贴板序号，每次内容变化都会加一。"""
    if not IS_WINDOWS:
        return 0
    return int(_GetClipboardSequenceNumber())


def clipboard_format_count() -> int:
    """剪贴板里有几种格式（不需要打开剪贴板，别的程序占用时也能查）。"""
    if not IS_WINDOWS:
        return 0
    return max(0, int(_CountClipboardFormats()))


def flush_clipboard() -> bool:
    """Qt 放进剪贴板的内容默认是“别人来取时才生成”。这里让它立即生成好：
    粘贴时不用等 ClipDock 响应，ClipDock 退出后剪贴板里的内容也还在。"""
    if not IS_WINDOWS:
        return True
    return _OleFlushClipboard() == 0  # S_OK


def clipboard_owner_pid() -> int:
    """当前剪贴板内容是哪个进程放的（拿不到时返回 0）。"""
    if not IS_WINDOWS:
        return 0
    return window_pid(_h(_GetClipboardOwner()))


def _open_clipboard(owner: int) -> bool:
    # 别的程序可能正占着剪贴板，稍等重试几次
    for _ in range(15):
        if _OpenClipboard(owner or None):
            return True
        time.sleep(0.01)
    log.warning("剪贴板被其他程序占用，打开失败")
    return False


def _read_dword_locked(fmt: int) -> int | None:
    """读取一个 DWORD 格式的剪贴板数据（调用前剪贴板必须已打开）。"""
    handle = _GetClipboardData(fmt)
    if not handle:
        return None
    ptr = _GlobalLock(handle)
    if not ptr:
        return None
    try:
        if _GlobalSize(handle) < 4:
            return None
        return ctypes.c_uint32.from_address(ptr).value
    finally:
        _GlobalUnlock(handle)


def clipboard_is_private() -> bool:
    """剪贴板内容是否带了“请不要记录”的标记（Windows 自带的剪贴板历史也遵守这些标记）。"""
    if not IS_WINDOWS:
        return False
    for name in _EXCLUDE_FORMATS:
        if _IsClipboardFormatAvailable(_format_id(name)):
            return True
    can_include = _format_id(_CAN_INCLUDE_FORMAT)
    if not _IsClipboardFormatAvailable(can_include):
        return False
    if not _open_clipboard(0):
        return True  # 带了这个标记却读不到值：宁可不记录
    try:
        return _read_dword_locked(can_include) == 0
    finally:
        _CloseClipboard()


DRIVE_REMOTE = 4


def is_remote_path(path: str) -> bool:
    """网络路径（\\\\服务器\\共享，或映射成盘符的网络驱动器）。
    网络断开时检查这类路径是否存在可能要卡很久，所以不检查。"""
    if path.startswith(("\\\\", "//")):
        return True
    if not IS_WINDOWS or len(path) < 2 or path[1] != ":":
        return False
    return _GetDriveTypeW(path[:2] + "\\") == DRIVE_REMOTE


def boot_time() -> float:
    """这次开机的时间（Unix 时间戳）。用来判断上次运行之后电脑有没有重启过。"""
    if IS_WINDOWS:
        return time.time() - _GetTickCount64() / 1000.0
    return time.time() - time.monotonic()
