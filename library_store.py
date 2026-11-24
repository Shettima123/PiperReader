"""Durable, private local Library storage for Piper Reader."""

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing


SCHEMA_VERSION = 1
MAX_LIBRARY_FILE_BYTES = 512 * 1024 * 1024
MAX_LIBRARY_TEXT_BYTES = 4 * 1024 * 1024
SUPPORTED_FILE_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
}


class LibraryError(RuntimeError):
    """Raised when a Library item cannot be stored or safely resolved."""


def _clean_title(value, fallback="Untitled"):
    title = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    return (title or fallback)[:240]


def _preview_text(value, limit=180):
    return " ".join(str(value or "").split())[:limit]


class LibraryStore:
    """Content-addressed objects plus a small transactional SQLite index."""

    def __init__(self, root_dir, open_binary=None):
        self.root_dir = os.path.abspath(root_dir)
        self.objects_dir = os.path.join(self.root_dir, "objects")
        self.staging_dir = os.path.join(self.root_dir, ".staging")
        self.database_path = os.path.join(self.root_dir, "library.db")
        self._open_binary = open_binary or (lambda path: open(path, "rb"))
        self._lock = threading.RLock()
        os.makedirs(self.objects_dir, exist_ok=True)
        os.makedirs(self.staging_dir, exist_ok=True)
        self._initialize_database()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_database(self):
        # sqlite3.Connection's context manager commits or rolls back but does
        # not close the handle. Explicit closing matters on Windows, where an
        # open handle keeps library.db locked during cleanup and upgrades.
        with self._lock, closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, SCHEMA_VERSION):
                raise LibraryError(
                    f"Library format {version} is newer than this app supports")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('file', 'text')),
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_opened_at REAL NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    blob_relpath TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    word_count INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    preview TEXT NOT NULL DEFAULT '',
                    UNIQUE(kind, content_sha256, extension)
                );
                CREATE INDEX IF NOT EXISTS library_recent
                    ON library_items(last_opened_at DESC);
                """)
            if version == 0:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _row_dict(row):
        return dict(row) if row is not None else None

    def _managed_object_path(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise LibraryError("Library item has no stored object")
        normalized = relative_path.replace("/", os.sep).replace("\\", os.sep)
        if os.path.isabs(normalized):
            raise LibraryError("Library item points outside local storage")
        candidate = os.path.realpath(os.path.join(self.root_dir, normalized))
        object_root = os.path.realpath(self.objects_dir)
        try:
            within_root = (os.path.normcase(os.path.commonpath(
                [candidate, object_root])) == os.path.normcase(object_root))
        except ValueError:
            within_root = False
        if not within_root:
            raise LibraryError("Library item points outside local storage")
        return candidate

    def _stage_path(self):
        return os.path.join(
            self.staging_dir,
            f"import-{os.getpid()}-{secrets.token_hex(10)}.tmp")

    def _place_staged_object(self, staging_path, digest, extension):
        relative = os.path.join(
            "objects", digest[:2], f"{digest}{extension}")
        destination = self._managed_object_path(relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.exists(destination):
            try:
                os.remove(staging_path)
            except FileNotFoundError:
                pass
        else:
            os.replace(staging_path, destination)
        return relative.replace(os.sep, "/")

    def _write_bytes_object(self, payload, digest, extension):
        staging_path = self._stage_path()
        try:
            with open(staging_path, "xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return self._place_staged_object(staging_path, digest, extension)
        finally:
            try:
                os.remove(staging_path)
            except OSError:
                pass

    def _upsert_item(self, *, kind, title, digest, extension, blob_relpath,
                     original_name="", word_count=0, size_bytes=0,
                     preview=""):
        now = time.time()
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM library_items WHERE kind=? AND "
                "content_sha256=? AND extension=?",
                (kind, digest, extension)).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE library_items SET title=?, updated_at=?, "
                    "last_opened_at=?, original_name=?, word_count=?, "
                    "size_bytes=?, preview=? WHERE id=?",
                    (_clean_title(title), now, now, original_name,
                     max(0, int(word_count)), max(0, int(size_bytes)),
                     _preview_text(preview), row["id"]))
                item_id = row["id"]
            else:
                item_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO library_items "
                    "(id, kind, title, created_at, updated_at, last_opened_at, "
                    "content_sha256, extension, blob_relpath, original_name, "
                    "word_count, size_bytes, preview) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item_id, kind, _clean_title(title), now, now, now,
                     digest, extension, blob_relpath, original_name,
                     max(0, int(word_count)), max(0, int(size_bytes)),
                     _preview_text(preview)))
            return self.get_item(item_id, connection=connection)

    def add_file(self, source_path, title=None, word_count=0):
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise LibraryError("That file is no longer available")
        extension = os.path.splitext(source_path)[1].lower()
        if extension not in SUPPORTED_FILE_EXTENSIONS:
            raise LibraryError("That file type cannot be stored in the Library")
        declared_size = os.path.getsize(source_path)
        if declared_size > MAX_LIBRARY_FILE_BYTES:
            raise LibraryError("That file is larger than the 512 MB Library limit")

        staging_path = self._stage_path()
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with self._open_binary(source_path) as source, open(
                    staging_path, "xb") as destination:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    size_bytes += len(block)
                    if size_bytes > MAX_LIBRARY_FILE_BYTES:
                        raise LibraryError(
                            "That file is larger than the 512 MB Library limit")
                    digest.update(block)
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            digest_hex = digest.hexdigest()
            relative = self._place_staged_object(
                staging_path, digest_hex, extension)
            name = os.path.basename(source_path)
            return self._upsert_item(
                kind="file", title=title or name, digest=digest_hex,
                extension=extension, blob_relpath=relative,
                original_name=name, word_count=word_count,
                size_bytes=size_bytes)
        except LibraryError:
            raise
        except OSError as exc:
            raise LibraryError(f"Could not store that file: {exc}") from exc
        finally:
            try:
                os.remove(staging_path)
            except OSError:
                pass

    def add_text(self, text, title):
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise LibraryError("There is no text to save")
        payload = normalized.encode("utf-8")
        if len(payload) > MAX_LIBRARY_TEXT_BYTES:
            raise LibraryError("That text is larger than the 4 MB Library limit")
        digest = hashlib.sha256(payload).hexdigest()
        relative = self._write_bytes_object(payload, digest, ".txt")
        return self._upsert_item(
            kind="text", title=title, digest=digest, extension=".txt",
            blob_relpath=relative, word_count=len(normalized.split()),
            size_bytes=len(payload), preview=normalized)

    def get_item(self, item_id, connection=None):
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM library_items WHERE id=?", (str(item_id),)
            ).fetchone()
            return self._row_dict(row)
        finally:
            if owns_connection:
                connection.close()

    def list_items(self, search="", sort="recent"):
        search = str(search or "").strip()
        order = ("title COLLATE NOCASE ASC, last_opened_at DESC"
                 if sort == "title" else
                 "last_opened_at DESC, title COLLATE NOCASE ASC")
        query = "SELECT * FROM library_items"
        params = ()
        if search:
            query += " WHERE title LIKE ? ESCAPE '\\' OR preview LIKE ? ESCAPE '\\'"
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params = (f"%{escaped}%", f"%{escaped}%")
        query += f" ORDER BY {order}"
        with self._lock, closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def count(self):
        with self._lock, closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM library_items").fetchone()[0])

    def resolve_item_path(self, item_or_id):
        item = (item_or_id if isinstance(item_or_id, dict)
                else self.get_item(item_or_id))
        if item is None:
            raise LibraryError("That Library item no longer exists")
        path = self._managed_object_path(item.get("blob_relpath"))
        if not os.path.isfile(path):
            raise LibraryError("The stored copy for that item is missing")
        return path

    def read_text(self, item_or_id):
        item = (item_or_id if isinstance(item_or_id, dict)
                else self.get_item(item_or_id))
        if item is None or item.get("kind") != "text":
            raise LibraryError("That Library item is not saved text")
        with open(self.resolve_item_path(item), encoding="utf-8") as stream:
            return stream.read()

    def touch(self, item_id):
        now = time.time()
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE library_items SET last_opened_at=?, updated_at=? "
                "WHERE id=?", (now, now, str(item_id)))
            return self.get_item(item_id, connection=connection)

    def remove(self, item_id):
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM library_items WHERE id=?", (str(item_id),)
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            object_path = self._managed_object_path(item["blob_relpath"])
            connection.execute(
                "DELETE FROM library_items WHERE id=?", (str(item_id),))
            references = int(connection.execute(
                "SELECT COUNT(*) FROM library_items WHERE blob_relpath=?",
                (item["blob_relpath"],)).fetchone()[0])
        if references == 0:
            try:
                os.remove(object_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise LibraryError(
                    f"The item was removed, but its stored copy could not be deleted: {exc}") from exc
        return item
