"""复制历史的本地存储（SQLite）。

规则：
- 同样的内容再复制一次，不新增记录，只把原记录挪到最前面（格式数据换成最新的）
- 收藏的记录永不自动删除，不计入条数上限，“关机清除”时也保留；收藏可以放进项目里分类
- 从面板里填入时，记录的位置不变（连续填入时位置稳定）
- 列表只取开头一段做预览；完整内容、格式和图片数据在真正填入时才读取

记录有三种：
- text   文字。text 列是文字本身；带格式时 formats 表里另存 HTML / RTF
- image  图片。text 列是“图片 宽×高”（方便搜索）；formats 表里存 PNG 原图和缩略图
- files  文件。text 列是文件路径，每行一个
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("clipdock")

PREVIEW_CHARS = 600
SCHEMA_VERSION = 3
ALL_FAVORITES = -1  # favorites(ALL_FAVORITES) 表示所有收藏，不分项目

_CREATE = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL DEFAULT 'text',
    text         TEXT    NOT NULL,
    digest       TEXT    NOT NULL UNIQUE,
    source       TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL,
    last_copied  REAL    NOT NULL,
    favorite     INTEGER NOT NULL DEFAULT 0,
    favorited_at REAL,
    project_id   INTEGER,
    use_count    INTEGER NOT NULL DEFAULT 0,
    meta         TEXT    NOT NULL DEFAULT '{}',
    size         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_last_copied ON items(last_copied);
CREATE TABLE IF NOT EXISTS formats (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name    TEXT    NOT NULL,
    data    BLOB    NOT NULL,
    PRIMARY KEY (item_id, name)
);
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 旧版本（0.1/0.2）的 items 表缺这些列，打开时自动补上
_NEW_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'text'",
    "favorite": "INTEGER NOT NULL DEFAULT 0",
    "favorited_at": "REAL",
    "project_id": "INTEGER",
    "meta": "TEXT NOT NULL DEFAULT '{}'",
    "size": "INTEGER NOT NULL DEFAULT 0",
}

# 一条记录占多少字节：文字本身 + 附带的格式数据（图片、HTML 等）
_SIZE_OF = (
    "LENGTH(CAST(text AS BLOB)) + "
    "COALESCE((SELECT SUM(LENGTH(f.data)) FROM formats f WHERE f.item_id = items.id), 0)"
)

_SELECT = (
    "SELECT id, kind, substr(text, 1, ?), length(text), source, created_at, last_copied, "
    "favorite, project_id, use_count, meta FROM items"
)


@dataclass(frozen=True)
class Item:
    id: int
    kind: str           # "text" / "image" / "files"
    preview: str        # text 列开头最多 PREVIEW_CHARS 个字
    length: int         # text 列完整长度
    source: str         # 从哪个程序复制的
    created_at: float   # 第一次复制的时间
    last_copied: float  # 最近一次复制的时间
    favorite: bool
    project_id: int | None
    use_count: int      # 从面板填入/复制过几次
    meta: dict = field(default_factory=dict, compare=False)  # 图片宽高、文件个数、是否带格式等

    @property
    def paths(self) -> list[str]:
        """文件记录的路径（从预览里取，够显示用；填入时用 text_of 拿完整列表）。"""
        return [p for p in self.preview.split("\n") if p] if self.kind == "files" else []


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    count: int
    created_at: float


def _clean(text: str) -> str:
    """把无法编码的孤立代理字符换成 �，否则 SQLite 存不进去。"""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def text_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_digest(kind: str, data: bytes) -> str:
    return f"{kind}:{hashlib.sha1(data).hexdigest()}"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_conditions(query: str) -> tuple[list[str], list[object]]:
    """每个关键词都要命中内容或来源程序。"""
    conditions: list[str] = []
    params: list[object] = []
    for term in query.split():
        pattern = f"%{_escape_like(term)}%"
        conditions.append("(text LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    return conditions, params


class HistoryStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA secure_delete = ON")  # 删除的内容在磁盘上也抹掉
        # 删掉记录后把空出来的空间还给磁盘（必须在建表之前设置才对新数据库生效）
        self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._conn.executescript(_CREATE)
        self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(items)")}
        with self._conn:
            for name, ddl in _NEW_COLUMNS.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
            if version < 2 and "pinned" in columns:
                # 旧版的“固定”合并进“收藏”
                self._conn.execute(
                    "UPDATE items SET favorite = 1, favorited_at = COALESCE(pinned_at, last_copied) "
                    "WHERE pinned = 1"
                )
            if version < 3:  # 记下每条占多少空间，按占用上限清理时用
                self._conn.execute(f"UPDATE items SET size = {_SIZE_OF}")
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if self._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0:
            # 旧版建的数据库还没开自动回收空间：整理一次（旧版只有文字，很快）
            try:
                self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                self._conn.execute("VACUUM")
            except sqlite3.Error as exc:
                log.warning("整理数据库失败：%s", exc)

    def _release_space(self) -> None:
        try:
            self._conn.execute("PRAGMA incremental_vacuum").fetchall()
        except sqlite3.Error as exc:
            log.warning("回收数据库空间失败：%s", exc)

    def close(self) -> None:
        self._conn.close()

    # ---- 写入 ----

    def add(
        self,
        text: str,
        source: str = "",
        now: float | None = None,
        *,
        kind: str = "text",
        formats: dict[str, bytes] | None = None,
        meta: dict | None = None,
        digest: str | None = None,
    ) -> Item:
        """记录一次复制。内容已存在时只更新时间（挪到最前面）。

        formats 为 None 表示不动格式数据；传字典（哪怕是空的）表示换成这一份。
        """
        text = _clean(text)
        now = time.time() if now is None else now
        digest = digest or text_digest(text)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE items SET last_copied = ?, kind = ?, text = ?, meta = ?, "
                "source = CASE WHEN ? != '' THEN ? ELSE source END WHERE digest = ?",
                (now, kind, text, meta_json, source, source, digest),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO items (kind, text, digest, source, created_at, last_copied, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (kind, text, digest, source, now, now, meta_json),
                )
            item_id = self._conn.execute("SELECT id FROM items WHERE digest = ?", (digest,)).fetchone()[0]
            if formats is not None:
                self._conn.execute("DELETE FROM formats WHERE item_id = ?", (item_id,))
                self._conn.executemany(
                    "INSERT INTO formats (item_id, name, data) VALUES (?, ?, ?)",
                    [(item_id, name, sqlite3.Binary(data)) for name, data in formats.items()],
                )
            self._conn.execute(f"UPDATE items SET size = {_SIZE_OF} WHERE id = ?", (item_id,))
        return self.get(item_id)

    def set_favorite(self, item_id: int, favorite: bool) -> None:
        with self._conn:
            if favorite:
                self._conn.execute(
                    "UPDATE items SET favorite = 1, favorited_at = ? WHERE id = ? AND favorite = 0",
                    (time.time(), item_id),
                )
            else:
                self._conn.execute(
                    "UPDATE items SET favorite = 0, favorited_at = NULL, project_id = NULL WHERE id = ?",
                    (item_id,),
                )

    def move_to_project(self, item_id: int, project_id: int | None) -> None:
        """放进项目（project_id 为 None 表示“未分类”）。没收藏的会顺便收藏。"""
        with self._conn:
            self._conn.execute(
                "UPDATE items SET favorite = 1, favorited_at = COALESCE(favorited_at, ?), project_id = ? "
                "WHERE id = ?",
                (time.time(), project_id, item_id),
            )

    def mark_used(self, item_id: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE items SET use_count = use_count + 1 WHERE id = ?", (item_id,))

    def delete(self, item_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._release_space()

    def clear(self, keep_favorites: bool = True) -> int:
        with self._conn:
            sql = "DELETE FROM items WHERE favorite = 0" if keep_favorites else "DELETE FROM items"
            removed = self._conn.execute(sql).rowcount
        self._release_space()
        return removed

    def usage(self) -> tuple[int, int]:
        """未收藏的历史：(条数, 占用字节数)。收藏的不算在存储上限里。"""
        count, size = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items WHERE favorite = 0"
        ).fetchone()
        return int(count), int(size)

    def _over_limit(self, max_items: int | None, max_bytes: int | None) -> list[int]:
        """超出上限、要删掉的记录：从最旧的开始。max_items / max_bytes 为 None 表示不限。
        最新的一条总会留下（哪怕它一条就超过了占用上限）。收藏的不算。"""
        count, size = self.usage()
        if (max_items is None or count <= max_items) and (max_bytes is None or size <= max_bytes):
            return []
        rows = self._conn.execute(
            "SELECT id, size FROM items WHERE favorite = 0 ORDER BY last_copied DESC, id DESC"
        )
        doomed, total = [], 0
        for index, (item_id, item_size) in enumerate(rows):
            total += item_size
            too_many = max_items is not None and index >= max_items
            too_big = max_bytes is not None and total > max_bytes and index > 0
            if too_many or too_big:
                doomed.append(item_id)
        return doomed

    def excess(self, max_items: int | None, max_bytes: int | None = None) -> int:
        """按这样的上限，要删掉几条（只算，不删）。"""
        return len(self._over_limit(max_items, max_bytes))

    def trim(self, max_items: int | None, max_bytes: int | None = None) -> int:
        """历史超出上限时从最旧的删起：最多 max_items 条、合计最多 max_bytes 字节（None 表示不限）。
        返回删掉的条数。收藏的内容不受影响。"""
        doomed = self._over_limit(max_items, max_bytes)
        if not doomed:
            return 0
        with self._conn:
            self._conn.executemany("DELETE FROM items WHERE id = ?", [(item_id,) for item_id in doomed])
        self._release_space()
        return len(doomed)

    # ---- 项目 ----

    def projects(self) -> list[Project]:
        rows = self._conn.execute(
            "SELECT p.id, p.name, "
            "(SELECT COUNT(*) FROM items i WHERE i.favorite = 1 AND i.project_id = p.id), p.created_at "
            "FROM projects p ORDER BY p.created_at, p.id"
        )
        return [Project(id=r[0], name=r[1], count=r[2], created_at=r[3]) for r in rows]

    def project(self, project_id: int) -> Project | None:
        return next((p for p in self.projects() if p.id == project_id), None)

    def create_project(self, name: str) -> Project:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO projects (name, created_at) VALUES (?, ?)", (name.strip(), time.time())
            )
        return self.project(cur.lastrowid)

    def rename_project(self, project_id: int, name: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name.strip(), project_id))

    def delete_project(self, project_id: int) -> None:
        """删除项目；里面的收藏不删，移到“未分类”。"""
        with self._conn:
            self._conn.execute("UPDATE items SET project_id = NULL WHERE project_id = ?", (project_id,))
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ---- 读取 ----

    def history(self, query: str = "", limit: int | None = None) -> list[Item]:
        """所有记录，最近复制的在前。"""
        conditions, params = _search_conditions(query)
        sql = _SELECT + (" WHERE " + " AND ".join(conditions) if conditions else "")
        sql += " ORDER BY last_copied DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._to_item(r) for r in self._conn.execute(sql, [PREVIEW_CHARS, *params])]

    def favorites(self, project_id: int | None = None, query: str = "", limit: int | None = None) -> list[Item]:
        """收藏：project_id 为 None 表示未分类的，ALL_FAVORITES 表示全部。最近收藏的在前。"""
        conditions, params = _search_conditions(query)
        conditions.insert(0, "favorite = 1")
        if project_id is None:
            conditions.insert(1, "project_id IS NULL")
        elif project_id != ALL_FAVORITES:
            conditions.insert(1, "project_id = ?")
            params.insert(0, project_id)
        sql = f"{_SELECT} WHERE {' AND '.join(conditions)} ORDER BY favorited_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._to_item(r) for r in self._conn.execute(sql, [PREVIEW_CHARS, *params])]

    def get(self, item_id: int) -> Item | None:
        row = self._conn.execute(f"{_SELECT} WHERE id = ?", (PREVIEW_CHARS, item_id)).fetchone()
        return self._to_item(row) if row else None

    def text_of(self, item_id: int) -> str | None:
        row = self._conn.execute("SELECT text FROM items WHERE id = ?", (item_id,)).fetchone()
        return row[0] if row else None

    def formats_of(self, item_id: int) -> dict[str, bytes]:
        rows = self._conn.execute("SELECT name, data FROM formats WHERE item_id = ?", (item_id,))
        return {name: bytes(data) for name, data in rows}

    def format_of(self, item_id: int, name: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT data FROM formats WHERE item_id = ? AND name = ?", (item_id, name)
        ).fetchone()
        return bytes(row[0]) if row else None

    def counts(self) -> tuple[int, int]:
        """(总条数, 收藏条数)"""
        total, favorites = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(favorite), 0) FROM items"
        ).fetchone()
        return int(total), int(favorites)

    # ---- 小设置（比如上次开机时间） ----

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @staticmethod
    def _to_item(row: tuple) -> Item:
        try:
            meta = json.loads(row[10]) if row[10] else {}
        except ValueError:
            meta = {}
        return Item(
            id=row[0],
            kind=row[1] or "text",
            preview=row[2],
            length=row[3],
            source=row[4],
            created_at=row[5],
            last_copied=row[6],
            favorite=bool(row[7]),
            project_id=row[8],
            use_count=row[9],
            meta=meta if isinstance(meta, dict) else {},
        )
