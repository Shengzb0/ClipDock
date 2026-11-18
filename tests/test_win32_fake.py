"""用“假的 Windows”把 win32.py 里只在 Windows 上执行的代码实际跑一遍。

做法：临时把 sys.platform 改成 win32，把 ctypes.WinDLL 换成假的 DLL，
再重新加载 clipdock.win32。假 DLL 用 Python 模拟剪贴板、窗口、按键等行为。
这样在任何系统上都能检查调用顺序、参数和数据格式有没有写错。
"""
import ctypes
import importlib
import sys

import pytest

import clipdock.win32 as win32_module

CF_UNICODETEXT = 13


class FakeFunc:
    def __init__(self, name, impl):
        self.name = name
        self.impl = impl
        self.restype = None
        self.argtypes = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


class FakeDLL:
    def __init__(self, impls):
        self._impls = impls
        self._funcs = {}

    def __getattr__(self, name):
        if name.startswith("_") or name not in self._impls:
            raise AttributeError(name)
        if name not in self._funcs:
            self._funcs[name] = FakeFunc(name, self._impls[name])
        return self._funcs[name]


class FakeAccessible:
    """用 ctypes 搭出来的假 COM 对象：虚函数表第 2 项是 Release，第 22 项是 accLocation。
    回调里不做断言（ctypes 回调里的异常不会传出来），只记录，测试里再检查。"""

    def __init__(self, location, hr=0):
        self.location = location
        self.hr = hr
        self.released = 0
        self.this = []
        self.children = []
        long_p = ctypes.POINTER(ctypes.c_int32)
        release_t = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        location_t = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, long_p, long_p, long_p, long_p,
                                      ctypes.c_void_p)

        def release(this):
            self.this.append(this)
            self.released += 1
            return 0

        def acc_location(this, px, py, pw, ph, pvar):
            self.this.append(this)
            var = ctypes.cast(pvar, ctypes.POINTER(win32_module.VARIANT_I4)).contents
            self.children.append((var.vt, var.lVal))
            px[0], py[0], pw[0], ph[0] = self.location
            return self.hr

        self._callbacks = (release_t(release), location_t(acc_location))
        self.vtable = (ctypes.c_void_p * 27)()
        self.vtable[2] = ctypes.cast(self._callbacks[0], ctypes.c_void_p).value
        self.vtable[22] = ctypes.cast(self._callbacks[1], ctypes.c_void_p).value
        self.obj = (ctypes.c_void_p * 1)(ctypes.addressof(self.vtable))
        self.ptr = ctypes.addressof(self.obj)


def _obj(arg):
    """byref(x) 传进来的是 CArgObject，._obj 是原对象。"""
    return getattr(arg, "_obj", arg)


class World:
    """一个极简的“Windows”。"""

    def __init__(self):
        self.last_error = 0
        self.fg = 0
        self.allow_set_fg = True
        self.windows = {}
        self.processes = {}
        self.monitors = {1: (0, 0, 1920, 1080), 2: (1920, 0, 3840, 1080)}
        self.formats = {}
        self.format_ids = {}
        self.owner = 0
        self.seq = 1
        self.clip_open = False
        self.clip_open_owner = 0
        self.busy_opens = 0
        self.globals = {}
        self.next_handle = 1000
        self.sent = []
        self.held = set()
        self.attach_calls = []
        self.uptime_ms = 3_600_000
        self.gui_caret = None   # (光标所在窗口, (左, 上, 右, 下) 客户区坐标)
        self.focus_hwnd = 0
        self.hung = False
        self.acc = None
        self.acc_calls = []
        self.com = []
        self.dlls = {
            "user32": FakeDLL(self._user32()),
            "kernel32": FakeDLL(self._kernel32()),
            "advapi32": FakeDLL(self._advapi32()),
            "ole32": FakeDLL({
                "OleFlushClipboard": lambda: self.flush_hr,
                "CoInitializeEx": lambda reserved, flags: self.com.append(("init", flags)) or 0,
                "CoUninitialize": lambda: self.com.append(("uninit",)),
            }),
            "oleacc": FakeDLL({"AccessibleObjectFromWindow": self._acc_from_window}),
        }
        self.flush_hr = 0

    def add_window(self, hwnd, pid=100, tid=200, cls="App", rect=(100, 100, 800, 600),
                   zoomed=False, style=0x00CF0000, exstyle=0, monitor=1):
        self.windows[hwnd] = dict(pid=pid, tid=tid, cls=cls, rect=rect, zoomed=zoomed,
                                  style=style, exstyle=exstyle, monitor=monitor)

    def _acc_from_window(self, hwnd, objid, riid, ppv):
        self.acc_calls.append((hwnd, objid, bytes(_obj(riid))))
        if self.acc is None:
            return -2147467259  # E_FAIL
        _obj(ppv).value = self.acc.ptr
        return 0

    def _new_global(self, data: bytes):
        h = self.next_handle
        self.next_handle += 1
        self.globals[h] = ctypes.create_string_buffer(data, len(data))
        return h

    def _user32(self):
        w = self

        def set_fg(h):
            if w.allow_set_fg and h in w.windows:
                w.fg = h
                return 1
            return 0

        def thread_pid(h, ppid):
            if h not in w.windows:  # 和 Windows 一样：无效窗口返回 0
                return 0
            if ppid is not None:
                _obj(ppid).value = w.windows[h]["pid"]
            return w.windows[h]["tid"]

        def attach(a, b, flag):
            w.attach_calls.append((a, b, bool(flag)))
            if flag:
                w.allow_set_fg = True
            return 1

        def class_name(h, buf, n):
            buf.value = w.windows[h]["cls"]
            return len(buf.value)

        def window_rect(h, prect):
            r = _obj(prect)
            r.left, r.top, r.right, r.bottom = w.windows[h]["rect"]
            return 1

        def monitor_info(hmon, pinfo):
            info = _obj(pinfo)
            assert info.cbSize == ctypes.sizeof(win32_module.MONITORINFO)
            m = info.rcMonitor
            m.left, m.top, m.right, m.bottom = w.monitors[hmon]
            return 1

        def get_long(h, idx):
            return w.windows[h]["exstyle" if idx == -20 else "style"]

        def set_long(h, idx, value):
            key = "exstyle" if idx == -20 else "style"
            old = w.windows[h][key]
            w.windows[h][key] = value
            return old

        def send_input(n, arr, size):
            assert size == ctypes.sizeof(win32_module.INPUT)
            for i in range(n):
                assert arr[i].type == 1
                w.sent.append((arr[i].ki.wVk, arr[i].ki.dwFlags))
            return n

        def open_clip(h):
            if w.busy_opens > 0:
                w.busy_opens -= 1
                return 0
            assert not w.clip_open, "剪贴板被重复打开"
            w.clip_open = True
            w.clip_open_owner = h or 0
            return 1

        def close_clip():
            assert w.clip_open, "关闭了没打开的剪贴板"
            w.clip_open = False
            return 1

        def empty_clip():
            assert w.clip_open
            w.formats.clear()
            w.owner = w.clip_open_owner
            w.seq += 1
            return 1

        def get_data(fmt):
            assert w.clip_open, "读剪贴板前必须先打开"
            if fmt not in w.formats:
                return None
            return w._new_global(w.formats[fmt])

        def set_data(fmt, h):
            assert w.clip_open
            w.formats[fmt] = bytes(w.globals[h])
            return h

        def register_format(name):
            return w.format_ids.setdefault(name, 0xC000 + len(w.format_ids))

        def gui_info(tid, pinfo):
            info = _obj(pinfo)
            assert info.cbSize == ctypes.sizeof(win32_module.GUITHREADINFO)
            info.hwndFocus = w.focus_hwnd or None
            if w.gui_caret:
                hwnd, (left, top, right, bottom) = w.gui_caret
                info.hwndCaret = hwnd
                info.rcCaret.left, info.rcCaret.top, info.rcCaret.right, info.rcCaret.bottom = left, top, right, bottom
            return 1

        def client_to_screen(h, ppt):
            point = _obj(ppt)
            left, top = w.windows[h]["rect"][:2]
            point.x += left + 8   # 客户区在窗口边框和标题栏里面
            point.y += top + 31
            return 1

        return {
            "GetForegroundWindow": lambda: w.fg or None,
            "SetForegroundWindow": set_fg,
            "BringWindowToTop": lambda h: 1,
            "IsWindow": lambda h: int(h in w.windows),
            "IsZoomed": lambda h: int(w.windows[h]["zoomed"]),
            "GetWindowThreadProcessId": thread_pid,
            "AttachThreadInput": attach,
            "GetClassNameW": class_name,
            "GetWindowRect": window_rect,
            "MonitorFromWindow": lambda h, flags: w.windows[h]["monitor"],
            "GetMonitorInfoW": monitor_info,
            "SetWindowPos": lambda *a: 1,
            "GetWindowLongPtrW": get_long,
            "SetWindowLongPtrW": set_long,
            "RegisterHotKey": lambda h, i, mods, vk: 1,
            "UnregisterHotKey": lambda h, i: 1,
            "SendInput": send_input,
            "MapVirtualKeyW": lambda vk, t: {0x11: 0x1D, 0x56: 0x2F}.get(vk, 0),
            "GetAsyncKeyState": lambda vk: -32768 if vk in w.held else 0,
            "AddClipboardFormatListener": lambda h: 1,
            "RemoveClipboardFormatListener": lambda h: 1,
            "OpenClipboard": open_clip,
            "CloseClipboard": close_clip,
            "EmptyClipboard": empty_clip,
            "GetClipboardData": get_data,
            "SetClipboardData": set_data,
            "IsClipboardFormatAvailable": lambda fmt: int(fmt in w.formats),
            "RegisterClipboardFormatW": register_format,
            "GetClipboardOwner": lambda: w.owner or None,
            "GetClipboardSequenceNumber": lambda: w.seq,
            "CountClipboardFormats": lambda: len(w.formats),
            "GetGUIThreadInfo": gui_info,
            "ClientToScreen": client_to_screen,
            "IsHungAppWindow": lambda h: int(w.hung),
        }

    def _kernel32(self):
        w = self

        def global_alloc(flags, size):
            assert flags == 0x0002
            return w._new_global(b"\x00" * size)

        def open_process(access, inherit, pid):
            if pid in w.processes and not w.processes[pid].get("protected"):
                return 50000 + pid
            w.last_error = 5
            return None

        def query_name(h, flags, buf, psize):
            pid = h - 50000
            buf.value = w.processes[pid]["path"]
            _obj(psize).value = len(buf.value)
            return 1

        return {
            "GlobalAlloc": global_alloc,
            "GlobalLock": lambda h: ctypes.addressof(w.globals[h]),
            "GlobalUnlock": lambda h: 0,
            "GlobalSize": lambda h: len(w.globals[h]),
            "GlobalFree": lambda h: w.globals.pop(h) and None,
            "GetCurrentThreadId": lambda: 111,
            "GetTickCount64": lambda: w.uptime_ms,
            "GetDriveTypeW": lambda root: 4 if root.upper() == "Z:\\" else 3,
            "GetCurrentProcess": lambda: 0xFFFFFFFFFFFFFFFF,
            "OpenProcess": open_process,
            "CloseHandle": lambda h: 1,
            "QueryFullProcessImageNameW": query_name,
        }

    def _advapi32(self):
        w = self

        def open_token(h, access, ptoken):
            pid = 0 if h == 0xFFFFFFFFFFFFFFFF else h - 50000
            proc = w.processes.get(pid, {})
            if proc.get("elevated") and not w.processes.get(0, {}).get("elevated"):
                w.last_error = 5
                return 0
            _obj(ptoken).value = 70000 + pid
            return 1

        def token_info(token, cls, pvalue, size, pret):
            assert cls == 20 and size == 4
            pid = _obj(token).value - 70000
            _obj(pvalue).value = 1 if w.processes.get(pid, {}).get("elevated") else 0
            return 1

        return {"OpenProcessToken": open_token, "GetTokenInformation": token_info}


@pytest.fixture
def fake():
    world = World()
    mp = pytest.MonkeyPatch()
    mp.setattr(sys, "platform", "win32")
    mp.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: world.dlls[name], raising=False)
    mp.setattr(ctypes, "get_last_error", lambda: world.last_error, raising=False)
    # Linux 的 wchar_t 是 4 字节，这里按 Windows 的 UTF-16 读
    mp.setattr(ctypes, "wstring_at",
               lambda ptr, n: ctypes.string_at(ptr, n * 2).decode("utf-16-le", "surrogatepass"))
    try:
        w = importlib.reload(win32_module)
        assert w.IS_WINDOWS
        yield world, w
    finally:
        mp.undo()
        importlib.reload(win32_module)


def test_clipboard_owner_and_sequence(fake):
    world, w = fake
    world.add_window(500, pid=4242)
    world.owner = 500
    assert w.clipboard_owner_pid() == 4242
    world.owner = 0
    assert w.clipboard_owner_pid() == 0
    world.seq = 77
    assert w.clipboard_sequence() == 77


def test_privacy_markers(fake):
    world, w = fake
    world.formats[13] = "secret".encode("utf-16-le") + b"\x00\x00"
    assert not w.clipboard_is_private()
    exclude = w._format_id("ExcludeClipboardContentFromMonitorProcessing")
    world.formats[exclude] = b""
    assert w.clipboard_is_private()
    del world.formats[exclude]
    can_include = w._format_id("CanIncludeInClipboardHistory")
    world.formats[can_include] = (0).to_bytes(4, "little")
    assert w.clipboard_is_private()
    assert not world.clip_open  # 读完记得关上剪贴板
    world.formats[can_include] = (1).to_bytes(4, "little")
    assert not w.clipboard_is_private()
    world.formats[can_include] = (0).to_bytes(4, "little")
    world.busy_opens = 100  # 有标记但剪贴板一直被占用：宁可当成隐私内容
    assert w.clipboard_is_private()


def test_flush_clipboard(fake):
    world, w = fake
    world.formats = {13: b"a\x00\x00\x00", 1: b"a\x00"}
    assert w.clipboard_format_count() == 2
    assert w.flush_clipboard()
    world.flush_hr = -2147221040  # CLIPBRD_E_CANT_OPEN
    assert not w.flush_clipboard()
    assert len(world.dlls["ole32"]._funcs["OleFlushClipboard"].calls) == 2


def test_remote_paths(fake):
    world, w = fake
    assert w.is_remote_path("\\\\nas\\share\\a.txt")
    assert w.is_remote_path("Z:\\课件\\a.pptx")         # 映射的网络驱动器
    assert not w.is_remote_path("C:\\Users\\a.txt")
    assert not w.is_remote_path("relative.txt")


def test_caret_from_system_caret(fake):
    world, w = fake
    world.add_window(700, pid=4242, tid=900, rect=(100, 50, 900, 650))
    world.fg = 700
    world.gui_caret = (700, (40, 60, 42, 80))
    assert w.caret_rect() == (100 + 8 + 40, 50 + 31 + 60, 2, 20)
    assert world.acc_calls == []  # 系统光标问到了，就不用辅助功能接口


def test_caret_from_accessibility_for_browsers(fake):
    world, w = fake
    world.add_window(700, pid=4242, tid=900, rect=(0, 0, 1920, 1040), cls="Chrome_WidgetWin_1")
    world.add_window(701, pid=4242, tid=900, rect=(0, 80, 1920, 1040), cls="Chrome_RenderWidgetHostHWND")
    world.fg = 700
    world.focus_hwnd = 701  # Chrome 不用系统光标，要问有焦点的那个子窗口
    world.acc = FakeAccessible((612, 333, 1, 19))
    assert w.caret_rect() == (612, 333, 1, 19)
    assert world.acc_calls == [(701, 0xFFFFFFF8, bytes(w.IID_IACCESSIBLE))]
    assert world.acc.children == [(3, 0)]                    # VARIANT：VT_I4、CHILDID_SELF
    assert world.acc.this == [world.acc.ptr, world.acc.ptr]  # accLocation 和 Release 都用对了对象
    assert world.acc.released == 1
    assert world.com == [("init", 2), ("uninit",)]           # 后台线程里初始化、退出 COM


def test_caret_rejects_nonsense_and_hung_windows(fake):
    world, w = fake
    world.add_window(700, pid=4242, tid=900, rect=(100, 50, 900, 650))
    world.fg = 700
    assert w.caret_rect() is None                             # 两种办法都问不到
    world.acc = FakeAccessible((0, 0, 0, 0))                  # 没有光标时有的程序这样报
    assert w.caret_rect() is None
    world.acc = FakeAccessible((5000, 300, 1, 18))            # 跑到窗口外面去了
    assert w.caret_rect() is None
    world.acc = FakeAccessible((300, 300, 1, 18), hr=1)       # S_FALSE
    assert w.caret_rect() is None and world.acc.released == 1
    world.hung = True
    asked = len(world.acc_calls)
    assert w.caret_rect() is None and len(world.acc_calls) == asked  # 程序没响应：不去问它
    world.fg = 0
    assert w.caret_rect() is None


def test_window_rect_and_maximized(fake):
    world, w = fake
    world.add_window(700, rect=(10, 20, 410, 320), zoomed=True)
    assert w.window_rect(700) == (10, 20, 410, 320)
    assert w.is_maximized(700) and not w.is_maximized(0)


def test_boot_time_from_tick_count(fake):
    world, w = fake
    world.uptime_ms = 7_200_000  # 开机两小时
    import time as _time
    assert abs(w.boot_time() - (_time.time() - 7200)) < 2


def test_send_ctrl_v_and_menu_mask(fake):
    world, w = fake
    assert w.send_ctrl_v()
    assert world.sent == [(0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)]
    world.sent.clear()
    w.suppress_alt_win_menu()
    assert world.sent == []  # 没按着 Alt/Win 时什么都不做
    world.held = {0x12}
    w.suppress_alt_win_menu()
    assert world.sent == [(0xE8, 0), (0xE8, 2)]


def test_fullscreen_detection(fake):
    world, w = fake
    world.processes[1] = {"path": r"C:\me\python.exe"}
    world.add_window(10, pid=1, cls="Qt", monitor=1)            # 自己的面板
    world.add_window(20, pid=2, rect=(0, 0, 1920, 1080), style=0x80000000)            # 全屏视频
    world.add_window(30, pid=3, rect=(-8, -8, 1928, 1088), zoomed=True)                 # 自动隐藏任务栏时的最大化
    world.add_window(40, pid=4, rect=(-8, -8, 1928, 1048), zoomed=True)                 # 普通最大化
    world.add_window(50, pid=5, cls="WorkerW", rect=(0, 0, 1920, 1080), style=0x80000000)  # 桌面
    world.add_window(60, pid=6, rect=(1920, 0, 3840, 1080), style=0x80000000, monitor=2)   # 另一块屏幕全屏
    my_monitor = w.monitor_of(10)
    expected = {10: 0, 20: 20, 30: 0, 40: 0, 50: 0, 60: 0}
    for hwnd, want in expected.items():
        world.fg = hwnd
        assert w.fullscreen_foreground(ignore_pid=1, monitor=my_monitor) == want, hwnd
    world.fg = 60
    assert w.fullscreen_foreground(ignore_pid=1, monitor=0) == 60  # 不限定屏幕时算全屏


def test_no_activate_style(fake):
    world, w = fake
    world.add_window(10, exstyle=0x00040000 | 0x8)
    assert w.make_no_activate(10)
    ex = world.windows[10]["exstyle"]
    assert ex & 0x08000000 and ex & 0x80 and not ex & 0x00040000 and ex & 0x8


def test_force_foreground_fallback(fake):
    world, w = fake
    world.add_window(10, tid=300)
    world.add_window(20, tid=400)
    world.fg = 10
    world.allow_set_fg = False
    assert w.force_foreground(20)
    assert world.fg == 20
    assert world.attach_calls == [(111, 300, True), (111, 300, False)]
    assert not w.force_foreground(999)  # 不存在的窗口


def test_process_name_and_elevation(fake):
    world, w = fake
    world.processes[0] = {"elevated": False}
    world.processes[7] = {"path": r"C:\Program Files\Google\Chrome\Application\chrome.exe"}
    world.processes[8] = {"path": r"C:\Windows\regedit.exe", "elevated": True}
    world.processes[9] = {"path": "x", "protected": True}
    world.add_window(70, pid=7)
    world.add_window(80, pid=8)
    world.add_window(90, pid=9)
    assert w.process_name(7) == "chrome.exe"
    assert w.process_name(12345) == ""
    assert w.is_window_elevated(70) is False
    assert w.is_window_elevated(80) is True
    assert w.is_window_elevated(90) is True  # 打不开（拒绝访问），按提权处理
    assert w.is_current_process_elevated() is False


def test_hotkeys_and_escape(fake):
    world, w = fake
    world.add_window(10)
    assert w.register_hotkey(10, 1, 0x8 | 0x1, 0x56)
    assert w.register_hotkey(10, 2, 0, w.VK_ESCAPE)  # Esc 不带修饰键
    calls = world.dlls["user32"]._funcs["RegisterHotKey"].calls
    assert calls[-2:] == [(10, 1, 0x8 | 0x1 | 0x4000, 0x56), (10, 2, 0x4000, 0x1B)]
    assert w.send_escape()
    assert world.sent == [(0x1B, 0), (0x1B, 2)]
