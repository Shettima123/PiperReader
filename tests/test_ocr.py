import hashlib
import importlib.machinery
import importlib.util
import io
import math
import os
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader("piper_reader_ocr_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)

try:
    import fitz
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - the frozen build requires both modules
    fitz = None
    Image = ImageDraw = ImageFont = None


BUNDLED_TESSDATA = os.path.join(ROOT, "build_assets", "ocr", "tessdata")
HAS_BUNDLED_ENGLISH = APP._ocr_model_is_valid(BUNDLED_TESSDATA, "eng")


def _word(text, x0=10, y0=10, x1=40, y1=25,
          block=0, line=0, number=0):
    return (float(x0), float(y0), float(x1), float(y1),
            text, block, line, number)


class OcrProfileTests(unittest.TestCase):
    def test_normalize_mode_accepts_keys_and_ui_labels(self):
        self.assertEqual(APP.normalize_ocr_mode(None), "auto")
        self.assertEqual(APP.normalize_ocr_mode("ENGLISH"), "english")
        self.assertEqual(
            APP.normalize_ocr_mode("Arabic + English"), "arabic")
        self.assertEqual(
            APP.normalize_ocr_mode("hausa (BASIC latin FALLBACK)"), "hausa")
        self.assertEqual(APP.normalize_ocr_mode("not a mode"), "auto")

    def test_auto_profile_tracks_voice_language(self):
        arabic = APP.resolve_ocr_profile("auto", "ar-JO")
        self.assertEqual(arabic["mode"], "arabic")
        self.assertEqual(arabic["language"], "ara+eng")

        hausa = APP.resolve_ocr_profile("Auto (match voice)", "ha-NG")
        self.assertEqual(hausa["mode"], "hausa")
        self.assertEqual(hausa["language"], "eng")
        self.assertIn("hooked letters", hausa["warning"])

        english = APP.resolve_ocr_profile("auto", "en-US")
        self.assertEqual(english["mode"], "english")
        self.assertEqual(english["language"], "eng")

    def test_explicit_profile_overrides_voice_language(self):
        profile = APP.resolve_ocr_profile("english", "ar-JO")
        self.assertEqual(profile["mode"], "english")
        self.assertEqual(profile["language"], "eng")


class OcrPageDetectionTests(unittest.TestCase):
    def test_blank_or_incidental_text_needs_ocr(self):
        self.assertTrue(APP.page_needs_ocr([]))
        self.assertTrue(APP.page_needs_ocr([_word("A very long watermark")]))
        self.assertTrue(APP.page_needs_ocr([
            _word("a", number=0), _word("b", number=1),
        ]))
        self.assertTrue(APP.page_needs_ocr([(0, 0, 1, 1)]))

    def test_substantial_native_text_skips_ocr(self):
        words = [
            _word("Native", number=0),
            _word("searchable", x0=45, x1=105, number=1),
            _word("paragraph", x0=110, x1=170, number=2),
        ]
        self.assertFalse(APP.page_needs_ocr(words))


class OcrModelResolutionTests(unittest.TestCase):
    def test_verified_bundled_model_is_preferred_without_download(self):
        bundled = os.path.join("X:\\", "bundled-ocr")
        writable = os.path.join("X:\\", "user-ocr")

        def valid(directory, code):
            return code == "eng" and directory == bundled

        with mock.patch.object(APP, "BUNDLED_OCR_TESSDATA_DIR", bundled), \
                mock.patch.object(APP, "OCR_TESSDATA_DIR", writable), \
                mock.patch.object(APP, "_ocr_model_is_valid",
                                  side_effect=valid) as checked, \
                mock.patch.object(APP, "download_file") as download:
            resolved = APP.ensure_ocr_tessdata("eng")

        self.assertEqual(resolved, bundled)
        self.assertGreaterEqual(checked.call_count, 1)
        download.assert_not_called()

    def test_missing_model_is_downloaded_and_verified(self):
        payload = b"small deterministic OCR model fixture"
        metadata = {
            "filename": "eng.traineddata",
            "url": "https://invalid.example/eng.traineddata",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "label": "test English OCR data",
        }
        with tempfile.TemporaryDirectory(prefix="piper_ocr_model_test_") as tmp:
            bundled = os.path.join(tmp, "bundled")
            writable = os.path.join(tmp, "writable")
            os.makedirs(bundled)
            calls = []

            def download(_url, destination, _status, _label,
                         expected_sha256=None, expected_size=None):
                calls.append((destination, expected_sha256, expected_size))
                with open(destination, "wb") as stream:
                    stream.write(payload)

            with mock.patch.dict(
                    APP.OCR_MODEL_FILES, {"eng": metadata}, clear=True), \
                    mock.patch.object(
                        APP, "BUNDLED_OCR_TESSDATA_DIR", bundled), \
                    mock.patch.object(APP, "OCR_TESSDATA_DIR", writable), \
                    mock.patch.object(APP, "download_file",
                                      side_effect=download):
                resolved = APP.ensure_ocr_tessdata("eng")
                self.assertTrue(APP._ocr_model_is_valid(resolved, "eng"))

            self.assertEqual(resolved, writable)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1:],
                             (metadata["sha256"], metadata["size"]))

    def test_cancelled_model_resolution_neither_downloads_nor_creates_data(self):
        with tempfile.TemporaryDirectory(prefix="piper_ocr_cancel_test_") as tmp:
            bundled = os.path.join(tmp, "missing-bundled")
            writable = os.path.join(tmp, "must-not-be-created")
            with mock.patch.object(
                    APP, "BUNDLED_OCR_TESSDATA_DIR", bundled), \
                    mock.patch.object(APP, "OCR_TESSDATA_DIR", writable), \
                    mock.patch.object(APP, "download_file") as download:
                with self.assertRaises(APP.OcrCancelled):
                    APP.ensure_ocr_tessdata(
                        "eng", is_current=lambda: False)
            download.assert_not_called()
            self.assertFalse(os.path.exists(writable))


class _FakePage:
    def __init__(self, native_words, ocr_words=()):
        self.native_words = list(native_words)
        self.ocr_words = list(ocr_words)
        self.ocr_calls = []
        self.rect = type("Rect", (), {"width": 612.0, "height": 792.0})()

    def get_text(self, kind, textpage=None, sort=False):
        if kind != "words":
            raise AssertionError(f"unexpected extraction kind: {kind}")
        return list(self.ocr_words if textpage is not None
                    else self.native_words)

    def get_textpage_ocr(self, **kwargs):
        self.ocr_calls.append(kwargs)
        return object()


class _FakeDocument:
    needs_pass = False

    def __init__(self, pages):
        self.pages = list(pages)
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.closed = True


@unittest.skipIf(fitz is None, "PyMuPDF is required")
class OcrPageSelectionTests(unittest.TestCase):
    def test_cancellation_before_page_prevents_ocr_call(self):
        scanned = _FakePage([])
        document = _FakeDocument([scanned])
        with mock.patch.object(fitz, "open", return_value=document), \
                mock.patch.object(APP, "ensure_ocr_tessdata",
                                  return_value="test-tessdata"):
            with self.assertRaises(APP.OcrCancelled):
                APP.recognize_pdf_pages(
                    "scan.pdf", "eng", "English", lambda _message: None,
                    before_page=lambda _page, _ordinal, _total: False)

        self.assertEqual(scanned.ocr_calls, [])
        self.assertTrue(document.closed)

    def test_mixed_document_only_ocrs_scanned_page(self):
        native_words = [
            _word("Native", number=0),
            _word("searchable", x0=45, x1=105, number=1),
            _word("paragraph", x0=110, x1=170, number=2),
        ]
        recognized_words = [
            _word("Scanned", block=1, number=0),
            _word("page", x0=45, x1=80, block=1, number=1),
            _word("77", x0=85, x1=105, block=1, number=2),
        ]
        native = _FakePage(native_words)
        scanned = _FakePage([], recognized_words)
        document = _FakeDocument([native, scanned])
        statuses = []

        with mock.patch.object(fitz, "open", return_value=document), \
                mock.patch.object(APP, "ensure_ocr_tessdata",
                                  return_value="test-tessdata") as ensure:
            result = APP.recognize_pdf_pages(
                "mixed.pdf", "eng", "English", statuses.append)

        self.assertEqual(native.ocr_calls, [])
        self.assertEqual(len(scanned.ocr_calls), 1)
        self.assertEqual(set(result["words_by_page"]), {1})
        self.assertEqual(result["attempted_pages"], 1)
        self.assertEqual(result["recognized_pages"], 1)
        self.assertEqual(result["word_count"], 6)
        self.assertTrue(any("page 2/2" in status for status in statuses))
        ensure.assert_called_once()


@unittest.skipIf(fitz is None or Image is None,
                 "PyMuPDF and Pillow are required")
class OcrImageAndRealRecognitionTests(unittest.TestCase):
    @staticmethod
    def _font(size):
        windows_font = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
        try:
            return ImageFont.truetype(windows_font, size)
        except OSError:
            try:
                return ImageFont.truetype("arial.ttf", size)
            except OSError:
                return ImageFont.load_default(size=max(16, size - 10))

    @classmethod
    def _save_text_image(cls, path, text, size=(1000, 220)):
        image = Image.new("RGB", size, "white")
        drawing = ImageDraw.Draw(image)
        drawing.text((30, 65), text, fill="black", font=cls._font(62))
        image.save(path)

    def test_image_to_pdf_produces_a_valid_atomic_pdf(self):
        with tempfile.TemporaryDirectory(prefix="piper_image_pdf_test_") as tmp:
            image_path = os.path.join(tmp, "page.png")
            pdf_path = os.path.join(tmp, "page.pdf")
            self._save_text_image(image_path, "IMAGE PAGE 12", (700, 180))

            returned = APP.image_to_pdf(image_path, pdf_path)

            self.assertEqual(returned, pdf_path)
            self.assertTrue(os.path.isfile(pdf_path))
            self.assertFalse(os.path.exists(pdf_path + ".part.pdf"))
            with fitz.open(pdf_path) as document:
                self.assertEqual(len(document), 1)
                self.assertGreater(document[0].rect.width, 0)
                self.assertGreater(document[0].rect.height, 0)

    @unittest.skipUnless(HAS_BUNDLED_ENGLISH,
                         "verified bundled English OCR model is absent")
    def test_scanned_english_ocr_returns_text_and_valid_word_boxes(self):
        with tempfile.TemporaryDirectory(prefix="piper_real_ocr_test_") as tmp:
            image_path = os.path.join(tmp, "scan.png")
            pdf_path = os.path.join(tmp, "scan.pdf")
            self._save_text_image(image_path, "PIPER OCR TEST 42")
            APP.image_to_pdf(image_path, pdf_path)
            writable = os.path.join(tmp, "user-tessdata")
            statuses = []

            with mock.patch.object(
                    APP, "BUNDLED_OCR_TESSDATA_DIR", BUNDLED_TESSDATA), \
                    mock.patch.object(APP, "OCR_TESSDATA_DIR", writable):
                result = APP.recognize_pdf_pages(
                    pdf_path, "eng", "English", statuses.append,
                    dpi=APP.OCR_DPI)

            self.assertEqual(result["attempted_pages"], 1)
            self.assertEqual(result["recognized_pages"], 1)
            words = result["words_by_page"][0]
            recognized = " ".join(str(word[4]) for word in words).upper()
            self.assertIn("PIPER", recognized)
            self.assertIn("42", recognized)
            self.assertGreaterEqual(len(words), 3)

            with fitz.open(pdf_path) as document:
                page_rect = document[0].rect
            for word in words:
                self.assertEqual(len(word), 8)
                x0, y0, x1, y1 = word[:4]
                self.assertTrue(all(math.isfinite(value)
                                    for value in (x0, y0, x1, y1)))
                self.assertLess(x0, x1)
                self.assertLess(y0, y1)
                self.assertGreaterEqual(x0, -0.01)
                self.assertGreaterEqual(y0, -0.01)
                self.assertLessEqual(x1, page_rect.width + 0.01)
                self.assertLessEqual(y1, page_rect.height + 0.01)
                self.assertTrue(all(isinstance(value, int)
                                    for value in word[5:8]))
            self.assertTrue(any("page 1/1" in status for status in statuses))


class PdfViewerChunkPlanningTests(unittest.TestCase):
    def test_ocr_words_use_native_chunk_repeat_and_box_planning_headlessly(self):
        native = _FakePage([
            _word("one.", x0=55, x1=85, number=1),
            _word("Native", x0=10, x1=50, number=0),
        ])
        ignored_native = _FakePage([_word("SHOULD-NOT-BE-USED")])
        viewer = APP.PdfViewer.__new__(APP.PdfViewer)
        viewer.doc = [native, ignored_native]
        viewer.ocr_words_by_page = {99: [_word("stale")]}
        ocr_words = {
            1: [
                _word("page.", x0=55, x1=90, block=2, number=1),
                _word("Second", x0=10, x1=50, block=2, number=0),
                _word("Another", x0=10, x1=60, y0=40, y1=55,
                      block=3, number=0),
            ],
        }

        chunks = APP.PdfViewer.build_chunks(
            viewer, max_len=100, words_by_page=ocr_words)

        self.assertEqual([chunk[0] for chunk in chunks],
                         ["Native one.", "Second page.", "Another"])
        self.assertEqual([chunk[1] for chunk in chunks], [0, 1, 1])
        self.assertEqual(chunks[0][2], [
            (10.0, 10.0, 50.0, 25.0),
            (55.0, 10.0, 85.0, 25.0),
        ])
        self.assertEqual(
            [group["sentence_id"] for group in viewer.repeat_groups],
            [0, 1, 2])
        self.assertEqual(
            [group["paragraph_id"] for group in viewer.repeat_groups],
            ["0:0", "1:2", "1:3"])
        self.assertEqual(set(viewer.ocr_words_by_page), {1})


if __name__ == "__main__":
    unittest.main()
