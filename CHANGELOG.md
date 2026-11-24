# Changelog

## 1.6.5

- Publish Windows installer and portable builds with license notices and matching source downloads.
- Use PyMuPDF/MuPDF 1.28.2, with recorded matching upstream native source.
- Build the WAV/MP3 decoder from pinned libsndfile, mpg123, and LAME sources.
- Add an About & licenses window with the application license, third-party notices, and source links.
- Require verified sources, license materials, and native audio checks during release packaging.
- Keep the document, clipboard, voice, and Table of Contents behavior from 1.6.4.

## 1.6.4

- Add a Table of Contents button and a hideable navigation sidebar.
- Preserve nested PDF bookmarks and show a page list when bookmarks are absent.
- Navigate existing web headings and explicit plain-text headings.
- Pause active document narration when jumping to a section.
- Guard against stale document targets and early PDF rendering callbacks.
- Publish the original application source under AGPL-3.0-only.

Validation: 319 regression tests passed. Chapter navigation and narration pause
were verified in the installed Windows application. Page-list fallback and the
smaller-window layout were also checked. This initial public release supplies
source; downloadable Windows builds are pending their complete distribution
materials.
