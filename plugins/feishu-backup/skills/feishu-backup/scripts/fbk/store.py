"""SQLite 清单：记录每个文件的备份状态，支持中断后续跑。线程安全。"""
import json
import sqlite3
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional, Set

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  token TEXT PRIMARY KEY, type TEXT, source TEXT, remote_dir TEXT, name TEXT,
  remote_mtime INTEGER, local_path TEXT, sha256 TEXT, status TEXT,
  also_seen TEXT, last_backup_at INTEGER);
CREATE TABLE IF NOT EXISTS blobs (sha256 TEXT PRIMARY KEY, local_path TEXT);
CREATE TABLE IF NOT EXISTS chat_cursor (chat_id TEXT PRIMARY KEY, last_ts INTEGER);
CREATE TABLE IF NOT EXISTS attachments (
  file_key TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT, sha256 TEXT,
  local_path TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS links (
  token TEXT PRIMARY KEY, kind TEXT, status TEXT, hint TEXT, sender TEXT,
  chat_name TEXT, sent_at INTEGER, first_seen INTEGER);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started INTEGER, finished INTEGER, ok INTEGER, summary TEXT);
"""

_ITEM_COLS = {"type", "source", "remote_dir", "name", "remote_mtime", "local_path",
              "sha256", "status", "also_seen", "last_backup_at"}
_ATT_COLS = {"chat_id", "message_id", "sha256", "local_path", "status", "file_name", "chat_name", "sent_at"}
_LINK_COLS = {"kind", "status", "hint", "sender", "chat_name", "sent_at", "first_seen", "url"}


# 备份根目录下程序自己的顶层目录；旧版清单里的绝对路径按它们定位相对部分
OWNED_TOP_DIRS = ("我的空间", "知识库", "共享给我", "群聊附件")
_PATH_TABLES = (("items", "token"), ("attachments", "file_key"), ("blobs", "sha256"))
SCHEMA_VERSION = 2


def _strip_old_root(path: str) -> Optional[str]:
    """绝对路径 → 从第一个程序自有顶层目录开始的相对路径；找不到返回 None。"""
    parts = PurePosixPath(path).parts
    for i, seg in enumerate(parts):
        if i > 0 and seg in OWNED_TOP_DIRS:
            return "/".join(parts[i:])
    return None


class Store:
    """local_path（items / attachments / blobs）一律存相对备份根目录的路径，读出时按当前根目录拼回绝对路径。
    这样整个备份目录被复制或搬到别处（换电脑、换外接盘）后，清单依然对得上，不会重新下载、也不会搬动旧副本。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        if not db_path.parent.parent.exists():
            raise FileNotFoundError(str(db_path.parent.parent))
        self.root = db_path.parent.parent
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 旧库迁移：links 表补 url 列
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(links)")}
            acols = {r[1] for r in self._conn.execute("PRAGMA table_info(attachments)")}
            for col, typ in (("file_name", "TEXT"), ("chat_name", "TEXT"), ("sent_at", "INTEGER")):
                if col not in acols:
                    self._conn.execute("ALTER TABLE attachments ADD COLUMN %s %s" % (col, typ))
            if "url" not in cols:
                self._conn.execute("ALTER TABLE links ADD COLUMN url TEXT")
            self._migrate_abs_paths()
            self._conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            self._conn.commit()

    def _migrate_abs_paths(self):
        """旧清单里的绝对路径改成相对路径（幂等，每次打开都检查）：先按当前根目录，
        再按第一个程序自有顶层目录（我的空间/知识库/共享给我/群聊附件）截掉旧根目录。截不出的原样保留。"""
        for table, pk in _PATH_TABLES:
            rows = self._conn.execute(
                "SELECT %s, local_path FROM %s WHERE local_path LIKE '/%%'" % (pk, table)).fetchall()
            for key, p in rows:
                rel = self._rel_under_root(p) or _strip_old_root(p)
                if rel is not None:
                    self._conn.execute("UPDATE %s SET local_path=? WHERE %s=?" % (table, pk), (rel, key))

    # ---- 路径：存相对、读绝对 ----
    def _rel_under_root(self, p) -> Optional[str]:
        p = Path(p)
        for base in (self.root, self.root.resolve()):
            try:
                rel = p.relative_to(base)
            except ValueError:
                continue
            return rel.as_posix() if rel.parts else None
        return None

    def to_stored(self, p) -> Optional[str]:
        if p is None:
            return None
        if not Path(p).is_absolute():
            return Path(p).as_posix()
        return self._rel_under_root(p) or str(p)

    def to_abs(self, stored) -> Optional[str]:
        """清单里的路径 → 按当前备份根目录拼成的绝对路径（字符串）。"""
        if not stored:
            return stored
        if stored.startswith("/"):
            return stored
        return str(self.root.joinpath(*PurePosixPath(stored).parts))

    def _resolved(self, row):
        if row is None:
            return None
        d = dict(row)
        if "local_path" in d:
            d["local_path"] = self.to_abs(d["local_path"])
        return d

    def _norm(self, k, v):
        if k in ("remote_dir", "also_seen") and not isinstance(v, str) and v is not None:
            return json.dumps(list(v), ensure_ascii=False)
        if k == "local_path" and v is not None:
            return self.to_stored(v)
        return v

    def close(self):
        with self._lock:
            self._conn.close()

    def _write(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _read(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _upsert(self, table, pk, pk_val, cols, allowed):
        bad = set(cols) - allowed
        if bad:
            raise ValueError("未知字段: %s" % ", ".join(sorted(bad)))
        vals = {k: self._norm(k, v) for k, v in cols.items()}
        names = [pk] + list(vals)
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(names), ",".join("?" * len(names)))
        if vals:
            sql += " ON CONFLICT(%s) DO UPDATE SET %s" % (
                pk, ",".join("%s=excluded.%s" % (k, k) for k in vals))
        else:
            sql += " ON CONFLICT(%s) DO NOTHING" % pk
        self._write(sql, [pk_val] + list(vals.values()))

    # items
    def get_item(self, token) -> Optional[dict]:
        rows = self._read("SELECT * FROM items WHERE token=?", (token,))
        return self._resolved(rows[0]) if rows else None

    def upsert_item(self, **cols):
        token = cols.pop("token")
        self._upsert("items", "token", token, cols, _ITEM_COLS)

    def all_tokens(self, statuses=("ok",)) -> Set[str]:
        statuses = tuple(statuses)
        if not statuses:
            return set()
        q = "SELECT token FROM items WHERE status IN (%s)" % ",".join("?" * len(statuses))
        return {r[0] for r in self._read(q, statuses)}

    def set_status(self, token, status):
        self._write("UPDATE items SET status=? WHERE token=?", (status, token))

    # blobs
    def find_blob(self, sha) -> Optional[Path]:
        rows = self._read("SELECT local_path FROM blobs WHERE sha256=?", (sha,))
        return Path(self.to_abs(rows[0][0])) if rows and rows[0][0] else None

    def add_blob(self, sha, path):
        self._write("INSERT OR REPLACE INTO blobs (sha256, local_path) VALUES (?,?)", (sha, self.to_stored(path)))

    # chat cursor
    def get_cursor(self, chat_id) -> Optional[int]:
        rows = self._read("SELECT last_ts FROM chat_cursor WHERE chat_id=?", (chat_id,))
        return rows[0][0] if rows else None

    def set_cursor(self, chat_id, ts):
        self._write("INSERT OR REPLACE INTO chat_cursor (chat_id, last_ts) VALUES (?,?)", (chat_id, int(ts)))

    # attachments
    def upsert_attachment(self, file_key, **cols):
        self._upsert("attachments", "file_key", file_key, cols, _ATT_COLS)

    def attachments_by_status(self, status):
        return [self._resolved(r) for r in
                self._read("SELECT * FROM attachments WHERE status=? ORDER BY file_key", (status,))]

    def get_attachment(self, file_key) -> Optional[dict]:
        rows = self._read("SELECT * FROM attachments WHERE file_key=?", (file_key,))
        return self._resolved(rows[0]) if rows else None

    # links
    def upsert_link(self, token, **cols):
        with self._lock:
            old = self._conn.execute("SELECT first_seen FROM links WHERE token=?", (token,)).fetchone()
            fs = cols.get("first_seen")
            if old is not None and old[0] is not None:
                cols["first_seen"] = old[0] if fs is None else min(old[0], fs)
            self._upsert("links", "token", token, cols, _LINK_COLS)

    def links(self):
        return self._read("SELECT * FROM links ORDER BY token")

    # runs
    def start_run(self) -> int:
        return self._write("INSERT INTO runs (started) VALUES (?)", (int(time.time()),)).lastrowid

    def finish_run(self, run_id, ok: bool, summary: dict):
        self._write("UPDATE runs SET finished=?, ok=?, summary=? WHERE id=?",
                    (int(time.time()), 1 if ok else 0,
                     json.dumps(summary, ensure_ascii=False), run_id))

    def last_run(self):
        """最近一次已结束（finished 非空）的运行；没有返回 None。"""
        rows = self._read("SELECT id, started, finished, ok, summary FROM runs "
                          "WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def last_success(self) -> Optional[int]:
        rows = self._read("SELECT MAX(finished) FROM runs WHERE ok=1")
        return rows[0][0] if rows and rows[0][0] is not None else None
