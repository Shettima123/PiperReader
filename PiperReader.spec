# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6 onedir build for Piper Reader on 64-bit Windows."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


PROJECT_ROOT = Path(SPECPATH).resolve()
ICON_PATH = PROJECT_ROOT / "build_assets" / "piper_reader.ico"
VERSION_PATH = PROJECT_ROOT / "build_assets" / "version_info.txt"
MANIFEST_PATH = PROJECT_ROOT / "build_assets" / "piper_reader.manifest"
BRIDGE_TEMPLATE = PROJECT_ROOT / "build_assets" / "bridge-config.template.js"
EXTENSION_ROOT = PROJECT_ROOT / "browser_extension"
OCR_ROOT = PROJECT_ROOT / "build_assets" / "ocr"
OCR_TESSDATA_ROOT = OCR_ROOT / "tessdata"
OCR_MODEL_REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"

datas = []
binaries = []
hiddenimports = [
    "browser_bridge",
    "library_store",
    "document_outline",
    "edge_engine",
    "clipboard_speech",
    "speech_diagnostics",
    "ai_studio",
    "ai_studio_ui",
    "lesson_audio",
    "gemini_engine",
    "textbook_core",
    "textbook_audio",
    "textbook_studio",
    "fitz",
    "pymupdf",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32com.client",
    "win32com.client.dynamic",
    "win32timezone",
    "pystray._win32",
    "lxml.etree",
    "lxml.html",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageTk",
]

# Dynamic voice, ONNX, PDF, tray, and HTML-reader packages need their native
# libraries, data files, and lazily imported modules collected together.
for package in (
    "piper",
    "kokoro_onnx",
    "espeakng_loader",
    "phonemizer",
    "language_tags",
    "pathvalidate",
    "pystray",
    "pymupdf",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

for package in ("edge_tts", "aiohttp", "certifi", "soundfile"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("phonemizer")

# PyMuPDF already supplies the native local OCR engine. Ship only the pinned
# English and Arabic tessdata_fast recognition data plus its manifest/license.
for model_name in ("eng.traineddata", "ara.traineddata"):
    datas.append((str(OCR_TESSDATA_ROOT / model_name), "ocr/tessdata"))
datas += [
    (str(OCR_ROOT / "MODELS.json"), "ocr"),
    (
        str(OCR_ROOT / "LICENSE"),
        f"licenses/tessdata_fast-{OCR_MODEL_REVISION}",
    ),
]

# Preserve package metadata used by runtime feature/version checks.
for dist_name in (
    "piper-tts",
    "kokoro-onnx",
    "onnxruntime",
    "espeakng-loader",
    "phonemizer-fork",
    "pathvalidate",
    "flatbuffers",
    "protobuf",
    "numpy",
    "pywin32",
    "pystray",
    "pillow",
    "pymupdf",
    "pypdf",
    "lxml",
    "edge-tts",
    "soundfile",
    "aiohttp",
    "certifi",
):
    datas += copy_metadata(dist_name)

# Ship the unpacked browser extension, but never package a workstation token.
# The generated config is ignored by Git; always stage the safe template.
datas.append((str(BRIDGE_TEMPLATE), "browser_extension"))
for source in EXTENSION_ROOT.rglob("*"):
    if not source.is_file():
        continue
    relative = source.relative_to(EXTENSION_ROOT)
    if relative.as_posix() == "bridge-config.js":
        continue
    destination = Path("browser_extension") / relative.parent
    datas.append((str(source), str(destination)))

a = Analysis(
    [str(PROJECT_ROOT / "piper_reader.pyw")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "tensorflow", "pytest", "onnx",
        "pystray._appindicator", "pystray._darwin", "pystray._gtk",
        "pystray._xorg",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PiperReader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH),
    version=str(VERSION_PATH),
    manifest=str(MANIFEST_PATH),
    # Keep runtime internals tidy; the app copies the unpacked extension from
    # this resource area to its writable LocalAppData folder on first launch.
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PiperReader",
)
