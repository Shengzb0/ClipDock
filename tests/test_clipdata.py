"""剪贴板数据 ↔ 记录 的转换测试（用 QMimeData 模拟各种程序放进剪贴板的内容）。"""
from PySide6.QtCore import QByteArray, QMimeData, QUrl
from PySide6.QtGui import QColor, QImage

from clipdock import clipdata
from clipdock.clipdata import HTML, PNG, RTF, RTF_MIME, THUMB, build_mime, capture

MAX_CHARS, MAX_IMAGE = 200_000, 30_000_000


def make_image(w, h, color="#3366cc"):
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    return image


def test_plain_text(qapp):
    mime = QMimeData()
    mime.setText("张三")
    cap = capture(mime, MAX_CHARS, MAX_IMAGE)
    assert cap.kind == "text" and cap.text == "张三" and cap.formats == {} and cap.meta == {}
    assert cap.summary == "「张三」"


def test_rich_text_keeps_html_and_rtf_and_round_trips(qapp):
    mime = QMimeData()
    mime.setText("加粗的字")
    mime.setHtml("<b>加粗的字</b>")
    mime.setData(RTF_MIME, QByteArray(b"{\\rtf1 \\b bold}"))
    cap = capture(mime, MAX_CHARS, MAX_IMAGE)
    assert cap.kind == "text" and cap.meta == {"rich": True}
    assert b"<b>" in cap.formats[HTML] and cap.formats[RTF] == b"{\\rtf1 \\b bold}"
    back, warning = build_mime(cap.kind, cap.text, cap.formats)
    assert warning == "" and back.text() == "加粗的字" and "<b>" in back.html()
    assert bytes(back.data(RTF_MIME)) == b"{\\rtf1 \\b bold}"


def test_text_wins_over_image(qapp):
    # Excel 复制单元格时既有文字又有一张图片：按文字记录
    mime = QMimeData()
    mime.setText("A1\tB1")
    mime.setImageData(make_image(40, 20))
    assert capture(mime, MAX_CHARS, MAX_IMAGE).kind == "text"


def test_whitespace_only_or_empty_is_not_recorded(qapp):
    mime = QMimeData()
    mime.setText("   \n ")
    assert capture(mime, MAX_CHARS, MAX_IMAGE) is None
    assert capture(QMimeData(), MAX_CHARS, MAX_IMAGE) is None
    assert capture(None, MAX_CHARS, MAX_IMAGE) is None


def test_image_with_thumbnail_and_round_trip(qapp):
    mime = QMimeData()
    mime.setImageData(make_image(1200, 600))
    cap = capture(mime, MAX_CHARS, MAX_IMAGE)
    assert cap.kind == "image" and cap.meta == {"w": 1200, "h": 600} and cap.summary == "图片 1200×600"
    thumb = QImage.fromData(cap.formats[THUMB], "PNG")
    assert thumb.width() <= 560 and thumb.height() <= 144
    back, warning = build_mime(cap.kind, cap.text, cap.formats)
    image = back.imageData()
    assert warning == "" and image.width() == 1200 and image.height() == 600
    assert QImage(image).pixelColor(10, 10) == QColor("#3366cc")


def test_image_too_large(qapp):
    mime = QMimeData()
    mime.setImageData(make_image(300, 300))
    cap = capture(mime, MAX_CHARS, max_image_bytes=10)
    assert cap.too_large and cap.summary == "图片 300×300"


def test_files_and_image_plus_files(qapp, tmp_path):
    a, b = tmp_path / "报告.docx", tmp_path / "数据.xlsx"
    a.write_text("a")
    b.write_text("b")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(a)), QUrl.fromLocalFile(str(b))])
    cap = capture(mime, MAX_CHARS, MAX_IMAGE)
    assert cap.kind == "files" and cap.meta == {"count": 2} and cap.summary == "2 个文件"
    assert cap.text.split("\n") == [str(a), str(b)]
    # 只剩一个文件时只填入剩下的
    b.unlink()
    back, warning = build_mime(cap.kind, cap.text, cap.formats)
    assert [u.toLocalFile() for u in back.urls()] == [str(a)] and "1 个文件" in warning
    a.unlink()
    assert build_mime(cap.kind, cap.text, cap.formats) == (None, "这些文件已经不在原来的位置了")
    # 聊天软件复制图片：同时带图片和文件时按图片处理
    mime2 = QMimeData()
    mime2.setUrls([QUrl.fromLocalFile(str(tmp_path / "x.png"))])
    mime2.setImageData(make_image(10, 10))
    assert capture(mime2, MAX_CHARS, MAX_IMAGE).kind == "image"


def test_single_file_summary_and_too_long_text(qapp):
    assert clipdata.describe_files(["C:\\Users\\me\\report.docx"]) == "report.docx"
    assert clipdata.describe_text("一二三四五六七八九十一二三四五六七八") == "「一二三四五六七八九十一二三四五六…」"
    mime = QMimeData()
    mime.setText("x" * 50)
    cap = capture(mime, 10, MAX_IMAGE)
    assert cap.too_large and cap.summary == "50 字"


def test_broken_image_record(qapp):
    assert build_mime("image", "图片", {PNG: b"not a png"}) == (None, "图片数据读不出来了")
