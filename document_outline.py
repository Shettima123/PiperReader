"""Read-only local navigation entries for PDF, web, and plain-text readers.

PDF y destinations are already in displayed page coordinates in PyMuPDF's
get_toc(False) output. Callers can use page_offset + y * zoom directly.
No function follows links, opens files, performs OCR, or generates headings.
"""

import math
import re


_MARKDOWN_HEADING = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)\s*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_NAMED_SECTION = re.compile(
    r'^(Chapter|Part|Section)\s+(?:\d+(?:\.\d+)*|[IVXLCDM]+)'
    r'(?:(?:\s*[:\-–—]\s*|\s+)\S.*)?$', re.IGNORECASE)


def _title(value):
    if not isinstance(value, str):
        return ''
    # Tree rows are single-line labels; keep Unicode titles but no controls.
    return ' '.join(''.join(
        character if ord(character) >= 32 else ' ' for character in value).split())


def _destination_y(destination, page):
    target = destination.get('to')
    try:
        value = target.y if hasattr(target, 'y') else target[1]
        if isinstance(value, bool):
            return 0.0
        value = float(value)
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(value, float(page.rect.height)))
    except (AttributeError, TypeError, ValueError, IndexError, KeyError):
        return 0.0


def pdf_outline(doc):
    """Return (entries, 'bookmarks'|'pages') for safe internal destinations.

Entries contain level, title, zero-based page, and displayed-page y in points.
Malformed, external, or unresolved links are omitted. An absent or unreadable
outline falls back to an explicit page list rather than guessed headings.
    """
    try:
        page_count = len(doc)
    except (TypeError, ValueError, RuntimeError):
        page_count = 0
    entries = []
    try:
        rows = doc.get_toc(simple=False)
    except Exception:
        # PDF outline corruption should not prevent ordinary page navigation.
        rows = []
    if not isinstance(rows, (list, tuple)):
        rows = []
    previous_level = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        level, title, page_number = row[:3]
        if (type(level) is not int or level < 1 or type(page_number) is not int
                or not 1 <= page_number <= page_count):
            continue
        title = _title(title)
        if not title:
            continue
        destination = row[3] if len(row) > 3 else {}
        if not isinstance(destination, dict):
            destination = {}
        # LINK_GOTO=1. Never interpret URI, launch, remote-file, or unresolved
        # named destinations as an in-document page, even with a positive row.
        if destination.get('kind', 1) != 1 or any(
                key in destination for key in ('uri', 'file')):
            continue
        page = page_number - 1
        if 'page' in destination and (
                type(destination['page']) is not int or destination['page'] != page):
            continue
        level = min(level, previous_level + 1)
        y = _destination_y(destination, doc[page])
        entries.append({'level': level, 'title': title, 'page': page, 'y': y})
        previous_level = level
    if entries:
        return entries, 'bookmarks'
    return [
        {'level': 1, 'title': f'Page {page + 1}', 'page': page, 'y': 0.0}
        for page in range(page_count)
    ], 'pages'


def text_outline(text):
    """Find explicit Markdown or Chapter/Part/Section heading lines only.

Line indexes refer to the original text, without rewriting or stripping it.
Fenced/indented code, ordinary prose and numbered lists are not inferred.
    """
    if not isinstance(text, str):
        return []
    entries = []
    fence_character = None
    fence_length = 0
    # Tk Text indexes count newline characters, not all Unicode separators.
    for line_number, line in enumerate(text.split('\n'), 1):
        fence = _FENCE.match(line)
        if fence_character:
            if (fence and fence[1][0] == fence_character
                    and len(fence[1]) >= fence_length and not fence[2].strip()):
                fence_character = None
            continue
        if fence:
            fence_character, fence_length = fence[1][0], len(fence[1])
            continue
        if line.startswith(('    ', '\t')) or len(line) > 300:
            continue
        markdown = _MARKDOWN_HEADING.match(line)
        if markdown:
            title = _title(re.sub(r'\s+#+\s*$', '', markdown[2]))
            level = len(markdown[1])
        else:
            title = _title(line)
            named = _NAMED_SECTION.fullmatch(title)
            if not named or title.endswith(('.', '!', '?', ';')):
                continue
            level = 2 if named[1].casefold() == 'section' else 1
        if title:
            entries.append({'level': level, 'title': title,
                            'index': f'{line_number}.0'})
    return entries


def web_outline(article):
    """Use existing heading blocks; indexes exclude inserted title/byline.

The article extractor currently keeps heading kinds but not HTML h1-h6 depth,
so entries are intentionally flat rather than guessing a hierarchy.
    """
    if not isinstance(article, dict):
        return []
    blocks = article.get('blocks')
    if not isinstance(blocks, (list, tuple)):
        return []
    entries = []
    for block_index, block in enumerate(blocks):
        if not isinstance(block, (list, tuple)) or len(block) != 2:
            continue
        kind, text = block
        if kind != 'heading':
            continue
        title = _title(text)
        if title:
            entries.append({'level': 1, 'title': title,
                            'block_index': block_index})
    return entries
