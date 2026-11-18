"""配置文件与数据目录。

数据都放在 %APPDATA%\\ClipDock 下：
- config.json   配置（第一次运行会自动生成，改完重启生效）
- history.db    复制历史（SQLite）
- clipdock.log  运行日志，出问题时看这里
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)


def data_dir() -> Path:
    """数据目录。可以用环境变量 CLIPDOCK_DATA_DIR 指定（测试时用）。"""
    override = os.environ.get("CLIPDOCK_DATA_DIR")
    if override:
        path = Path(override)
    elif os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / APP_NAME
    else:
        path = Path.home() / ".clipdock"
    path.mkdir(parents=True, exist_ok=True)
    return path


POPUP_POSITIONS = ("caret", "last")


@dataclass
class Config:
    hotkey: str = "win+alt+v"          # 呼出面板的全局快捷键
    popup_position: str = "caret"      # 按快捷键时面板出现在哪："caret" 输入光标旁边，"last" 上次的位置
    max_items: int = 500               # 最多保留多少条未收藏的记录（收藏的不算）
    max_image_mb: int = 300            # 未收藏的图片等数据最多占多少 MB，超出时从最旧的删起
    max_text_chars: int = 200_000      # 单条最多多少字，超过的不记录
    hide_when_fullscreen: bool = True  # 前台是全屏程序时自动隐藏
    show_on_start: bool = True         # 启动时显示面板
    background_opacity: int = 70       # 面板空白处的不透明度（%），100 为不透明，最低 10
    copy_feedback: bool = True         # 复制成功时在鼠标旁边提示
    clear_on_shutdown: bool = False    # 关机/注销时清空历史（收藏保留）
    group_gap_minutes: int = 5         # 两次复制间隔不超过这么多分钟，就归为同一组
    paused: bool = False               # 暂停记录
    geometry: list[int] | None = None  # 面板位置和大小 [x, y, 宽, 高]，自动保存

    @classmethod
    def load(cls, path: Path) -> Config:
        cfg = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError) as exc:
            log.warning("配置文件读取失败，使用默认配置：%s", exc)
            return cfg
        if not isinstance(raw, dict):
            return cfg
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name == "geometry":
                if (
                    isinstance(value, list)
                    and len(value) == 4
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
                ):
                    cfg.geometry = value
                continue
            default = getattr(cfg, f.name)
            # 注意 bool 是 int 的子类，所以先判断 bool
            if isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(cfg, f.name, value)
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    setattr(cfg, f.name, value)
            elif isinstance(default, str):
                if isinstance(value, str) and value.strip():
                    setattr(cfg, f.name, value.strip())
        cfg.background_opacity = max(10, min(100, cfg.background_opacity))
        cfg.group_gap_minutes = max(1, min(240, cfg.group_gap_minutes))
        cfg.max_image_mb = max(20, min(10_000, cfg.max_image_mb))
        if cfg.popup_position not in POPUP_POSITIONS:
            cfg.popup_position = "caret"
        return cfg

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 先写临时文件再替换，避免写一半断电把配置弄坏
