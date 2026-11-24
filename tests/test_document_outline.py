"""Local contents extraction without text inference or external navigation."""
import math
import unittest
from unittest import mock

import fitz

from document_outline import pdf_outline, text_outline, web_outline


class PdfOutlineTests(unittest.TestCase):
    def make_document(self, pages=3):
        document = fitz.open()
        self.addCleanup(document.close)
        for _ in range(pages):
            document.new_page(width=400, height=600)
        return document

    def test_real_bookmarks_keep_order_hierarchy_and_page_destinations(self):
        document = self.make_document()
        document.set_toc([[1, 'Chapter One', 1, 60],
                          [2, 'A section', 2, 120],
                          [1, 'Chapter Two', 3, 180]])
        before = document.get_toc(simple=False)
        entries, source = pdf_outline(document)
        self.assertEqual(source, 'bookmarks')
        self.assertEqual([(e['level'], e['title'], e['page']) for e in entries],
                         [(1, 'Chapter One', 0), (2, 'A section', 1),
                          (1, 'Chapter Two', 2)])
        self.assertEqual([e['y'] for e in entries], [60, 120, 180])
        self.assertEqual(document.get_toc(simple=False), before)

    def test_unbookmarked_pdf_has_honest_complete_page_list(self):
        document = self.make_document()
        entries, source = pdf_outline(document)
        self.assertEqual(source, 'pages')
        self.assertEqual(entries, [
            {'level': 1, 'title': f'Page {page + 1}', 'page': page, 'y': 0.0}
            for page in range(3)])

    def test_rotated_cropped_page_keeps_displayed_destination_coordinate(self):
        document = self.make_document(1)
        page = document[0]
        page.set_cropbox(fitz.Rect(20, 30, 380, 570))
        page.set_rotation(90)
        document.set_toc([[1, 'Rotated heading', 1,
                           {'kind': fitz.LINK_GOTO, 'page': 0,
                            'to': fitz.Point(75, 110)}]])
        expected = document.get_toc(simple=False)[0][3]['to'].y
        entries, source = pdf_outline(document)
        self.assertEqual(source, 'bookmarks')
        self.assertAlmostEqual(entries[0]['y'], expected)
        self.assertLessEqual(entries[0]['y'], page.rect.height)

    def test_external_and_invalid_destinations_are_not_navigable(self):
        document = self.make_document()
        rows = [
            [1, 'Web', 1, {'kind': fitz.LINK_URI, 'uri': 'https://example.com'}],
            [1, 'Other file', 1, {'kind': fitz.LINK_GOTOR, 'file': 'other.pdf'}],
            [1, 'No target', -1, {'kind': fitz.LINK_NONE}],
            [1, 'Bad page', 99],
            [1, 'Zero', 0],
            [1, 'Fraction', 1.5],
            [1, 'Bool', True],
            [1, '', 1],
            ['bad', 'Wrong level', 1],
            [1, 'Good', 2, {'kind': fitz.LINK_GOTO, 'page': 1}],
        ]
        with mock.patch.object(document, 'get_toc', return_value=rows):
            entries, source = pdf_outline(document)
        self.assertEqual(source, 'bookmarks')
        self.assertEqual([entry['title'] for entry in entries], ['Good'])
        self.assertEqual(entries[0]['page'], 1)

    def test_bad_coordinate_and_depth_are_safely_normalized(self):
        document = self.make_document(1)
        rows = [[8, '  First\nheading  ', 1, {'kind': 1, 'to': (0, math.nan)}],
                [90, 'Second', 1, {'kind': 1, 'to': (0, 10000)}],
                [1, 'Third', 1, {'kind': 1, 'to': (0, -30)}]]
        with mock.patch.object(document, 'get_toc', return_value=rows):
            entries, _ = pdf_outline(document)
        self.assertEqual([entry['level'] for entry in entries], [1, 2, 1])
        self.assertEqual([entry['y'] for entry in entries], [0, 600, 0])
        self.assertEqual(entries[0]['title'], 'First heading')

    def test_unreadable_outline_falls_back_without_reading_page_text(self):
        document = self.make_document()
        with mock.patch.object(document, 'get_toc', side_effect=RuntimeError('broken outline')):
            entries, source = pdf_outline(document)
        self.assertEqual(source, 'pages')
        self.assertEqual(len(entries), 3)


class TextOutlineTests(unittest.TestCase):
    def test_markdown_and_explicit_sections_use_original_tk_line_indexes(self):
        text = 'Preface prose\n\n# Introduction\nBody.\n## A detail ##\n\nChapter 2: Water\nSection 2.1 — Pressure\nPart IV: Review\n'
        self.assertEqual(text_outline(text), [
            {'level': 1, 'title': 'Introduction', 'index': '3.0'},
            {'level': 2, 'title': 'A detail', 'index': '5.0'},
            {'level': 1, 'title': 'Chapter 2: Water', 'index': '7.0'},
            {'level': 2, 'title': 'Section 2.1 — Pressure', 'index': '8.0'},
            {'level': 1, 'title': 'Part IV: Review', 'index': '9.0'},
        ])

    def test_plain_prose_lists_hashtags_and_fenced_code_are_not_headings(self):
        text = ('A sentence about Chapter 2.\n1. A numbered item\nALL CAPS PROSE\n'
                '#hashtag\n```python\n# A comment\nChapter 2: sample\n```\n'
                '~~~\n## Also code\n~~~\n    # Indented code\n'
                'Chapter two discusses pressure.\n')
        self.assertEqual(text_outline(text), [])

    def test_blank_nontext_and_excessively_long_lines_have_no_entries(self):
        for value in (None, 7, '', '   \n', '# ' + 'x' * 400):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(text_outline(value), [])

    def test_crlf_and_unicode_heading_titles_preserve_line_number(self):
        entries = text_outline('Body\r\n# Résumé and الماء\r\nText\r\n')
        self.assertEqual(entries, [{'level': 1, 'title': 'Résumé and الماء', 'index': '2.0'}])

    def test_unicode_separators_do_not_change_tk_newline_indexes(self):
        entries = text_outline('A body\u2028separator\n# A real heading\n')
        self.assertEqual(entries[0]['index'], '2.0')


class WebOutlineTests(unittest.TestCase):
    def test_existing_heading_blocks_keep_original_article_indexes(self):
        article = {'title': 'Article', 'byline': 'Author', 'blocks': [
            ('body', 'Introductory text.'), ('heading', '  First section  '),
            ('body', 'Text.'), ('heading', 'Second section'), ('quote', 'Not heading')]}
        self.assertEqual(web_outline(article), [
            {'level': 1, 'title': 'First section', 'block_index': 1},
            {'level': 1, 'title': 'Second section', 'block_index': 3},
        ])

    def test_malformed_or_heading_free_article_has_no_synthetic_sections(self):
        for article in (None, {}, {'blocks': None}, {'blocks': [None, ('heading', ''), ('body', 'Text')]}):
            with self.subTest(article=article):
                self.assertEqual(web_outline(article), [])


if __name__ == '__main__':
    unittest.main()
