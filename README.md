# PiperReader

PiperReader is a Windows desktop reader for documents, webpages, and copied text. It provides local speech voices, optional online voices, read-along highlighting, a document library, and tools for preparing spoken lessons and textbook readings.

## Version 1.6.4

The **Table of Contents** button above the reading area opens a navigation sidebar. PDF bookmarks retain their chapter and section hierarchy. PDFs without usable bookmarks show a page list. Web articles use their headings, and plain text recognizes explicit Markdown or Chapter headings.

Selecting an entry jumps to that location and pauses active document narration. Click document text to read from the new location. Hide the sidebar when you want the full reading width.

## Features

- Read PDF, Word, image, and text documents, with local English and Arabic OCR.
- Read copied text automatically, with a separately selected clipboard voice.
- Use Piper, Kokoro, and Windows voices locally. Piper and Kokoro models download when first selected.
- Use Microsoft online voices, including Andrew, Ava, Emma, and Sonia. These require internet access and send the selected text to Microsoft.
- Fall back to an already installed Lessac voice when Microsoft clipboard speech is unavailable and Offline backup is enabled.
- Keep a local library, adjust speed and volume, repeat passages, and export WAV audio.
- Prepare lecture, podcast, and study scripts in AI Studio using a separately installed and authenticated Codex CLI.
- Prepare complete textbook readings with optional explanations. Google Gemini narration requires the user's own API access; availability and billing depend on the Google project.

Online providers, voice availability, and account limits can change. The app does not include an AI subscription or a provider API key.

## Source setup

Use **64-bit Windows 10 or Windows 11** and **64-bit Python 3.12** with Tkinter. The pinned requirements record the environment used for the 1.6.4 build.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_tools\fetch_ocr_models.py
.\.venv\Scripts\python.exe piper_reader.pyw
```

The OCR helper downloads the two public recognition models from the immutable revision in `build_assets/ocr/MODELS.json`, checks their file size and SHA-256, and writes them to the build assets directory. Voice models, personal settings, API keys, and document libraries are not included in this repository.

Word conversion needs Microsoft Word or LibreOffice installed separately. AI Studio needs Codex installed separately. These applications are not bundled here.

## Tests and Windows packaging

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\build_installer.ps1 -Python .\.venv\Scripts\python.exe
```

Packaging additionally needs Inno Setup 6. Use `-InnoCompiler` if `ISCC.exe` is installed outside its normal per-user location. The script runs regression tests and a frozen-runtime check before generating the installer, standalone ZIP, and checksums in `release/`.

The 1.6.4 application passed 319 regression tests and was checked in an installed Windows build. Dependency installation on a clean machine is a separate verification step.

## Data and privacy

Installed versions keep settings, downloaded voices, the document library, and projects under `%LOCALAPPDATA%\PiperReader`. Local voices process speech on the computer after their model downloads. Online speech sends reading text to the selected provider. AI script generation sends source text to OpenAI through Codex. Gemini API keys can optionally be stored using Windows account encryption and are never placed in textbook projects.

The browser extension uses a local bridge credential generated per installation. The repository contains only a placeholder. Follow the installed app's instructions to load the generated extension copy.

## License

PiperReader's original application code, tests, build scripts, documentation, and artwork are licensed under **GNU Affero General Public License version 3 (AGPL-3.0-only)**. Copyright (C) 2026 Shettima123. See [LICENSE](LICENSE) for the full terms. The application is provided without warranty.

Third-party components retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This license does not grant rights to online services or downloaded voice models beyond their providers' terms.

## Downloads

The source for version 1.6.4 is available here. Windows installer and portable downloads will be added to [GitHub Releases](https://github.com/Shettima123/PiperReader/releases) after the bundled license and corresponding-source package is complete. Existing private builds are not part of this public source release.
