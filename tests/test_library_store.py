import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import library_store


class LibraryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "library"
        self.store = library_store.LibraryStore(self.root)

    def test_arabic_and_hausa_text_round_trip(self):
        source = (
            "  مرحبًا بالعالم\r\n\r\n"
            "Sannu duniya — ƙwallo, ɗalibi, and ƴanci.  "
        )
        expected = (
            "مرحبًا بالعالم\n\n"
            "Sannu duniya — ƙwallo, ɗalibi, and ƴanci."
        )

        item = self.store.add_text(source, "العربية da Hausa")

        self.assertEqual(self.store.read_text(item["id"]), expected)
        self.assertEqual(item["word_count"], len(expected.split()))
        self.assertEqual(item["size_bytes"], len(expected.encode("utf-8")))
        self.assertEqual(
            Path(self.store.resolve_item_path(item)).read_bytes(),
            expected.encode("utf-8"),
        )

    def test_imported_file_survives_deleting_original(self):
        original = Path(self.temporary_directory.name) / "uploaded.pdf"
        payload = b"%PDF-1.7\nPiper Reader library snapshot\n%%EOF\n"
        original.write_bytes(payload)

        item = self.store.add_file(original, word_count=4)
        stored_path = Path(self.store.resolve_item_path(item))
        original.unlink()

        self.assertFalse(original.exists())
        self.assertTrue(stored_path.is_file())
        self.assertEqual(stored_path.read_bytes(), payload)
        self.assertEqual(
            Path(self.store.resolve_item_path(item["id"])).read_bytes(), payload)

    def test_normalized_text_and_same_file_bytes_are_deduplicated(self):
        first_text = self.store.add_text("  One\r\nTwo  ", "First title")
        second_text = self.store.add_text("One\nTwo", "Updated title")

        first_file = Path(self.temporary_directory.name) / "first.md"
        second_file = Path(self.temporary_directory.name) / "renamed.md"
        first_file.write_bytes(b"identical markdown")
        second_file.write_bytes(b"identical markdown")
        first_file_item = self.store.add_file(first_file)
        second_file_item = self.store.add_file(second_file)

        self.assertEqual(first_text["id"], second_text["id"])
        self.assertEqual(second_text["title"], "Updated title")
        self.assertEqual(first_file_item["id"], second_file_item["id"])
        self.assertEqual(second_file_item["original_name"], "renamed.md")
        self.assertEqual(self.store.count(), 2)
        objects = [path for path in (self.root / "objects").rglob("*")
                   if path.is_file()]
        self.assertEqual(len(objects), 2)

    def test_file_import_uses_injected_shared_binary_opener(self):
        original = Path(self.temporary_directory.name) / "locked.docx"
        original.write_bytes(b"bytes visible through an ordinary opener")
        shared_snapshot = b"bytes returned by the shared-read helper"
        calls = []

        def open_binary(path):
            calls.append(os.path.abspath(path))
            return io.BytesIO(shared_snapshot)

        store = library_store.LibraryStore(
            Path(self.temporary_directory.name) / "shared-library",
            open_binary=open_binary,
        )
        with original.open("rb") as held_open:
            item = store.add_file(original)
            self.assertFalse(held_open.closed)

        self.assertEqual(calls, [os.path.abspath(original)])
        self.assertEqual(
            Path(store.resolve_item_path(item)).read_bytes(), shared_snapshot)

    def test_search_and_recent_or_title_sorting(self):
        with mock.patch.object(library_store.time, "time", side_effect=[100.0, 200.0, 300.0]):
            self.store.add_text("plain first content", "Zulu")
            self.store.add_text("contains a searchable needle", "Bravo")
            self.store.add_text("plain third content", "alpha")

        self.assertEqual(
            [item["title"] for item in self.store.list_items(sort="recent")],
            ["alpha", "Bravo", "Zulu"],
        )
        self.assertEqual(
            [item["title"] for item in self.store.list_items(sort="title")],
            ["alpha", "Bravo", "Zulu"],
        )
        self.assertEqual(
            [item["title"] for item in self.store.list_items(search="needle")],
            ["Bravo"],
        )
        self.assertEqual(
            [item["title"] for item in self.store.list_items(search="BRAVO")],
            ["Bravo"],
        )
        # SQL wildcard characters in a search are literals, not match-all input.
        self.assertEqual(self.store.list_items(search="%"), [])

    def test_remove_deletes_managed_copy_but_preserves_original(self):
        original = Path(self.temporary_directory.name) / "keep-me.txt"
        payload = b"The source file must never be modified or deleted."
        original.write_bytes(payload)
        item = self.store.add_file(original)
        stored_path = Path(self.store.resolve_item_path(item))

        removed = self.store.remove(item["id"])

        self.assertEqual(removed["id"], item["id"])
        self.assertEqual(original.read_bytes(), payload)
        self.assertFalse(stored_path.exists())
        self.assertEqual(self.store.count(), 0)

    def test_resolve_rejects_database_path_traversal(self):
        outside = Path(self.temporary_directory.name) / "outside.txt"
        outside.write_text("must not be exposed", encoding="utf-8")
        item = self.store.add_text("safe text", "Safe")
        with closing(sqlite3.connect(self.store.database_path)) as connection, connection:
            connection.execute(
                "UPDATE library_items SET blob_relpath=? WHERE id=?",
                ("../outside.txt", item["id"]),
            )

        with self.assertRaisesRegex(
                library_store.LibraryError, "outside local storage"):
            self.store.resolve_item_path(item["id"])
        self.assertEqual(outside.read_text(encoding="utf-8"),
                         "must not be exposed")

    def test_newer_database_schema_is_rejected_without_modification(self):
        newer_root = Path(self.temporary_directory.name) / "newer-library"
        newer_root.mkdir()
        database = newer_root / "library.db"
        future_version = library_store.SCHEMA_VERSION + 1
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(f"PRAGMA user_version = {future_version}")

        with self.assertRaisesRegex(
                library_store.LibraryError,
                f"Library format {future_version} is newer"):
            library_store.LibraryStore(newer_root)

        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                future_version,
            )
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        self.assertEqual(tables, [])


if __name__ == "__main__":
    unittest.main()
