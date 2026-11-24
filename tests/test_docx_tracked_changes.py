import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader("piper_reader_docx_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">Keep </w:t></w:r>
      <w:del w:id="1" w:author="Reviewer"><w:r><w:delText>Old</w:delText></w:r></w:del>
      <w:ins w:id="2" w:author="Reviewer"><w:r><w:t>New</w:t></w:r></w:ins>
      <w:r><w:t xml:space="preserve"> text.</w:t></w:r>
      <w:r><w:rPr><w:rPrChange w:id="3" w:author="Reviewer"/></w:rPr><w:t> Bold.</w:t></w:r>
    </w:p>
    <w:p>
      <w:moveFrom w:id="4"><w:r><w:t>Moved away.</w:t></w:r></w:moveFrom>
      <w:moveTo w:id="5"><w:r><w:t>Moved here.</w:t></w:r></w:moveTo>
    </w:p>
  </w:body>
</w:document>
"""

SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:trackRevisions/>
</w:settings>
"""

COMMENTS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="Reviewer"><w:p><w:r><w:t>Check this.</w:t></w:r></w:p></w:comment>
</w:comments>
"""


def make_docx(path, tracked=True):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", DOCUMENT_XML)
        if tracked:
            archive.writestr("word/settings.xml", SETTINGS_XML)
            archive.writestr("word/comments.xml", COMMENTS_XML)


class _FakeView:
    pass


class _FakeDocument:
    def __init__(self, fail_export=False):
        self.ActiveWindow = types.SimpleNamespace(View=_FakeView())
        self.fail_export = fail_export
        self.export_args = None
        self.closed_with = None

    def ExportAsFixedFormat(self, *args):
        self.export_args = args
        with open(args[0], "wb") as output:
            output.write(b"partial" if self.fail_export else b"%PDF-test")
        if self.fail_export:
            raise RuntimeError("export failed")

    def Close(self, save_changes):
        self.closed_with = save_changes


class _FakeDocuments:
    def __init__(self, document):
        self.document = document
        self.open_args = None

    def Open(self, *args):
        self.open_args = args
        return self.document


class _FakeWord:
    def __init__(self, document):
        self.Documents = _FakeDocuments(document)
        self.quit_called = False

    def Quit(self):
        self.quit_called = True


class DocxTrackedChangesTests(unittest.TestCase):
    def test_review_summary_and_final_text_exclude_deleted_content(self):
        with tempfile.TemporaryDirectory(prefix="piper_docx_review_") as tmp:
            path = os.path.join(tmp, "review.docx")
            make_docx(path)
            text, summary = APP._docx_review_content(path)

        self.assertEqual(text, "Keep New text. Bold.\n\nMoved here.")
        self.assertNotIn("Old", text)
        self.assertNotIn("Moved away", text)
        self.assertEqual(summary["insertions"], 2)
        self.assertEqual(summary["deletions"], 2)
        self.assertEqual(summary["format_changes"], 1)
        self.assertEqual(summary["comments"], 1)
        self.assertTrue(summary["tracking_enabled"])
        self.assertTrue(summary["has_markup"])

    def test_extract_document_reads_current_text_only(self):
        with tempfile.TemporaryDirectory(prefix="piper_docx_extract_") as tmp:
            path = os.path.join(tmp, "review.docx")
            make_docx(path)
            text = APP.extract_document(path, lambda _message: None)

        self.assertEqual(text, "Keep New text. Bold.\n\nMoved here.")

    def _run_fake_export(self, source_path, output_dir, fail=False):
        document = _FakeDocument(fail_export=fail)
        word = _FakeWord(document)
        calls = []
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = lambda: calls.append("initialize")
        pythoncom.CoUninitialize = lambda: calls.append("uninitialize")
        client = types.ModuleType("win32com.client")
        client.DispatchEx = lambda name: (
            calls.append(("dispatch", name)) or word)
        win32com = types.ModuleType("win32com")
        win32com.client = client
        modules = {
            "pythoncom": pythoncom,
            "win32com": win32com,
            "win32com.client": client,
        }
        with mock.patch.dict(sys.modules, modules):
            if fail:
                with self.assertRaisesRegex(RuntimeError, "export failed"):
                    APP.docx_to_pdf(source_path, output_dir)
                output = None
            else:
                output = APP.docx_to_pdf(source_path, output_dir)
        return output, document, word, calls

    def test_word_export_is_isolated_read_only_and_includes_markup(self):
        with tempfile.TemporaryDirectory(prefix="piper_docx_export_") as tmp:
            source_path = os.path.join(tmp, "source.docx")
            output_dir = os.path.join(tmp, "output")
            os.makedirs(output_dir)
            make_docx(source_path)
            with open(source_path, "rb") as source:
                before = source.read()
            output, document, word, calls = self._run_fake_export(
                source_path, output_dir)
            with open(source_path, "rb") as source:
                after = source.read()

            self.assertTrue(os.path.isfile(output))
            self.assertEqual(before, after)
            self.assertEqual(word.Documents.open_args[1:4],
                             (False, True, False))
            self.assertNotEqual(word.Documents.open_args[0], source_path)
            self.assertFalse(os.path.exists(word.Documents.open_args[0]))
            self.assertEqual(document.export_args[1], 17)
            self.assertEqual(document.export_args[7], 7)
            self.assertTrue(document.ShowRevisions)
            self.assertTrue(document.PrintRevisions)
            self.assertEqual(document.ActiveWindow.View.RevisionsView, 0)
            self.assertEqual(document.ActiveWindow.View.MarkupMode, 1)
            self.assertEqual(document.closed_with, False)
            self.assertTrue(word.quit_called)
            self.assertEqual(calls[0], "initialize")
            self.assertIn(("dispatch", "Word.Application"), calls)
            self.assertEqual(calls[-1], "uninitialize")

    def test_failed_export_removes_partial_pdf_and_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="piper_docx_failure_") as tmp:
            source_path = os.path.join(tmp, "source.docx")
            output_dir = os.path.join(tmp, "output")
            os.makedirs(output_dir)
            make_docx(source_path)
            _output, document, word, calls = self._run_fake_export(
                source_path, output_dir, fail=True)

            self.assertEqual(os.listdir(output_dir), [])
            self.assertEqual(document.closed_with, False)
            self.assertTrue(word.quit_called)
            self.assertEqual(calls[-1], "uninitialize")

    def test_speech_alignment_uses_final_words_and_skips_review_text(self):
        words = {
            0: [
                (0, 0, 20, 10, "Keep", 0, 0, 0),
                (21, 0, 60, 10, "OldNew", 0, 0, 1),
                (61, 0, 85, 10, "text.", 0, 0, 2),
                (90, 0, 130, 10, "Comment", 1, 0, 0),
            ],
            1: [(0, 0, 30, 10, "Next.", 0, 0, 0)],
        }
        aligned, stats = APP.align_spoken_text_to_pdf_words(
            "Keep New text.\n\nNext.", words)
        spoken = [word[4] for page in sorted(aligned)
                  for word in aligned[page]]

        self.assertEqual(spoken, ["Keep", "New", "text.", "Next."])
        self.assertNotIn("Old", " ".join(spoken))
        self.assertNotIn("Comment", spoken)
        self.assertEqual(stats["source_words"], 4)
        self.assertEqual(stats["matched_words"], 4)
        self.assertEqual(stats["confidence"], 1.0)
        new_word = aligned[0][1]
        self.assertGreater(new_word[0], 21)
        self.assertLessEqual(new_word[2], 60)


if __name__ == "__main__":
    unittest.main()
