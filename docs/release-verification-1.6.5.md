# PiperReader 1.6.5 release verification

Verified on Windows x64, November 24, 2026:

- All 333 automated tests passed in the isolated release environment.
- PyInstaller and Inno Setup builds completed successfully.
- Frozen runtime checks passed, including imports, first-run data, OCR, library,
  offline AI Studio initialization, MP3 support, and distribution materials.
- All 62 dependency source archives matched their recorded sizes and SHA256 hashes.
- All 3,730 portable files matched the common installer input payload byte-for-byte.
- The final packaged app opened and rendered a three-page PDF, started Windows
  voice narration, displayed nested bookmarks, and paused at the selected chapter.
- The About window displayed the application license, third-party notices, and
  source directions with visible, working Close controls.
- Source and binary reviews found no personal documents, recordings, settings,
  API keys, downloaded voices, or live browser bridge credentials in the packages.

The native PDF version is PyMuPDF/MuPDF 1.28.2. The source-built audio DLL has
SHA256 `8c1d64dada2071ae82fbb98d4a3e2dc0c9c8269bcace0e0aa396923363ccf492`.
See the pinned manifests and native audio recipe for the corresponding sources.

The desktop check used the final packaged executable and a separate user-data
directory. It did not perform a fresh installer installation or test every online
provider. The installer was compiled and its input payload, version, and checksum
were checked. It is unsigned. Final download hashes are in the release's
`SHA256SUMS-1.6.5.txt`.
