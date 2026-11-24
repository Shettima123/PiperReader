# Piper Reader Windows installer

This project builds a per-user, 64-bit Windows installer. It installs Piper
Reader without requiring Python on the destination computer. Voice models are
not bundled; each Piper or Kokoro model downloads only when that voice is first
selected. English and Arabic OCR language data are bundled so scanned documents
and supported image files can be recognized locally.

## Table of Contents (version 1.6.4)

Choose **Table of Contents** to open the document sidebar. Select a section to
jump to its location without starting audio. PDF documents use their bookmarks
when available; PDFs without usable bookmarks show a page list instead. Web
articles use headings, and plain text uses Markdown or Chapter headings when
present.

## Microsoft online voices

Version 1.4.7 adds Ava, Emma, and Sonia alongside Andrew:

| Voice menu label | Microsoft voice ID |
| --- | --- |
| Andrew (US male) - Microsoft, online | `en-US-AndrewMultilingualNeural` |
| Ava (US female) - Microsoft, online | `en-US-AvaMultilingualNeural` |
| Emma (US female) - Microsoft, online | `en-US-EmmaMultilingualNeural` |
| Sonia (UK female) - Microsoft, online | `en-GB-SoniaNeural` |

Select a voice in the Voice menu, then use Preview voice, Play, or Save to WAV
as usual. Speed and volume controls apply. Microsoft Emma is an American voice;
the existing British Emma is a separate local Kokoro voice.

These voices require an internet connection and send the selected reading text to
Microsoft's online speech service through the open-source `edge-tts` client.
No account, API key, subscription, or voice-model download is needed. Online
service availability may change. Piper and Kokoro remain local after their
initial model downloads. The clipboard voice is selected separately.

## Clipboard recovery (version 1.6.3)

For clipboard reading with a Microsoft voice, each speech chunk has a six-second
request budget. An empty response can be retried once within that same budget.
If the online request fails and the Lessac Piper voice is already installed,
reading continues with Lessac offline, starting with the chunk that failed. The
rest of that copied passage stays on Lessac. No words are intentionally skipped
or summarized, and the saved clipboard voice selection stays unchanged.

After a failure, new clipboard copies also use Lessac for five minutes, avoiding
repeated waits on the unavailable service. The next copy after this cooldown
tries the selected Microsoft voice again. The fallback never downloads a voice
during reading; if Lessac is unavailable, the app reports the online failure.
The six-second budget limits the online request; loading a local model and
preparing or playing audio can take additional time.

Copies remain in the order they arrived. New copies wait for the current reading
or studio playback to finish. Stop clears pending copies, and turning Auto-read
off discards them. Clipboard recovery does not change the voice or request
policy used for ordinary document reading, studio narration, or WAV exports.

Small local diagnostic records contain timing, queue wait, engine, playback
counts, fixed error categories, and HTTP status numbers. They never contain
copied text, document contents, URLs, API keys, or raw server responses. They are
stored in `%LOCALAPPDATA%\PiperReader\speech_diagnostics.jsonl`, with one rotated
backup named `speech_diagnostics.jsonl.1`; each file is limited to about 64 KiB.
Diagnostic files are not included in installers or standalone archives.

## AI Studio (version 1.5.0)

Open a document, then choose **AI Studio** in the header. The studio takes a
copy of the current text; the original document and Library item stay intact.
You can also paste material into its Source tab.

1. Choose Lecture, Podcast, or Study, an approximate length, and an audience.
2. Check the Codex connection. If needed, use **Sign in to Codex** and complete
   the official ChatGPT sign-in in your browser.
3. Generate a script and review/edit it in the Script tab.
4. Choose the narrator and podcast voices, then play or export the WAV audio.

Lecture uses one narrator. Podcast assigns Host and Guest their own voices.
Study includes review questions and pauses. Requested length is a writing
target; actual audio length depends on the script, pauses, and reading speed.
Speaker labels and pause markers control narration and are not spoken.

Script generation uses the locally installed Codex command-line runtime with
ChatGPT authentication and shares the account's included Codex usage limits.
The runtime is a separate prerequisite, not bundled in the Piper Reader
installer. API-key authentication is refused; there is no paid API fallback.
Piper Reader never stores an OpenAI password or token. Codex manages sign-in.
The source text is sent to OpenAI only when generating a script; Microsoft
receives the script text when rendering the selected online voices.

Scripts may contain mistakes; review explanations against the source before
relying on them. Generated examples are illustrative, not new source facts.
Save lesson projects to reopen the source, script, and settings together.
Projects default to `%LOCALAPPDATA%\PiperReader\ai_lessons` in the installed
app. They are not automatically added to the document Library.

Version 1.5.1 keeps clipboard auto-reading available while AI Studio is open,
generating a script, or preparing/exporting audio. Once a lesson is ready to
play, it waits for current reader speech to finish. New clipboard copies wait
in order until the lesson ends or is stopped. Closing Studio or a playback
error also releases them. The main reader's Stop button clears waiting copies;
turning Auto-read off discards them as well.

Official integration reference: https://learn.chatgpt.com/docs/non-interactive-mode

## Build

The build computer needs an isolated 64-bit Python 3.12 environment, the pinned
runtime/build packages, Inno Setup 6, and the verified native audio decoder.
Follow README.md to build that decoder from source with Visual Studio C++ tools
and copy its verified output into `build_assets/native_audio`. Then run:

```powershell
.\build_installer.ps1 -Python .\.venv\Scripts\python.exe
```

The build also needs `soundfile==0.13.1` and the pinned `edge-tts` revision in
`requirements-online.txt`; their networking dependencies and the SoundFile
decoder are bundled in the installed app. Install the speech client using the
same Python interpreter that runs the build:

```powershell
python -m pip install --force-reinstall --no-deps -r .\requirements-online.txt
python .\build_tools\check_online_dependencies.py
```

The `--no-deps` command preserves the build environment's already installed
runtime dependencies. On a new environment, install those runtime dependencies
first. The archive URL is pinned to upstream commit
`ba61d94442c18893f0c4d3c5ad253718b21ab93e` and verified by SHA-256. This October 13,
2026 upstream change supports Microsoft's v2 speech request tokens. The earlier
client returned HTTP 403 during synthesis. Both revisions report version 7.2.8,
so installing `edge-tts==7.2.8` alone does not supply the fix.

The offline build preflight checks the imported protocol source files against
the pinned revision. It rejects the old client before packaging and never sends
text to Microsoft. App construction and voice-readiness checks remain offline;
speech requests begin only when a reading or audio export is requested.

Upstream change: https://github.com/rany2/edge-tts/commit/ba61d94442c18893f0c4d3c5ad253718b21ab93e

The script creates `release\PiperReaderSetup-1.6.5.exe`,
`release\PiperReaderStandalone-1.6.5-x64.zip`, and
`release\SHA256SUMS-1.6.5.txt`. It recreates this project's `build` and `dist`
directories and keeps earlier release versions available for rollback.

## Install on another Windows computer

Copy `PiperReaderSetup-1.6.5.exe` to the other 64-bit Windows 10 or Windows 11
computer and run it. Administrator access is not required. The optional startup
task is checked by default and uses the current user's Windows Startup Apps
registry entry. The optional desktop shortcut is unchecked by default.

Alternatively, extract `PiperReaderStandalone-1.6.5-x64.zip` to a writable
folder and run `PiperReader.exe`. The portable archive contains the same local
OCR engine and English/Arabic recognition data as the installer build.

The installer is not code-signed, so Windows may identify its publisher as
unknown. Obtain the installer from the project's GitHub release and verify its
published SHA-256 checksum.

The application installs under:

```text
%LOCALAPPDATA%\Programs\PiperReader
```

Settings, the unique local browser-bridge credential, and downloaded voices are
stored under `%LOCALAPPDATA%\PiperReader`. Uninstalling the application preserves
that user data so downloaded voices are not lost during an upgrade or reinstall.

## Local Library

Choose **Library** in the header (or press **Ctrl+L**) to reopen imported PDF,
Word, image, and text files. A private managed copy is stored under
`%LOCALAPPDATA%\PiperReader\library`, so a Library item remains available even
if its original file is moved. Successfully opened files are added
automatically; typed or pasted text is added only when **Save current text** is
chosen. Clipboard auto-read content is never archived automatically. Removing
an item deletes only Piper Reader's managed copy and never changes the original
file.

## Memorization repeats

Use the compact **Repeat** controls beside Zoom to choose **Sentence**,
**Paragraph**, or **Passage**, then choose 2–20 total readings. For example,
Sentence with 7 times reads the first sentence seven times before moving to the
second. Passage repeats the entire current reading scope. Repeat settings apply
to documents, webpages, and clipboard reading, while voice previews and WAV
exports remain single-copy. Set Repeat to **Off** for normal playback.

## Reading speed

Use the large minus and plus buttons for exact 0.1x adjustments. Select the
center speed value to open the preset panel with 0.8x, 1.0x, 1.2x, 1.5x,
2.0x, and 2.5x shortcuts plus a fine-adjust slider. A changed speed applies to
the next reading request; audio that was already generated keeps its original
speed so clipboard order, repeats, and highlighting are not interrupted.

## Centered document pages

PDF, Word-converted, scanned, and image pages are centered in the available
document area whenever they fit. Pages that are wider than the window remain
scrollable. Page clicks, OCR word boxes, and read-along highlights all use the
same centered page origin, including after zooming or resizing the window.

## Word tracked changes

Word documents with tracked changes now open in **Final Showing Markup** style:
insertions, deletions, formatting revisions, and comments remain visible in the
original page view. Playback follows only the current/final text, so deleted
text and reviewer comments are not read aloud. Piper Reader converts an
isolated temporary copy and never accepts, rejects, saves, or closes the source
document, including when that document is already open in Microsoft Word.

## Chrome or Microsoft Edge extension

Start Piper Reader once so it can create the unique local bridge credential.
Then open `chrome://extensions` or `edge://extensions`, enable Developer mode,
choose **Load unpacked**, and select:

```text
%LOCALAPPDATA%\PiperReader\browser_extension
```

The installer never packages the build computer's bridge token. A placeholder
is embedded; on first start, Piper Reader copies the extension to the folder
above and replaces the placeholder with that installation's local credential.

## Textbook Studio (1.6.0)

Use **Textbook** beside AI Studio to prepare a complete reading or a complete
reading with separate teaching notes. Both modes retain the same imported source
text. Optional explanations are written with your existing ChatGPT/Codex
connection, saved after each completed batch, and can be switched off later.

Choose Google Gemini TTS for expressive narration, or explicitly select
Microsoft Andrew for the existing free online voice. Gemini requires your own
Google API access. Enter the key in the masked in-app dialog; saving it is opt-in
and uses Windows account encryption. It is never included in project files,
ordinary settings, or installers. Never put a key into a shared project.

Make a short sample before exporting a complete reading. Estimates use an
approximate 150 words per minute. Check Google's current model availability and
pricing for your project before synthesis. Estimates are not account spending
limits; input, output, explanations, and retakes may incur separate costs.
Free quotas depend on the Google account.

Projects save separately under `%LOCALAPPDATA%\PiperReader\textbooks`. Finished
speech clips are reused when text, voice, delivery and speed match. A failed
request is shown immediately, without an automatic billed retry. Stop preserves
completed clips, although stopping a request cannot reverse a provider charge.

Coverage verifies that every imported passage remains in the reading plan.
Review PDF extraction, tables, figures and equations before synthesis, and
listen to the generated audio to check pronunciation and omissions.

The clipboard reader continues while either studio prepares text or audio.
Studio playback waits for the current clipboard reading to finish; new clipboard
items wait until studio playback releases the speaker.

References: https://ai.google.dev/gemini-api/docs/speech-generation and
https://ai.google.dev/gemini-api/docs/pricing.

## Distribution status

Version 1.6.5 is the public Windows release. Its installer and portable ZIP
include the application LICENSE and NOTICE, third-party license texts, and
SOURCE_CODE.md with directions to the matching source archives. Share the
installer together with the GitHub release link so recipients can obtain those
sources and verify the published checksums. See README.md for the updated build
steps, including the required source-built native audio decoder.

Release: https://github.com/Shettima123/PiperReader/releases/tag/v1.6.5
