# Third-party notices

PiperReader's original application code is licensed under AGPL-3.0-only; see
the project LICENSE. Third-party components retain their original licenses.
This file is a project-level summary, not a substitute for complete third-party
license texts. Public Windows release 1.6.5 includes the notices in `licenses/`
and provides matching application and dependency sources on its release page.
See SOURCE_CODE.md and `licenses/SOURCES.json` for exact source locations/hashes.

## Bundled runtime components

Version 1.6.5 uses PyMuPDF 1.28.2 with its explicitly recorded MuPDF 1.28.2
source archive. The audio library is built from unmodified libsndfile 1.2.2,
mpg123 1.33.7, and LAME 3.100 sources, with the build recipe included in the
application source. It retains WAV and MP3 support while omitting unused external
FLAC/Ogg/Vorbis/Opus codecs. These components' full notices and sources accompany
the release. The SoundFile Python wrapper retains its BSD license.

The packaged runtime includes Python and third-party libraries. Important
license families include:

- Piper TTS and phonemizer-fork: GPL-3.0-or-later
- PyMuPDF: AGPL-3.0 or a commercial Artifex license
- pystray: LGPL-3.0
- Kokoro ONNX, ONNX Runtime, NumPy, Pillow, lxml, pypdf, pywin32, and their
  dependencies: see the exact package versions and license texts in
  `licenses\INDEX.txt`

Bundled binary releases must include the applicable notices and provide access
to the corresponding source required by their component licenses. The build's
collected license index alone does not establish that these requirements are met.

## Tesseract tessdata_fast OCR models

- Model files: `eng.traineddata` and `ara.traineddata`
- Pinned revision: `87416418657359cb625c412a48b6e1d6d41c29bd`
- Source: https://github.com/tesseract-ocr/tessdata_fast
- License: Apache License 2.0

Piper Reader uses these files with PyMuPDF's local OCR support to recognize
English and Arabic text in scanned documents and supported images. OCR input and
recognized text remain on the computer and are not sent to an online OCR
service. The exact model sizes and SHA-256 hashes are recorded in the packaged
OCR model manifest.

## Microsoft online speech

Andrew, Ava, Emma, and Sonia use Microsoft's online voices through the
open-source `edge-tts` client (https://github.com/rany2/edge-tts). Their voice IDs
are `en-US-AndrewMultilingualNeural`, `en-US-AvaMultilingualNeural`,
`en-US-EmmaMultilingualNeural`, and `en-GB-SoniaNeural`, respectively. Reading
text selected for these voices is sent to Microsoft's speech service. The voices
require internet access; they are not offline models bundled with Piper Reader.
The installed runtime includes edge-tts, aiohttp, certifi, SoundFile/libsndfile,
and their required dependencies. Exact package versions and available license
texts are included in `licenses\INDEX.txt`. These client-library licenses do
not grant additional rights in Microsoft's service or voice models.

The speech client is pinned to upstream commit
`ba61d94442c18893f0c4d3c5ad253718b21ab93e` (October 13, 2026), which adds
Sec-MS-GEC v2 protocol compatibility. Its package metadata still reports 7.2.8.
The source archive is recorded in `requirements-online.txt` with SHA-256
`7cb0766016de59572cb8c7c31ed865b995034d8db7872199e8a83172f7e9a34d`.
The client is licensed under LGPL-3.0; its full upstream license is collected
into the installed `licenses\edge-tts-7.2.8` directory. Piper Reader uses the
upstream implementation without a custom token algorithm.

Pinned source: https://github.com/rany2/edge-tts/tree/ba61d94442c18893f0c4d3c5ad253718b21ab93e

## Kokoro model files

- Model files: `kokoro-v1.0.onnx` and `voices-v1.0.bin`
- Source release: https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0

The files download on first use and are verified against pinned SHA-256 hashes
before they replace local files.

Piper Reader downloads voice models only when a user selects them for the first
time. The application does not send reading text to these model hosts.

## Murya Piper Hausa TTS

- Model: `adab-tech/murya-piper-hausa-tts`
- Pinned revision: `7eccd91ec0dc154c789b778c924b89f59a0dbccb`
- Model license: MIT
- Model author: Adamu Danjuma Abubakar, ADAB-TECH Labs
- Source: https://huggingface.co/adab-tech/murya-piper-hausa-tts
- Training data attribution: Google WAXAL (`google/WaxalNLP`), whose source
  files are published under CC BY 4.0 / CC BY-SA 4.0 as described by the model
  card.

The downloaded ONNX and JSON files are checked against pinned SHA-256 hashes
before they replace the local voice files.

## Piper Arabic Kareem

- Model: `ar_JO-kareem-medium`
- Language: Arabic (Jordan)
- Source: https://huggingface.co/rhasspy/piper-voices/tree/main/ar/ar_JO/kareem/medium

This and the offered English Piper voices are downloaded from the immutable
`rhasspy/piper-voices` revision
`ea046e8458f6acd997706d6e6066a022b42f6fb1`. The application validates each
file's catalog size and checksum before replacing local files. Piper's model
documentation asks distributors to review the license of each voice/dataset;
the Kareem model card points to a training-data source without a clearly
detected license, so broad or commercial redistribution of the model itself is
not claimed here.
