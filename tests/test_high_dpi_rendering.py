import importlib.machinery
import importlib.util
import math
import os
import xml.etree.ElementTree as ET
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader(
    "piper_reader_high_dpi_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


class DisplayScaleTests(unittest.TestCase):
    def test_standard_windows_dpi_scales(self):
        self.assertEqual(APP.display_scale_from_dpi(96), 1.0)
        self.assertEqual(APP.display_scale_from_dpi(120), 1.25)
        self.assertEqual(APP.display_scale_from_dpi(144), 1.5)

    def test_invalid_dpi_falls_back_to_one(self):
        for value in (None, "", 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(APP.display_scale_from_dpi(value), 1.0)

    def test_fixed_window_sizes_preserve_physical_size(self):
        self.assertEqual(APP.scaled_ui_pixels(1160, 120), 1450)
        self.assertEqual(APP.scaled_ui_pixels(870, 120), 1088)


class NativePdfRenderingTests(unittest.TestCase):
    def test_fit_width_is_native_at_common_dpi_values(self):
        page_width = 612.0
        self.assertAlmostEqual(
            APP.pdf_fit_zoom(page_width, 96), 660.0 / page_width)
        self.assertAlmostEqual(
            APP.pdf_fit_zoom(page_width, 120), 825.0 / page_width)
        self.assertAlmostEqual(
            APP.pdf_fit_zoom(page_width, 144), 990.0 / page_width)

    def test_real_pdf_pixmap_has_native_125_percent_dimensions(self):
        import fitz

        document = fitz.open()
        try:
            page = document.new_page(width=612, height=792)
            zoom = APP.pdf_fit_zoom(page.rect.width, 120)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            self.assertEqual((pixmap.width, pixmap.height), (825, 1068))
        finally:
            document.close()


class SmoothHighlightTests(unittest.TestCase):
    def test_neighboring_words_merge_on_one_line(self):
        merged = APP.merge_pdf_highlight_rectangles([
            (10, 20, 30, 32), (33, 20.5, 55, 32.5),
            (10, 45, 40, 57),
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], (10.0, 20.0, 55.0, 32.5))

    def test_overlay_is_transparent_outside_and_translucent_inside(self):
        overlay, left, top = APP.create_pdf_highlight_overlay(
            [(10, 20, 30, 32), (33, 20, 55, 32)], 1.25)
        self.assertIsNotNone(overlay)
        self.assertLessEqual(left, math.floor(10 * 1.25))
        self.assertLessEqual(top, math.floor(20 * 1.25))
        self.assertEqual(overlay.mode, "RGBA")
        self.assertEqual(overlay.getpixel((0, 0))[3], 0)
        alpha_extrema = overlay.getchannel("A").getextrema()
        self.assertEqual(alpha_extrema[0], 0)
        self.assertGreater(alpha_extrema[1], 0)
        self.assertLess(alpha_extrema[1], 255)


class PackagedManifestTests(unittest.TestCase):
    def test_manifest_declares_per_monitor_v2(self):
        manifest = os.path.join(ROOT, "build_assets", "piper_reader.manifest")
        tree = ET.parse(manifest)
        root = tree.getroot()
        awareness = root.find(
            ".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}"
            "dpiAwareness")
        legacy = root.find(
            ".//{http://schemas.microsoft.com/SMI/2005/WindowsSettings}"
            "dpiAware")
        self.assertIsNotNone(awareness)
        self.assertTrue(awareness.text.startswith("PerMonitorV2"))
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.text, "true/pm")

    def test_import_does_not_change_process_dpi_mode(self):
        self.assertEqual(APP.WINDOWS_DPI_MODE, "not-requested")


if __name__ == "__main__":
    unittest.main()
