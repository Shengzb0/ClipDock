import sqlite3

from clipdock.store import ALL_FAVORITES, PREVIEW_CHARS, HistoryStore, content_digest


def make_store(tmp_path):
    return HistoryStore(tmp_path / "h.db")


def test_add_and_history_order(tmp_path):
    s = make_store(tmp_path)
    s.add("第一条", "chrome", now=1)
    s.add("第二条", "微信", now=2)
    s.add("第三条", "", now=3)
    assert [i.preview for i in s.history()] == ["第三条", "第二条", "第一条"]
    assert s.counts() == (3, 0)
    assert s.history()[0].kind == "text"


def test_duplicate_moves_to_top_and_replaces_formats(tmp_path):
    s = make_store(tmp_path)
    a = s.add("hello", "chrome", now=1, formats={"text/html": b"<b>hello</b>"}, meta={"rich": True})
    s.add("world", "code", now=2)
    b = s.add("hello", "", now=3, formats={})  # 来源未知时保留原来源；格式换成新的（这次没有）
    assert a.id == b.id and b.source == "chrome"
    assert [i.preview for i in s.history()] == ["hello", "world"]
    assert s.formats_of(a.id) == {}
    c = s.add("hello", "Edge", now=4)  # formats=None：格式不动
    assert c.source == "Edge" and c.meta == {}


def test_favorites_projects_and_trim(tmp_path):
    s = make_store(tmp_path)
    ids = [s.add(f"t{i}", now=i).id for i in range(10)]
    s.set_favorite(ids[2], True)
    work = s.create_project("课程作业")
    s.move_to_project(ids[5], work.id)  # 没收藏的放进项目会顺便收藏
    assert [i.preview for i in s.favorites(None)] == ["t2"]
    assert [i.preview for i in s.favorites(work.id)] == ["t5"]
    assert {i.preview for i in s.favorites(ALL_FAVORITES)} == {"t2", "t5"}
    assert s.projects()[0].name == "课程作业" and s.projects()[0].count == 1
    removed = s.trim(3)  # 8 条未收藏，只留最近 3 条
    assert removed == 5
    assert [i.preview for i in s.history()] == ["t9", "t8", "t7", "t5", "t2"]
    assert s.counts() == (5, 2)
    # 取消收藏会离开项目
    s.set_favorite(ids[5], False)
    assert s.favorites(work.id) == [] and s.get(ids[5]).project_id is None


def test_project_rename_delete_keeps_items(tmp_path):
    s = make_store(tmp_path)
    p = s.create_project(" 实习 ")
    assert p.name == "实习"
    a = s.add("a", now=1)
    s.move_to_project(a.id, p.id)
    s.rename_project(p.id, "暑期实习")
    assert s.project(p.id).name == "暑期实习"
    s.delete_project(p.id)
    assert s.projects() == []
    item = s.get(a.id)
    assert item.favorite and item.project_id is None  # 项目删了，收藏还在，回到未分类


def test_clear_keeps_favorites_and_cascades_formats(tmp_path):
    s = make_store(tmp_path)
    a = s.add("a", now=1, formats={"text/html": b"x"})
    b = s.add("b", now=2, formats={"text/html": b"y"})
    s.set_favorite(b.id, True)
    assert s.clear(keep_favorites=True) == 1
    assert [i.preview for i in s.history()] == ["b"]
    assert s.formats_of(a.id) == {}  # 格式数据跟着删掉
    assert s.formats_of(b.id) == {"text/html": b"y"}


def test_search_terms_source_and_escaping(tmp_path):
    s = make_store(tmp_path)
    s.add("学号 20230001", "微信", now=1)
    s.add("邮箱 a_b@x.com", "Chrome", now=2)
    s.add("100% 完成", "Word", now=3)
    s.add("C:\\Users\\me\\report.docx", "资源管理器", now=4, kind="files",
          digest=content_digest("files", b"C:\\Users\\me\\report.docx"))
    assert [i.preview for i in s.history("学号")] == ["学号 20230001"]
    assert [i.preview for i in s.history("chrome 邮箱")] == ["邮箱 a_b@x.com"]
    assert [i.preview for i in s.history("a_b")] == ["邮箱 a_b@x.com"]
    assert [i.preview for i in s.history("%")] == ["100% 完成"]
    assert [i.kind for i in s.history("report")] == ["files"]  # 能按文件名搜
    assert s.history("不存在") == []


def test_kinds_preview_and_paths(tmp_path):
    s = make_store(tmp_path)
    img = s.add("图片 800×600", "Chrome", now=1, kind="image", meta={"w": 800, "h": 600},
                formats={"image/png": b"PNG...", "thumb/png": b"T"}, digest=content_digest("image", b"PNG..."))
    files = s.add("C:\\a.txt\nC:\\b.txt", "资源管理器", now=2, kind="files", meta={"count": 2},
                  digest=content_digest("files", b"C:\\a.txt\nC:\\b.txt"))
    assert img.kind == "image" and img.meta == {"w": 800, "h": 600}
    assert s.format_of(img.id, "thumb/png") == b"T"
    assert files.paths == ["C:\\a.txt", "C:\\b.txt"]
    long_text = "啊" * (PREVIEW_CHARS + 50)
    item = s.add(long_text, now=3)
    assert len(item.preview) == PREVIEW_CHARS and item.length == PREVIEW_CHARS + 50
    assert s.text_of(item.id) == long_text


def test_lone_surrogate_is_replaced(tmp_path):
    s = make_store(tmp_path)
    item = s.add("ab\ud800cd", now=1)
    assert s.text_of(item.id) == "ab\ufffdcd"


def test_settings_and_persistence(tmp_path):
    s = make_store(tmp_path)
    s.add("留下来", now=1)
    s.set_setting("boot_time", "123")
    s.set_setting("boot_time", "456")
    s.close()
    s2 = make_store(tmp_path)
    assert [i.preview for i in s2.history()] == ["留下来"]
    assert s2.get_setting("boot_time") == "456" and s2.get_setting("nope") is None


def test_migrates_v1_database_pinned_becomes_favorite(tmp_path):
    path = tmp_path / "h.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, last_copied REAL NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0, pinned_at REAL, use_count INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO items (text, digest, source, created_at, last_copied, pinned, pinned_at)
        VALUES ('地址', 'd1', '微信', 1, 1, 1, 5), ('学号', 'd2', 'Chrome', 2, 2, 0, NULL);
        """
    )
    conn.commit()
    conn.close()
    s = HistoryStore(path)
    favs = s.favorites(None)
    assert [i.preview for i in favs] == ["地址"] and favs[0].kind == "text"
    assert [i.preview for i in s.history()] == ["学号", "地址"]
    # 迁移后照常写入
    s.add("新内容", now=3, formats={"text/html": b"<i>x</i>"})
    assert s.counts() == (3, 1)
    s.close()
    assert HistoryStore(path).counts() == (3, 1)  # 再打开不会重复迁移


def test_trim_by_bytes_keeps_favorites_and_newest(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    big = b"x" * 1000
    ids = []
    for i in range(5):
        item = store.add(f"图片 {i}", now=100 + i, kind="image", formats={"image/png": big + bytes([i])},
                         digest=content_digest("image", big + bytes([i])))
        ids.append(item.id)
    store.add("纯文字", now=200)
    store.set_favorite(ids[0], True)            # 最旧的一张收藏了：不受限制
    removed = store.trim(500, max_bytes=2500)   # 最多留 2 张左右
    assert removed == 2
    left = {i.preview for i in store.history()}
    assert left == {"纯文字", "图片 4", "图片 3", "图片 0"}
    assert store.trim(500, max_bytes=10) == 1   # 预算再小，最新的一张也留着
    assert {i.preview for i in store.history()} == {"纯文字", "图片 4", "图片 0"}


def test_space_is_released_after_clear(tmp_path):
    path = tmp_path / "h.db"
    store = HistoryStore(path)
    assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    blob = bytes(range(256)) * 4000             # 约 1 MB
    for i in range(5):
        store.add(f"图片 {i}", kind="image", formats={"image/png": blob + bytes([i])},
                  digest=content_digest("image", blob + bytes([i])))
    full = path.stat().st_size
    assert full > 4_000_000
    store.clear()
    assert path.stat().st_size < full / 4


def test_old_database_gets_auto_vacuum(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, text TEXT NOT NULL, digest TEXT UNIQUE, "
                 "source TEXT, created_at REAL, last_copied REAL, pinned INTEGER DEFAULT 0, pinned_at REAL)")
    conn.commit()
    conn.close()
    store = HistoryStore(path)
    assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
