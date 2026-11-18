"""全局快捷键字符串的解析与显示。

配置里写 "win+alt+v" 这样的字符串，这里把它转换成 RegisterHotKey 需要的
（修饰键位掩码, 虚拟键码）。纯 Python，没有平台依赖，方便测试。
"""
from __future__ import annotations

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 按住不放时不重复触发

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

# 名字 -> 虚拟键码（字母、数字、F1-F24 在 _vk_from_name 里单独处理）
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "`": 0xC0,
    "backquote": 0xC0,
    "grave": 0xC0,
    "-": 0xBD,
    "minus": 0xBD,
    "=": 0xBB,
    "equal": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    "backslash": 0xDC,
    ";": 0xBA,
    "semicolon": 0xBA,
    "'": 0xDE,
    "quote": 0xDE,
    ",": 0xBC,
    "comma": 0xBC,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
}

# 显示用的名字（虚拟键码 -> 文本）
_KEY_LABELS = {
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x2D: "Insert", 0x2E: "Delete",
    0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown", 0x26: "Up", 0x28: "Down",
    0x25: "Left", 0x27: "Right", 0xC0: "`", 0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]",
    0xDC: "\\", 0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/",
}

_DISPLAY_ORDER = [(MOD_WIN, "Win"), (MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift")]


def _vk_from_name(name: str) -> int:
    if len(name) == 1 and name.isascii() and name.isalpha():
        return ord(name.upper())
    if len(name) == 1 and name.isascii() and name.isdigit():
        return ord(name)
    if name.startswith("f") and name[1:].isdigit():
        n = int(name[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    raise ValueError(f"不认识的按键：{name}")


def parse_hotkey(text: str) -> tuple[int, int]:
    """把 "win+alt+v" 解析成 (修饰键位掩码, 虚拟键码)，格式不对时抛 ValueError。"""
    parts = [p.strip().lower() for p in text.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"快捷键格式不对：{text!r}，应该像 win+alt+v 这样")
    mods = 0
    key = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
            continue
        if key is not None:
            raise ValueError(f"快捷键里只能有一个普通键：{text!r}")
        key = _vk_from_name(part)
    if key is None:
        raise ValueError(f"快捷键缺少普通键：{text!r}")
    if mods == 0:
        raise ValueError(f"快捷键至少要带一个 Ctrl/Alt/Shift/Win：{text!r}")
    return mods, key


def format_hotkey(text: str) -> str:
    """把配置里的快捷键变成好看的显示文本，例如 "win+alt+v" -> "Win+Alt+V"。"""
    try:
        mods, vk = parse_hotkey(text)
    except ValueError:
        return text
    names = [label for bit, label in _DISPLAY_ORDER if mods & bit]
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        key = chr(vk)
    elif 0x70 <= vk <= 0x87:
        key = f"F{vk - 0x70 + 1}"
    else:
        key = _KEY_LABELS.get(vk, hex(vk))
    return "+".join(names + [key])
