import importlib.machinery
import importlib.util
import os
from types import SimpleNamespace
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader(
    "piper_reader_document_centering_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


class _Page:
    def __init__(self, width, height):
        self.rect = SimpleNamespace(width=width, height=height)


class _Canvas:
    def __init__(self, width=1000, height=600):
        self.width = width
        self.height = height
        self.created = []
        self.coordinates = {}
        self.config = {}
        self.y_fraction = None

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    @staticmethod
    def canvasx(value):
        return value

    @staticmethod
    def canvasy(value):
        return value

    def delete(self, _tag):
        return None

    def create_rectangle(self, *coords, **options):
        item = len(self.created) + 1
        self.created.append((item, coords, options))
        self.coordinates[item] = coords
        return item

    def coords(self, item, *coords):
        self.coordinates[item] = coords

    def configure(self, **changes):
        self.config.update(changes)

    def tag_raise(self, _tag):
        return None

    def yview_moveto(self, fraction):
        self.y_fraction = fraction


class DocumentPageLayoutTests(unittest.TestCase):
    def test_page_is_centered_when_viewport_is_wider(self):
        content, layout, offsets = APP.PdfViewer.centered_page_layout(
            [660], 1200)

        self.assertEqual(content, 680)
        self.assertEqual(layout, 1200)
        self.assertEqual(offsets, [270])

    def test_wide_page_keeps_margins_and_horizontal_scroll_width(self):
        content, layout, offsets = APP.PdfViewer.centered_page_layout(
            [660], 400)

        self.assertEqual(content, 680)
        self.assertEqual(layout, 680)
        self.assertEqual(offsets, [APP.DOCUMENT_PAGE_MARGIN])

    def test_mixed_page_sizes_are_centered_independently(self):
        content, layout, offsets = APP.PdfViewer.centered_page_layout(
            [660, 400], 1000)

        self.assertEqual(content, 680)
        self.assertEqual(layout, 1000)
        self.assertEqual(offsets, [170, 300])

    def test_layout_pages_uses_centered_origins(self):
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.doc = [_Page(600, 800), _Page(400, 500)]
        viewer.zoom = 1.0
        viewer.canvas = _Canvas(width=1000)
        viewer.images = {0: object()}
        viewer.after_idle = lambda _callback: "after-id"
        viewer._sync_horizontal_scrollbar = lambda: None

        APP.PdfViewer._layout_pages(viewer)

        self.assertEqual(viewer.page_widths, [600, 400])
        self.assertEqual(viewer.page_x_offsets, [200, 300])
        self.assertEqual(viewer.canvas.coordinates[1], (200, 10, 800, 810))
        self.assertEqual(viewer.canvas.coordinates[2], (300, 824, 700, 1324))
        self.assertEqual(
            viewer.canvas.config["scrollregion"], (0, 0, 1000, 1338))

    def test_resize_moves_page_border_image_and_highlight_origin(self):
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.canvas = _Canvas(width=1000)
        viewer.page_widths = [600]
        viewer.page_heights = [800]
        viewer.page_x_offsets = [10]
        viewer.page_rectangles = [41]
        viewer.offsets = [10]
        viewer.images = {0: object()}
        viewer.total_h = 824
        viewer.focused_index = None
        viewer._recenter_after_id = "pending"
        viewer.winfo_exists = lambda: True
        viewer._sync_horizontal_scrollbar = lambda: None
        viewer._render_visible = lambda: None

        APP.PdfViewer._recenter_pages(viewer)

        self.assertIsNone(viewer._recenter_after_id)
        self.assertEqual(viewer.page_x_offsets, [200])
        self.assertEqual(viewer.canvas.coordinates[41], (200, 10, 800, 810))
        self.assertEqual(viewer.canvas.coordinates["img0"], (200, 10))

    def test_click_and_highlight_share_centered_page_origin(self):
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.doc = [_Page(600, 800)]
        viewer.zoom = 1.0
        viewer.offsets = [10]
        viewer.page_widths = [600]
        viewer.page_x_offsets = [200]
        viewer.chunks = [("Target sentence.", 0, [(10, 10, 40, 25)])]
        viewer.canvas = _Canvas(width=1000)
        viewer.total_h = 824
        viewer.images = {}
        clicked = []
        viewer.click_cb = clicked.append

        APP.PdfViewer._on_click(viewer, SimpleNamespace(x=215, y=25))
        self.assertEqual(clicked, [0])

        APP.PdfViewer._on_click(viewer, SimpleNamespace(x=100, y=25))
        self.assertEqual(clicked, [0], "clicking the centered gutter is ignored")

        viewer.winfo_exists = lambda: True
        viewer._render_visible = lambda: None
        APP.PdfViewer.focus_chunk(viewer, 0, scroll=False)
        self.assertEqual(
            viewer.canvas.created[-1][1], (210, 20, 240, 35))


if __name__ == "__main__":
    unittest.main()
