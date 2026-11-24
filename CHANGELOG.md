# Changelog

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
