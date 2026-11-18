import json

import pytest

from clipdock.config import Config
from clipdock.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, format_hotkey, parse_hotkey


def test_parse_default():
    assert parse_hotkey("win+alt+v") == (MOD_WIN | MOD_ALT, 0x56)
    assert parse_hotkey(" Ctrl + Shift + F12 ") == (MOD_CONTROL | MOD_SHIFT, 0x7B)
    assert parse_hotkey("ctrl+`") == (MOD_CONTROL, 0xC0)
    assert parse_hotkey("alt+space") == (MOD_ALT, 0x20)
    assert parse_hotkey("ctrl+alt+1") == (MOD_CONTROL | MOD_ALT, 0x31)


@pytest.mark.parametrize("bad", ["v", "ctrl+", "ctrl+a+b", "ctrl+foo", "", "win+alt", "ctrl+f25"])
def test_parse_errors(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_format():
    assert format_hotkey("alt+win+v") == "Win+Alt+V"
    assert format_hotkey("ctrl+shift+f2") == "Ctrl+Shift+F2"
    assert format_hotkey("nonsense") == "nonsense"


def test_config_load_validates(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "hotkey": "ctrl+alt+c",
                "max_items": "many",       # 类型不对，忽略
                "hide_when_fullscreen": 0,  # 不是 bool，忽略
                "paused": True,
                "geometry": [10, 20, 300, 400],
                "unknown": 1,
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.hotkey == "ctrl+alt+c"
    assert cfg.max_items == 500
    assert cfg.hide_when_fullscreen is True
    assert cfg.paused is True
    assert cfg.geometry == [10, 20, 300, 400]
    cfg.max_items = 42
    cfg.save(path)
    assert Config.load(path).max_items == 42


def test_config_broken_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert Config.load(path) == Config()


def test_background_opacity_is_clamped(tmp_path):
    path = tmp_path / "config.json"
    for raw, expected in ((5, 10), (150, 100), (55, 55), ("x", 70), (0.5, 70)):
        path.write_text(json.dumps({"background_opacity": raw}), encoding="utf-8")
        assert Config.load(path).background_opacity == expected, raw
