"""在任何平台上都能跑：检查结构体大小与 Windows x64 一致，非 Windows 上函数安全返回。"""
import ctypes
import sys

import pytest

from clipdock import win32

is64 = ctypes.sizeof(ctypes.c_void_p) == 8


@pytest.mark.skipif(not is64, reason="按 64 位 Windows 的大小核对")
def test_struct_sizes_match_windows_x64():
    assert ctypes.sizeof(win32.POINT) == 8
    assert ctypes.sizeof(win32.RECT) == 16
    assert ctypes.sizeof(win32.MSG) == 48
    assert ctypes.sizeof(win32.MONITORINFO) == 40
    assert ctypes.sizeof(win32.KEYBDINPUT) == 24
    assert ctypes.sizeof(win32.MOUSEINPUT) == 32
    assert ctypes.sizeof(win32.INPUT) == 40
    # MSG 字段偏移（nativeEvent 里按地址读取）
    assert win32.MSG.message.offset == 8
    assert win32.MSG.wParam.offset == 16
    assert win32.MSG.lParam.offset == 24


def test_input_union_fields_accessible():
    arr = (win32.INPUT * 2)()
    arr[0].type = win32.INPUT_KEYBOARD
    arr[0].ki.wVk = win32.VK_V
    arr[0].ki.dwFlags = win32.KEYEVENTF_KEYUP
    assert arr[0].ki.wVk == 0x56 and arr[0].type == 1


@pytest.mark.skipif(sys.platform == "win32", reason="只在非 Windows 上检查空实现")
def test_non_windows_stubs_are_safe():
    assert win32.get_foreground_window() == 0
    assert win32.clipboard_owner_pid() == 0
    assert win32.clipboard_is_private() is False
    assert win32.boot_time() > 0
    assert win32.caret_rect() is None
    assert win32.window_rect(1) is None and win32.is_maximized(1) is False
    assert win32.send_ctrl_v() is False
    assert win32.make_no_activate(123) is False
    assert win32.fullscreen_foreground(1) == 0
    assert win32.is_window_elevated(1) is None
    assert win32.force_foreground(1) is False
    win32.suppress_alt_win_menu()
    win32.set_topmost(1)
