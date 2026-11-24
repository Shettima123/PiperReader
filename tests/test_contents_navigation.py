import importlib.machinery
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

APP = importlib.machinery.SourceFileLoader(
    'reader_contents_tests', str(Path(__file__).resolve().parents[1] / 'piper_reader.pyw')).load_module()


class ContentsNavigationTests(unittest.TestCase):
    def pdf(self):
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.doc = [SimpleNamespace(rect=SimpleNamespace(height=800)) for _ in range(3)]
        viewer.offsets = [10, 1624, 3238]
        viewer.zoom = 2.0
        viewer.total_h = 4852
        viewer.canvas = mock.Mock()
        viewer._render_visible = mock.Mock()
        viewer.focused_index = 8
        return viewer

    def test_pdf_navigation_uses_current_zoom_and_within_page_target(self):
        viewer = self.pdf()
        self.assertTrue(viewer.go_to_destination(1, 125))
        viewer.canvas.yview_moveto.assert_called_once_with((1624 + 250 - 10) / 4852)
        self.assertEqual(viewer.zoom, 2.0)
        self.assertIsNone(viewer.focused_index)

    def test_invalid_pdf_destination_never_changes_scroll(self):
        viewer = self.pdf()
        for page in (-1, 3, True, '1'):
            self.assertFalse(viewer.go_to_destination(page, 0))
        viewer.canvas.yview_moveto.assert_not_called()

    def test_initial_scroll_callback_waits_until_page_layout_exists(self):
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.winfo_exists = lambda: True
        viewer._visible_pages = mock.Mock(return_value=(0, 0))
        viewer._render_visible()
        viewer._visible_pages.assert_not_called()

    def test_web_navigation_uses_rendered_heading_anchor(self):
        viewer = APP.WebArticleViewer.__new__(APP.WebArticleViewer)
        viewer.block_starts = {3: '12.0'}
        viewer.reader = mock.Mock()
        viewer.click_cb = mock.Mock()
        self.assertTrue(viewer.go_to_section(3))
        viewer.reader.see.assert_called_once_with('12.0')
        viewer.click_cb.assert_not_called()
        self.assertFalse(viewer.go_to_section(999))

    def app(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._playback_lock = threading.RLock()
        app._speech_requests = set()
        app._pause_requested = threading.Event()
        app._active_cancel = None
        app._document_read_cancel = None
        app._document_read_viewer = None
        app._set_playback_state = mock.Mock()
        app.set_status = mock.Mock()
        app.stop = mock.Mock()
        app.speak_doc = mock.Mock()
        app.text = mock.Mock()
        app._library_is_visible = lambda: False
        return app

    def test_navigating_pauses_document_but_does_not_cancel_other_jobs(self):
        app = self.app()
        viewer = self.pdf()
        app.viewer = viewer
        cancel = threading.Event()
        app._speech_requests.add(cancel)
        app._active_cancel = app._document_read_cancel = cancel
        app._document_read_viewer = viewer
        self.assertTrue(app._navigate_contents_entry({'title': 'Chapter 2', 'page': 1, 'y': 125}, viewer))
        self.assertTrue(app._pause_requested.is_set())
        self.assertFalse(cancel.is_set())
        app.stop.assert_not_called()
        app.speak_doc.assert_not_called()
        app._set_playback_state.assert_called_with('paused')

    def test_clipboard_reading_is_not_paused_by_document_browsing(self):
        app = self.app()
        app.viewer = self.pdf()
        app._active_cancel = threading.Event()
        app._document_read_cancel = threading.Event()
        app._speech_requests.add(app._active_cancel)
        self.assertTrue(app._navigate_contents_entry({'title': 'Page 2', 'page': 1}, app.viewer))
        self.assertFalse(app._pause_requested.is_set())
        app.stop.assert_not_called()

    def test_stale_contents_entry_cannot_navigate_replacement_document(self):
        app = self.app()
        old = self.pdf()
        app.viewer = self.pdf()
        self.assertFalse(app._navigate_contents_entry({'title': 'Old chapter', 'page': 1}, old))
        app.viewer.canvas.yview_moveto.assert_not_called()

    def test_plain_text_navigation_moves_to_heading_without_starting_reading(self):
        app = self.app()
        app.viewer = None
        self.assertTrue(app._navigate_contents_entry({'title': 'Chapter Two', 'index': '9.0'}, None))
        app.text.see.assert_called_once_with('9.0')
        app.text.mark_set.assert_called_once_with('insert', '9.0')
        app.speak_doc.assert_not_called()

    def test_late_highlight_cannot_pull_view_away_while_browsing_paused_document(self):
        app = self.app()
        app.viewer = mock.Mock()
        app._contents_browsing_viewer = app.viewer
        app._pause_requested.set()
        app._safe_focus({'viewer': app.viewer, 'chunk_index': 3})
        app.viewer.focus_chunk.assert_not_called()


if __name__ == '__main__':
    unittest.main()
