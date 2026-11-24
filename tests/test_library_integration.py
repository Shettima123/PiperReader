import importlib.machinery
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = importlib.machinery.SourceFileLoader(
    "piper_reader_library_integration",
    str(PROJECT_ROOT / "piper_reader.pyw"),
).load_module()


class _ImmediateThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class _ImmediateRoot:
    def after(self, _delay, callback, *args):
        return callback(*args)

    def clipboard_get(self):
        return "manual clipboard text"


class LibraryReaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = APP.LibraryStore(
            os.path.join(self.tempdir.name, "library"))

    def tearDown(self):
        self.tempdir.cleanup()

    def _bare_app(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.library_store = self.store
        app.library_error = None
        app.root = _ImmediateRoot()
        return app

    def test_successful_plain_text_import_is_automatically_remembered(self):
        source = os.path.join(self.tempdir.name, "lesson.txt")
        Path(source).write_text("Sannu duniya. مرحبا بالعالم.", encoding="utf-8")
        app = self._bare_app()
        app.viewer = None
        app._close_library = mock.Mock()
        app._begin_content_load = mock.Mock(return_value=19)
        app._ocr_profile_snapshot = mock.Mock(return_value={})
        app._content_load_is_current = mock.Mock(return_value=True)
        app._load_status = mock.Mock()
        app._show_text_document = mock.Mock()
        app._finish_load_error = mock.Mock()

        with mock.patch.object(APP.threading, "Thread", _ImmediateThread):
            APP.ReaderApp._open_file_path(app, source)

        self.assertEqual(self.store.count(), 1)
        item = self.store.list_items()[0]
        self.assertEqual(item["title"], "lesson.txt")
        shown = app._show_text_document.call_args.args
        self.assertEqual(shown[1:3], ("lesson.txt", 19))
        self.assertEqual(shown[3], item["id"])
        self.assertIsNone(shown[4])

    def test_open_saved_text_dispatches_to_the_text_reader(self):
        item = self.store.add_text("Karatu. قراءة.", "Bilingual lesson")
        app = self._bare_app()
        app._close_library = mock.Mock()
        app._begin_content_load = mock.Mock(return_value=7)
        app._show_text_document = mock.Mock()

        APP.ReaderApp._open_library_item(app, item["id"])

        app._close_library.assert_called_once()
        app._show_text_document.assert_called_once_with(
            "Karatu. قراءة.", "Bilingual lesson", 7,
            library_item_id=item["id"])

    def test_file_reopens_from_managed_copy_after_original_is_deleted(self):
        source = os.path.join(self.tempdir.name, "material.md")
        Path(source).write_text("A durable lesson", encoding="utf-8")
        item = self.store.add_file(source, title="Material")
        os.remove(source)
        app = self._bare_app()
        app._close_library = mock.Mock()
        app._open_file_path = mock.Mock()

        APP.ReaderApp._open_library_item(app, item["id"])

        managed_path = self.store.resolve_item_path(item)
        app._open_file_path.assert_called_once_with(
            managed_path, remember=False, library_item_id=item["id"],
            display_name="Material")

    def test_remove_selection_never_deletes_the_original_file(self):
        source = os.path.join(self.tempdir.name, "original.txt")
        Path(source).write_text("Keep the original", encoding="utf-8")
        item = self.store.add_file(source)
        app = self._bare_app()
        app.current_library_item_id = None
        app._selected_library_id = mock.Mock(return_value=item["id"])
        app._refresh_library_view = mock.Mock()
        app.set_status = mock.Mock()

        with mock.patch.object(APP.messagebox, "askyesno", return_value=True):
            APP.ReaderApp._remove_library_selection(app)

        self.assertTrue(os.path.isfile(source))
        self.assertEqual(self.store.count(), 0)

    def test_manual_clipboard_read_does_not_write_to_library(self):
        app = self._bare_app()
        app.library_store = mock.Mock(wraps=self.store)
        app.speak = mock.Mock()
        app.set_status = mock.Mock()

        APP.ReaderApp.read_clipboard(app)

        app.speak.assert_called_once_with("manual clipboard text")
        app.library_store.add_text.assert_not_called()
        app.library_store.add_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
