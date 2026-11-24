# Public Windows Binary Release Implementation Plan

> **For agentic workers:** Execute independent dependency provenance and source collection tasks in parallel; root integrates, reviews, verifies and publishes.

**Goal:** Publish a usable PiperReader 1.6.5 Windows installer and portable ZIP with complete license notices and access to matching source.

**Architecture:** Build in an isolated Python environment from the public repository. Pin native PDF/audio libraries to verifiable source, collect required source archives with hashes, stage all distribution notices into the common installer/portable payload, and enforce preflight checks. Keep the user's working application and data untouched.

**Tech Stack:** Python 3.12 x64, PyInstaller, Inno Setup 6, Tk, PyMuPDF/MuPDF, SoundFile/libsndfile, Piper and Kokoro.

**Spec:** User authorization to complete shareable EXE and publish publicly under AGPL-3.0; existing source release v1.6.4.

## Global Constraints

- Application license AGPL-3.0-only. Preserve upstream copyright and license terms.
- Never package credentials, user settings, documents, recordings, downloaded voices or local bridge credentials.
- Every redistributed copyleft binary has identified matching source and build instructions; do not relabel unresolved binaries.
- No mutation of global Python, live app, or user settings for build preparation.
- Preserve reader features, clipboard offline fallback, TOC navigation and MP3 decoding.

## Review Focus

- Unresolved native source: replace with a pinned build instead of claiming version strings establish provenance.
- Portable ZIP missing notices: verify same common license/source-direction payload as installer.
- Missing source/license downloads: fail release preparation on absent or wrong hash.
- Engine dependency changes: test PDF/OCR, WAV and Microsoft MP3 decode plus frozen runtime.
- Private data: inspect staged file paths and credential placeholder before upload.

## Tasks

- [x] Pin verified PDF binary/source in isolated build environment and record provenance.
- [x] Build or identify fully documented native audio dependencies; test MP3 and WAV decoding.
- [x] Add pinned source/license collection with missing-material regression tests.
- [x] Stage LICENSE, NOTICE, third-party notices and source directions into both Windows packages; bump to 1.6.5.
- [x] Run full tests, frozen checks, actual installer/portable checks and independent release review.
- [ ] Commit/tag, publish installer/portable/app source/dependency sources/checksums, and verify public downloads.
