# Native audio library for Windows releases

PiperReader's public Windows build uses a source-built `libsndfile_x64.dll` in
place of the bundled DLL supplied by the SoundFile wheel. This gives the release
an explicit, small native source dependency set:

- libsndfile 1.2.2: built-in WAV/PCM formats and MP3 support.
- mpg123 1.33.7: MP3 decoder, linked statically into libsndfile.
- LAME 3.100: MP3 encoder required by libsndfile's MPEG configuration, linked
  statically into libsndfile.

FLAC, Ogg, Vorbis, Opus, experimental codecs, network input, command-line audio
players, and examples are disabled. PiperReader's WAV exports and Microsoft
voice MP3 decoding use the enabled formats. This build is not intended as a
general replacement for the full-format SoundFile wheel in other applications.

The upstream source archives and SHA256 digests are pinned in `sources.json`.
No upstream code patches are applied. Compiler options are provided to the
upstream build scripts. Python 3.12+, Visual Studio 2022 C++ x64 build tools,
CMake, and Ninja are required. Visual Studio's bundled CMake/Ninja are located
automatically; optional script parameters allow explicit paths.

```powershell
.\build_tools\native_audio\build.ps1 -WorkDirectory C:\build\piper-audio -Python python
python .\build_tools\native_audio\verify.py C:\build\piper-audio\output\libsndfile_x64.dll --report C:\build\piper-audio\output\verification.json
```

The build uses the x64 MSVC toolchain and static Microsoft C runtime (`/MT`).
The resulting library is `output/libsndfile_x64.dll`; full upstream license
texts are copied into `output/licenses/`. `output/build-manifest.json` records
the output checksum and build scope, and `output/dll-dependencies.txt` records
the dynamic dependencies reported by `dumpbin`.

`verify.py` imports the installed Python SoundFile wrapper in a fresh process
and directs it to the specified DLL, checking the actual function address to
guard against a library fallback. It exercises WAV PCM16/float and MP3
roundtrips at 24 kHz mono and 44.1 kHz stereo. An optional `--mp3-fixture` checks
independently encoded MP3 input. It neither edits nor replaces any Python
installation files.

## Packaging and source distribution

Run verification before copying the DLL into the isolated release environment's
`_soundfile_data` package. Do not replace a live user's library in place. Include
the license directory, this recipe, `sources.json`, and all three unmodified
source archives in the release's corresponding-source download. Preserve the
upstream notices in those source archives. The source and recipe allow recipients
to rebuild or modify the library and relink it with the application.

The three upstream libraries keep their LGPL licenses; the application license
does not replace them. See the complete `COPYING` files in the source archives
and output license folders. Included libsndfile codecs also retain their
ALAC Apache-2.0, GSM, and Sun G72x notices. The full Apache-2.0 text in
`licenses/Apache-2.0.txt` was retrieved from
<https://www.apache.org/licenses/LICENSE-2.0.txt>. The recipe links the Windows
system `shlwapi.lib` required by mpg123's path handling; no separate codec DLL
or Visual C++ runtime DLL is needed.
