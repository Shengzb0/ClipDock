"""剪贴板内容 ↔ 历史记录 之间的转换：文字（含 HTML/RTF 格式）、图片、文件。

读写剪贴板用 Qt 的 QClipboard / QMimeData：Qt 已经处理好了 Windows 各种剪贴板格式
（CF_UNICODETEXT、HTML Format、CF_DIB/PNG、CF_HDROP……）的转换。
这个模块只决定“存什么”和“填入时还原成什么”，不碰 Windows API，在任何系统上都能测试。
"""
from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, field

from PySide6.QtCore import QBuffer, QByteArray, QDir, QIODevice, QMimeData, QSize, Qt, QUrl
from PySide6.QtGui import QImage, QPixmap

from . import win32
from .store import content_digest, text_digest

HTML = "text/html"
RTF = "text/rtf"
PNG = "image/png"
THUMB = "thumb/png"
RTF_MIME = 'application/x-qt-windows-mime;value="Rich Text Format"'  # Qt 里 Windows 自定义格式的写法
THUMB_MAX = QSize(560, 144)  # 缩略图最大尺寸（像素）；显示时缩小一半，高分屏也清晰


@dataclass
class Capture:
    kind: str                      # "text" / "image" / "files"
    text: str = ""                 # 文字内容；图片是“图片 宽×高”；文件是路径（每行一个）
    digest: str = ""
    formats: dict[str, bytes] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    summary: str = ""              # 给“已复制”提示用：「张三」、图片 1920×1080、3 个文件
    too_large: bool = False


def describe_text(text: str, limit: int = 16) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > limit:
        one_line = one_line[:limit] + "…"
    return f"「{one_line}」"


def describe_files(paths: list[str]) -> str:
    if len(paths) == 1:
        return ntpath.basename(paths[0].rstrip("\\/")) or paths[0]
    return f"{len(paths)} 个文件"


def image_to_png(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def capture(mime: QMimeData | None, max_chars: int, max_image_bytes: int) -> Capture | None:
    """从剪贴板数据里抽取要保存的内容。认不出来（比如只有某个软件的私有格式）时返回 None。"""
    if mime is None:
        return None
    has_image = mime.hasImage()

    # 1. 文件：资源管理器里复制的文件。同时带图片的（比如聊天软件里复制图片）按图片处理
    paths: list[str] = []
    if mime.hasUrls():
        paths = [QDir.toNativeSeparators(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
    if paths and not has_image:
        text = "\n".join(paths)
        return Capture(kind="files", text=text, digest=content_digest("files", text.encode("utf-8")),
                       meta={"count": len(paths)}, summary=describe_files(paths))

    # 2. 文字：Word、Excel、网页里复制的文字常常同时带 HTML / RTF，一起存下来，填入时保留格式。
    #    文字排在图片前面：Excel 复制单元格时也会附带一张图片，但用户要的是文字和表格
    # 注意不能用 hasText()：Qt 会把文件链接也当成文字，用 hasFormat 只认真正的文字
    text = mime.text() if mime.hasFormat("text/plain") else ""
    if text.strip():
        if len(text) > max_chars:
            return Capture(kind="text", summary=f"{len(text):,} 字", too_large=True)
        formats: dict[str, bytes] = {}
        if mime.hasHtml():
            html = mime.html()
            if html.strip() and len(html) <= max_chars * 4:
                formats[HTML] = html.encode("utf-8")
        rtf_name = next((f for f in mime.formats() if 'value="Rich Text Format"' in f), None)
        if rtf_name:
            rtf = bytes(mime.data(rtf_name))
            if rtf and len(rtf) <= max_chars * 8:
                formats[RTF] = rtf
        return Capture(kind="text", text=text, digest=text_digest(text), formats=formats,
                       meta={"rich": True} if formats else {}, summary=describe_text(text))

    # 3. 图片：截图、网页或聊天软件里的“复制图片”
    if has_image:
        image = mime.imageData()
        if isinstance(image, QPixmap):
            image = image.toImage()
        if not isinstance(image, QImage) or image.isNull():
            return None
        width, height = image.width(), image.height()
        label = f"图片 {width}×{height}"
        png = image_to_png(image)
        if len(png) > max_image_bytes:
            return Capture(kind="image", summary=label, too_large=True)
        thumb = png
        if width > THUMB_MAX.width() or height > THUMB_MAX.height():
            small = image.scaled(THUMB_MAX, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            thumb = image_to_png(small)
        return Capture(kind="image", text=label, digest=content_digest("image", png),
                       formats={PNG: png, THUMB: thumb}, meta={"w": width, "h": height}, summary=label)
    return None


def build_mime(kind: str, text: str, formats: dict[str, bytes]) -> tuple[QMimeData | None, str]:
    """把一条记录还原成剪贴板数据。返回 (数据, 提示)；数据为 None 时，提示说明原因。"""
    mime = QMimeData()
    if kind == "files":
        paths = [p for p in text.split("\n") if p]
        existing = [p for p in paths if win32.is_remote_path(p) or os.path.exists(p)]
        if not existing:
            return None, "这些文件已经不在原来的位置了"
        mime.setUrls([QUrl.fromLocalFile(p) for p in existing])
        missing = len(paths) - len(existing)
        return mime, (f"有 {missing} 个文件已经不在了，只填入了其余的" if missing else "")
    if kind == "image":
        png = formats.get(PNG)
        image = QImage.fromData(png, "PNG") if png else QImage()
        if image.isNull():
            return None, "图片数据读不出来了"
        mime.setImageData(image)
        return mime, ""
    mime.setText(text)
    if HTML in formats:
        mime.setHtml(formats[HTML].decode("utf-8", "replace"))
    if RTF in formats:
        mime.setData(RTF_MIME, QByteArray(formats[RTF]))
    return mime, ""


def text_mime(text: str) -> QMimeData:
    """只含纯文字的剪贴板数据（比如“复制文件路径”）。"""
    mime = QMimeData()
    mime.setText(text)
    return mime
