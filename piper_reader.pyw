"""
Piper Reader - free natural-voice text-to-speech clipboard reader for Windows.
Works like the Zabaware TTS Reader: copy any text (Ctrl+C) and it reads it aloud.

Local voices run without internet after setup:
  - Piper  : instant, lightweight (~60 MB per voice)
  - Kokoro : most natural free voices (~330 MB one-time download, all voices)

Andrew, Ava, Emma, and Sonia are optional Microsoft online voices: spoken text
is sent to Microsoft and internet is required. No account or API key is needed.
Engines and local voice files install on first use. The optional webpage reader
also connects to the page address the user opens.
Run with:  pythonw piper_reader.pyw   (or double-click run.bat)
"""

import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import webbrowser
import zlib
import ctypes
import unicodedata
from collections import deque
from ctypes import wintypes


def _enable_windows_high_dpi():
    """Declare DPI awareness before Tk creates any native Windows handles."""
    if os.name != "nt":
        return "unsupported"
    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_context(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError, ValueError):
        pass
    try:
        # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        # Vista/Windows 7 fallback. This is still sharper than bitmap scaling.
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    # A packaged build can reach here when its manifest set awareness first.
    return "manifest-or-existing"


WINDOWS_DPI_MODE = "not-requested"

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog

from browser_bridge import BrowserBridge
from edge_engine import (
    ONLINE_VOICES, ONLINE_NOTICE, EdgeEngine, SpeechCancelled, OnlineSpeechError,
)
from clipboard_speech import ClipboardSpeechEngine
from speech_diagnostics import SpeechDiagnostics
from document_outline import pdf_outline, text_outline, web_outline
from library_store import LibraryError, LibraryStore

IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if IS_FROZEN else __file__))
RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = (os.path.join(
    os.environ.get("LOCALAPPDATA", APP_DIR), "PiperReader")
    if IS_FROZEN else APP_DIR)
VOICES_DIR = os.path.join(DATA_DIR, "voices")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
BROWSER_BRIDGE_CONFIG = os.path.join(DATA_DIR, "browser_bridge.json")
BUNDLED_BROWSER_EXTENSION_DIR = os.path.join(
    RESOURCE_DIR, "browser_extension")
BROWSER_EXTENSION_DIR = (os.path.join(DATA_DIR, "browser_extension")
                         if IS_FROZEN else BUNDLED_BROWSER_EXTENSION_DIR)
BROWSER_EXTENSION_ID = "inggjcimhmlkhfclodcihjpbhaddnlid"
APP_VERSION = "1.6.5"
CLIPBOARD_ONLINE_TIMEOUT = 6.0
CLIPBOARD_OFFLINE_COOLDOWN = 300.0
CLIPBOARD_BACKUP_VOICE_ID = "en_US-lessac-medium"
SPEED_MIN = 0.5
SPEED_MAX = 2.5
SPEED_FINE_STEP = 0.05
SPEED_BUTTON_STEP = 0.1
SPEED_PRESETS = (0.8, 1.0, 1.2, 1.5, 2.0, 2.5)
DOCUMENT_PAGE_MARGIN = 10.0
REPEAT_MODE_LABELS = {
    "off": "Off",
    "sentence": "Sentence",
    "paragraph": "Paragraph",
    "passage": "Passage",
}
REPEAT_LABEL_MODES = {
    label.casefold(): mode for mode, label in REPEAT_MODE_LABELS.items()
}
REPEAT_COUNT_MIN = 2
REPEAT_COUNT_MAX = 20
REPEAT_COUNT_DEFAULT = 7
# Larger logical units are re-synthesized per repetition so an hours-long
# passage cannot fill the temporary drive. Normal sentences/passages remain
# cached, which is especially useful for the slower natural Kokoro voices.
REPEAT_WAV_CACHE_MAX_BYTES = 64 * 1024 * 1024
# Heart/Kokoro can synthesize a roughly 70-character opening in about a second
# on the reference laptop.  The remaining text is prepared one block ahead.
KOKORO_INITIAL_CHUNK_LEN = 72
# Re-prime only when the user returns after a long idle period or Windows has
# plausibly resumed.  The existing clipboard timer performs the inexpensive
# check; inference is never run on a repeating timer.
RUNTIME_PROBE_INTERVAL_SECONDS = 5.0
RUNTIME_RESUME_GAP_SECONDS = 30.0
RUNTIME_USER_IDLE_SECONDS = 10 * 60.0
RUNTIME_USER_ACTIVE_SECONDS = 15.0
RUNTIME_REPRIME_GRACE_SECONDS = 12.0
RUNTIME_REPRIME_QUIET_SECONDS = 3.0
RUNTIME_REPRIME_COOLDOWN_SECONDS = 5 * 60.0
SENTENCE_END_CHARS = ".!?\u2026\u061f\u06d4"
SENTENCE_CLOSERS = "\"'\u2019\u201d\u00bb)]}"
INSTANCE_MUTEX_NAME = r"Local\PiperReader_019fc50746427f718848fb9a4f039b82"
INSTANCE_COMMAND_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", DATA_DIR), "PiperReader")
INSTANCE_COMMAND_FILE = os.path.join(
    INSTANCE_COMMAND_DIR, "instance-command.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WEB_WIRE_LIMIT = 3 * 1024 * 1024
WEB_CONTENT_LIMIT = 8 * 1024 * 1024
WEB_TEXT_LIMIT = 250000
WEB_BLOCK_LIMIT = 2000
WEB_CHUNK_LIMIT = 5000
WEB_REQUEST_TIMEOUT = 8
WEB_TOTAL_TIMEOUT = 20
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 PiperReader/1.0"
)
OCR_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
}
OCR_DPI = 200
OCR_MIN_DPI = 72
OCR_MAX_RENDER_PIXELS = 24_000_000
OCR_IMAGE_PAGE_MAX_POINTS = 1440.0
OCR_NATIVE_TEXT_MIN_WORDS = 2
OCR_NATIVE_TEXT_MIN_CHARS = 12
OCR_AUTO_CONFIRM_PAGES = 20
OCR_MAX_PAGES_PER_JOB = 500
OCR_MAX_IMAGE_FILE_BYTES = 500 * 1024 * 1024
OCR_TESSDATA_REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"
OCR_TESSDATA_DIR = os.path.join(DATA_DIR, "ocr", "tessdata")
BUNDLED_OCR_TESSDATA_DIR = os.path.join(
    RESOURCE_DIR, "ocr", "tessdata") if IS_FROZEN else os.path.join(
        RESOURCE_DIR, "build_assets", "ocr", "tessdata")
OCR_MODE_LABELS = {
    "auto": "Auto (match voice)",
    "english": "English",
    "arabic": "Arabic + English",
    "hausa": "Hausa (basic Latin fallback)",
}
OCR_LABEL_MODES = {
    label.casefold(): mode for mode, label in OCR_MODE_LABELS.items()
}
# Immutable model metadata from tesseract-ocr/tessdata_fast. English and
# Arabic ship with the installer. Tesseract has no validated Hausa model, so
# the Hausa UI choice is an honestly labelled English/basic-Latin fallback.
OCR_MODEL_FILES = {
    "eng": {
        "filename": "eng.traineddata",
        "url": (
            "https://raw.githubusercontent.com/tesseract-ocr/"
            f"tessdata_fast/{OCR_TESSDATA_REVISION}/eng.traineddata"
        ),
        "size": 4113088,
        "sha256": (
            "7d4322bd2a7749724879683fc3912cb5"
            "42f19906c83bcc1a52132556427170b2"
        ),
        "label": "English OCR data (~4 MB)",
    },
    "ara": {
        "filename": "ara.traineddata",
        "url": (
            "https://raw.githubusercontent.com/tesseract-ocr/"
            f"tessdata_fast/{OCR_TESSDATA_REVISION}/ara.traineddata"
        ),
        "size": 1432056,
        "sha256": (
            "e3206d3dc87fd50c24a0fb9f0183861"
            "5911d25168f4e64415244b67d2bb3e729"
        ),
        "label": "Arabic OCR data (~1.4 MB)",
    },
}


def _replace_text_file(path, text):
    """Atomically replace a small UTF-8 configuration file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _sync_browser_extension():
    """Refresh the writable unpacked extension from packaged resources."""
    if not IS_FROZEN:
        return
    source = BUNDLED_BROWSER_EXTENSION_DIR
    if not os.path.isdir(source):
        return
    os.makedirs(BROWSER_EXTENSION_DIR, exist_ok=True)
    for source_root, _dirs, files in os.walk(source):
        relative = os.path.relpath(source_root, source)
        destination_root = (BROWSER_EXTENSION_DIR if relative == "." else
                            os.path.join(BROWSER_EXTENSION_DIR, relative))
        os.makedirs(destination_root, exist_ok=True)
        for filename in files:
            # This file contains a per-install secret and is generated below.
            if filename.casefold() == "bridge-config.js":
                continue
            source_path = os.path.join(source_root, filename)
            destination_path = os.path.join(destination_root, filename)
            try:
                with open(source_path, "rb") as source_stream:
                    source_bytes = source_stream.read()
                destination_bytes = None
                try:
                    with open(destination_path, "rb") as destination_stream:
                        destination_bytes = destination_stream.read()
                except OSError:
                    pass
                if source_bytes != destination_bytes:
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                    temporary = f"{destination_path}.tmp-{os.getpid()}"
                    try:
                        with open(temporary, "wb") as destination_stream:
                            destination_stream.write(source_bytes)
                        os.replace(temporary, destination_path)
                    finally:
                        try:
                            os.remove(temporary)
                        except OSError:
                            pass
            except OSError:
                # The app itself remains usable if extension staging fails.
                continue


def ensure_browser_bridge_config():
    """Create a unique local bridge credential for this installation."""
    _sync_browser_extension()
    expected_origins = [
        f"chrome-extension://{BROWSER_EXTENSION_ID}",
        f"extension://{BROWSER_EXTENSION_ID}",
    ]
    config = None
    try:
        with open(BROWSER_BRIDGE_CONFIG, encoding="utf-8") as stream:
            candidate = json.load(stream)
        origins = candidate.get("extension_origins")
        if (int(candidate.get("port", 0)) == 57638 and
                isinstance(candidate.get("token"), str) and
                len(candidate["token"]) >= 32 and
                origins == expected_origins):
            config = candidate
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    if config is None:
        config = {
            "port": 57638,
            "token": secrets.token_urlsafe(32),
            "extension_origins": expected_origins,
        }
        _replace_text_file(
            BROWSER_BRIDGE_CONFIG,
            json.dumps(config, indent=2) + "\n")

    extension_config = os.path.join(
        BROWSER_EXTENSION_DIR, "bridge-config.js")
    if os.path.isdir(BROWSER_EXTENSION_DIR):
        base_url = f"http://127.0.0.1:{int(config['port'])}"
        script = (
            "// Generated locally by Piper Reader. Do not share this file.\n"
            "globalThis.PIPER_BRIDGE_CONFIG = Object.freeze({\n"
            f"  baseUrl: {json.dumps(base_url)},\n"
            f"  token: {json.dumps(config['token'])}\n"
            "});\n"
        )
        try:
            current = ""
            try:
                with open(extension_config, encoding="utf-8") as stream:
                    current = stream.read()
            except OSError:
                pass
            if current != script:
                _replace_text_file(extension_config, script)
        except OSError:
            # The desktop reader remains usable if an externally managed
            # extension folder is read-only.
            pass
    return BROWSER_BRIDGE_CONFIG


_INSTANCE_MUTEX_HANDLE = None


def _send_instance_command(command):
    """Ask the already-running current-user instance to act."""
    try:
        payload = {
            "command": command,
            "created": time.time(),
            "pid": os.getpid(),
        }
        _replace_text_file(
            INSTANCE_COMMAND_FILE, json.dumps(payload) + "\n")
        return True
    except OSError:
        return False


def acquire_single_instance(start_hidden=False, shutdown_existing=False):
    """Allow one clipboard/tray reader per Windows sign-in session."""
    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        # A mutex failure should not prevent the reader from launching.
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        if shutdown_existing:
            _send_instance_command("exit")
        elif not start_hidden:
            if not _send_instance_command("show"):
                # Best-effort fallback for an older build without polling.
                hwnd = ctypes.windll.user32.FindWindowW(
                    None, "Piper Reader - Free Natural TTS")
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
        return False
    _INSTANCE_MUTEX_HANDLE = handle
    try:
        os.remove(INSTANCE_COMMAND_FILE)
    except OSError:
        pass
    return True


def release_single_instance():
    global _INSTANCE_MUTEX_HANDLE
    handle, _INSTANCE_MUTEX_HANDLE = _INSTANCE_MUTEX_HANDLE, None
    if handle and os.name == "nt":
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass

# ----------------------------------------------------------- premium UI theme
# Centralized design tokens keep the native Tkinter UI visually consistent.
APP_BG = "#edf3ff"
APP_BG_TOP = "#f7faff"
CARD = "#ffffff"
CARD_SOFT = "#f8fafe"
CARD_BLUE = "#f1f5ff"
CARD_LINE = "#d8e2f1"
CARD_LINE_STRONG = "#c7d5ea"
SHADOW = "#dce6f5"
INK = "#14213d"
INK_SOFT = "#43536f"
INK_MUTED = "#7888a3"
ACCENT = "#3c63f3"
ACCENT_DARK = "#3156dd"
ACCENT_PRESSED = "#2949c5"
ACCENT_SOFT = "#eaf0ff"
ACCENT_LIGHT = "#8baaff"
BTN_LINE = "#d8e2f1"
BTN_HOVER = "#f1f5ff"
TRACK = "#dce5f2"
SUCCESS = "#22c55e"
DANGER = "#d94a62"
FALLBACK_BG = APP_BG
MAGIC = APP_BG              # retained for compatibility with old patch scripts

FONT_UI = "Segoe UI"
FONT_TITLE = "Georgia"


def display_scale_from_dpi(dpi):
    """Return a bounded physical-pixel scale for a Windows monitor DPI."""
    try:
        dpi = float(dpi)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(dpi) or dpi <= 0:
        return 1.0
    return max(0.75, min(4.0, dpi / 96.0))


def pdf_fit_zoom(page_width, dpi, target_logical_width=660.0):
    """Fit a PDF page at native monitor pixels instead of OS stretching it."""
    try:
        page_width = float(page_width)
    except (TypeError, ValueError):
        page_width = 1.0
    page_width = max(page_width, 1.0)
    target_pixels = float(target_logical_width) * display_scale_from_dpi(dpi)
    return max(0.15, min(4.0, target_pixels / page_width))


def get_window_dpi(widget=None):
    """Return the actual DPI of a Tk top-level, with portable fallbacks."""
    if os.name == "nt" and widget is not None:
        try:
            toplevel = widget.winfo_toplevel()
            toplevel.update_idletasks()
            user32 = ctypes.windll.user32
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            get_parent = user32.GetParent
            get_parent.argtypes = [wintypes.HWND]
            get_parent.restype = wintypes.HWND
            child = wintypes.HWND(toplevel.winfo_id())
            hwnd = get_parent(child) or child
            dpi = int(get_dpi(hwnd))
            if dpi > 0:
                return float(dpi)
        except (AttributeError, OSError, tk.TclError, ValueError):
            pass
    if widget is not None:
        try:
            dpi = float(widget.winfo_fpixels("1i"))
            if math.isfinite(dpi) and dpi > 0:
                return dpi
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass
    if os.name == "nt":
        try:
            dpi = int(ctypes.windll.user32.GetDpiForSystem())
            if dpi > 0:
                return float(dpi)
        except (AttributeError, OSError, ValueError):
            pass
    return 96.0


def scaled_ui_pixels(value, dpi):
    """Scale fixed window dimensions so DPI awareness preserves physical size."""
    return max(1, int(round(float(value) * display_scale_from_dpi(dpi))))


def place_window_in_workarea(root, width, height):
    """Size the window in physical pixels and keep it above the taskbar."""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        width = min(width, max(900, work_w - 20))
        height = min(height, max(650, work_h - 40))
        x = rect.left + max(0, (work_w - width) // 2)
        root.geometry(f"{width}x{height}+{x}+{rect.top}")
    except Exception:
        root.geometry(f"{width}x{height}")


def enable_acrylic(root):
    """Turn the window background into white frosted glass (Windows 10/11)."""
    try:
        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int),
                        ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_uint),
                        ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        set_wca = ctypes.windll.user32.SetWindowCompositionAttribute
        # 0xAABBGGRR - strong white frost
        for state, tint in ((4, 0x73FFFFFF), (3, 0x73FFFFFF)):
            accent = ACCENTPOLICY(state, 2, tint, 0)
            data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(accent),
                                                   ctypes.c_void_p),
                                   ctypes.sizeof(accent))
            if set_wca(hwnd, ctypes.byref(data)):
                return True
        return False
    except Exception:
        return False


def _round_pts(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class GlassCard(tk.Canvas):
    """Rounded elevated surface used for the application's major cards."""

    def __init__(self, master, frost, radius=20, pad=18, fill=CARD,
                 outline=CARD_LINE, shadow=True, **kw):
        super().__init__(master, bg=frost, highlightthickness=0, bd=0, **kw)
        self.radius, self.pad = radius, pad
        self.fill, self.outline, self.shadow = fill, outline, shadow
        self.inner = tk.Frame(self, bg=fill)
        self._win = None
        self.bind("<Configure>", self._redraw)

    def set_frost(self, color):
        self.configure(bg=color)

    def _redraw(self, e=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        self.delete("surface")
        bottom = 6 if self.shadow else 2
        if self.shadow:
            self.create_polygon(
                _round_pts(4, 5, w - 2, h - 2, self.radius), smooth=True,
                fill=SHADOW, outline="", tags="surface")
        self.create_polygon(
            _round_pts(2, 2, w - 4, h - bottom, self.radius), smooth=True,
            fill=self.fill, outline=self.outline, width=1, tags="surface")
        if self._win is None:
            self._win = self.create_window(0, 0, window=self.inner, anchor="nw")
        inset = self.pad + 2
        self.coords(self._win, inset, inset)
        self.itemconfigure(self._win, width=max(1, w - 2 * inset - 2),
                           height=max(1, h - 2 * inset - bottom + 2))
        self.tag_lower("surface")


class PillButton(tk.Canvas):
    """Rounded 'liquid glass' pill button."""

    def __init__(self, master, text, command, frost, primary=False, height=36):
        import tkinter.font as tkfont
        self.font = tkfont.Font(family="Segoe UI Semibold", size=10)
        w = self.font.measure(text) + 40
        super().__init__(master, width=w, height=height, bg=frost,
                         highlightthickness=0, bd=0)
        self.text, self.command, self.primary = text, command, primary
        self.hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set_frost(self, color):
        self.configure(bg=color)
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self.configure(cursor="hand2" if v else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        r = (h - 4) // 2
        if self.primary:
            fill = ACCENT_DARK if self.hover else ACCENT
            outline, txt = fill, "#ffffff"
        else:
            fill = BTN_HOVER if self.hover else CARD
            outline, txt = BTN_LINE, INK
        self.create_polygon(_round_pts(2, 2, w - 2, h - 2, r), smooth=True,
                            fill=fill, outline=outline, width=1)
        self.create_text(w // 2, h // 2, text=self.text, fill=txt,
                         font=self.font)


def draw_icon(canvas, kind, cx, cy, color=ACCENT, scale=1.0):
    """Draw a small, dependency-free line icon on a Tk canvas."""
    s = scale
    line = max(1, round(1.8 * s))

    if kind == "feather":
        canvas.create_line(cx - 7*s, cy + 11*s, cx + 7*s, cy - 11*s,
                           fill=color, width=line, smooth=True)
        canvas.create_line(cx - 5*s, cy + 7*s, cx - 8*s, cy + 2*s,
                           cx + 4*s, cy - 9*s, fill=color, width=line,
                           smooth=True)
        canvas.create_line(cx - 3*s, cy + 4*s, cx - 6*s, cy - 1*s,
                           cx + 6*s, cy - 10*s, fill=color, width=line,
                           smooth=True)
        canvas.create_line(cx - 1*s, cy + 1*s, cx + 8*s, cy - 5*s,
                           fill=color, width=line)
    elif kind == "wave":
        heights = (7, 13, 19, 12, 6)
        for i, height in enumerate(heights):
            x = cx + (i - 2) * 5*s
            canvas.create_line(x, cy - height*s/2, x, cy + height*s/2,
                               fill=color, width=line, capstyle="round")
    elif kind == "play":
        canvas.create_polygon(cx - 5*s, cy - 8*s, cx + 8*s, cy,
                              cx - 5*s, cy + 8*s, fill="", outline=color,
                              width=line)
    elif kind == "pause":
        canvas.create_rectangle(cx - 7*s, cy - 9*s, cx - 2*s, cy + 9*s,
                                fill=color, outline=color)
        canvas.create_rectangle(cx + 2*s, cy - 9*s, cx + 7*s, cy + 9*s,
                                fill=color, outline=color)
    elif kind in ("zoom_in", "zoom_out"):
        canvas.create_oval(cx - 9*s, cy - 9*s, cx + 5*s, cy + 5*s,
                           fill="", outline=color, width=line)
        canvas.create_line(cx + 3*s, cy + 3*s, cx + 10*s, cy + 10*s,
                           fill=color, width=line, capstyle="round")
        canvas.create_line(cx - 5*s, cy - 2*s, cx + 1*s, cy - 2*s,
                           fill=color, width=line, capstyle="round")
        if kind == "zoom_in":
            canvas.create_line(cx - 2*s, cy - 5*s, cx - 2*s, cy + 1*s,
                               fill=color, width=line, capstyle="round")
    elif kind == "folder":
        canvas.create_line(cx - 12*s, cy - 7*s, cx - 3*s, cy - 7*s,
                           cx + 1*s, cy - 3*s, cx + 12*s, cy - 3*s,
                           cx + 10*s, cy + 9*s, cx - 12*s, cy + 9*s,
                           cx - 12*s, cy - 7*s, fill=color, width=line,
                           joinstyle="round")
        canvas.create_line(cx - 11*s, cy - 1*s, cx + 11*s, cy - 1*s,
                           fill=color, width=line)
    elif kind == "document":
        canvas.create_polygon(cx - 9*s, cy - 12*s, cx + 4*s, cy - 12*s,
                              cx + 10*s, cy - 6*s, cx + 10*s, cy + 12*s,
                              cx - 9*s, cy + 12*s, fill="", outline=color,
                              width=line)
        canvas.create_line(cx + 4*s, cy - 12*s, cx + 4*s, cy - 6*s,
                           cx + 10*s, cy - 6*s, fill=color, width=line)
        for off in (-2, 3, 8):
            canvas.create_line(cx - 5*s, cy + off*s, cx + 6*s,
                               cy + off*s, fill=color, width=line)
    elif kind == "clipboard":
        canvas.create_rectangle(cx - 9*s, cy - 9*s, cx + 9*s, cy + 12*s,
                                outline=color, width=line)
        canvas.create_rectangle(cx - 4*s, cy - 12*s, cx + 4*s, cy - 7*s,
                                outline=color, width=line)
        canvas.create_line(cx - 5*s, cy + 1*s, cx - 1*s, cy + 5*s,
                           cx + 6*s, cy - 3*s, fill=color, width=line,
                           joinstyle="round")
    elif kind == "stop":
        canvas.create_rectangle(cx - 9*s, cy - 9*s, cx + 9*s, cy + 9*s,
                                outline=color, width=line)
        canvas.create_rectangle(cx - 5*s, cy - 5*s, cx + 5*s, cy + 5*s,
                                fill=color, outline=color)
    elif kind == "speaker":
        canvas.create_polygon(cx - 11*s, cy - 4*s, cx - 6*s, cy - 4*s,
                              cx, cy - 10*s, cx, cy + 10*s,
                              cx - 6*s, cy + 4*s, cx - 11*s, cy + 4*s,
                              fill="", outline=color, width=line)
        canvas.create_arc(cx - 3*s, cy - 9*s, cx + 12*s, cy + 9*s,
                          start=-48, extent=96, style="arc", outline=color,
                          width=line)
    elif kind == "speaker_loud":
        draw_icon(canvas, "speaker", cx - 3*s, cy, color, scale)
        canvas.create_arc(cx - 5*s, cy - 13*s, cx + 18*s, cy + 13*s,
                          start=-47, extent=94, style="arc", outline=color,
                          width=line)
    elif kind == "slow":
        canvas.create_oval(cx - 8*s, cy - 5*s, cx + 5*s, cy + 5*s,
                           outline=color, width=line)
        canvas.create_oval(cx + 4*s, cy - 3*s, cx + 9*s, cy + 2*s,
                           outline=color, width=line)
        canvas.create_line(cx - 5*s, cy + 5*s, cx - 7*s, cy + 8*s,
                           cx, cy + 5*s, cx + 2*s, cy + 8*s,
                           fill=color, width=line)
    elif kind == "fast":
        canvas.create_oval(cx - 5*s, cy - 4*s, cx + 7*s, cy + 7*s,
                           outline=color, width=line)
        canvas.create_line(cx - 3*s, cy - 4*s, cx - 7*s, cy - 11*s,
                           cx - 1*s, cy - 5*s, cx + 2*s, cy - 12*s,
                           cx + 4*s, cy - 4*s, fill=color, width=line,
                           joinstyle="round")
        canvas.create_line(cx - 10*s, cy + 1*s, cx - 5*s, cy + 1*s,
                           fill=color, width=line)
    elif kind == "sparkle":
        for ox, oy, size in ((0, 0, 9), (9, -8, 4), (-8, 8, 4)):
            x, y, z = cx + ox*s, cy + oy*s, size*s
            canvas.create_line(x, y - z, x, y + z, fill=color, width=line)
            canvas.create_line(x - z, y, x + z, y, fill=color, width=line)
    elif kind == "globe":
        canvas.create_oval(cx - 11*s, cy - 11*s, cx + 11*s, cy + 11*s,
                           outline=color, width=line)
        canvas.create_arc(cx - 6*s, cy - 11*s, cx + 6*s, cy + 11*s,
                          start=90, extent=180, style="arc", outline=color,
                          width=line)
        canvas.create_arc(cx - 6*s, cy - 11*s, cx + 6*s, cy + 11*s,
                          start=-90, extent=180, style="arc", outline=color,
                          width=line)
        canvas.create_line(cx - 10*s, cy, cx + 10*s, cy, fill=color,
                           width=line)
        canvas.create_arc(cx - 10*s, cy - 6*s, cx + 10*s, cy + 6*s,
                          start=0, extent=180, style="arc", outline=color,
                          width=line)
        canvas.create_arc(cx - 10*s, cy - 6*s, cx + 10*s, cy + 6*s,
                          start=180, extent=180, style="arc", outline=color,
                          width=line)


class LogoBadge(tk.Canvas):
    def __init__(self, master, bg):
        super().__init__(master, width=58, height=58, bg=bg,
                         highlightthickness=0, bd=0)
        self.bind("<Configure>", self._draw)

    def _draw(self, _event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.create_polygon(_round_pts(3, 5, w - 2, h - 2, 15), smooth=True,
                            fill=SHADOW, outline="")
        self.create_polygon(_round_pts(2, 2, w - 4, h - 5, 15), smooth=True,
                            fill=CARD, outline=CARD_LINE, width=1)
        draw_icon(self, "feather", w/2, h/2 - 1, ACCENT_DARK, 1.15)


class SurfaceButton(tk.Canvas):
    """Premium horizontal button used for the voice preview action."""

    def __init__(self, master, text, command, bg, icon="play", width=220,
                 height=58):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2")
        self.text, self.command, self.icon = text, command, icon
        self.hover = self.pressed = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)

    def _set_hover(self, value):
        self.hover = value
        self._draw()

    def _press(self, _event):
        self.focus_set()
        self.pressed = True
        self._draw()

    def _release(self, event):
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if was_pressed and 0 <= event.x <= self.winfo_width() \
                and 0 <= event.y <= self.winfo_height():
            self.command()

    def _draw(self, _event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        fill = ACCENT_SOFT if self.hover else CARD
        if self.pressed:
            fill = "#e1e9ff"
        outline = ACCENT_LIGHT if self.focus_get() == self else CARD_LINE
        if not self.pressed:
            self.create_polygon(_round_pts(4, 5, w - 2, h - 2, 14),
                                smooth=True, fill=SHADOW, outline="")
        self.create_polygon(_round_pts(2, 2, w - 4, h - 5, 14), smooth=True,
                            fill=fill, outline=outline, width=1)
        self.create_oval(23, h/2 - 17, 57, h/2 + 17,
                         fill=ACCENT_SOFT, outline="")
        draw_icon(self, self.icon, 40, h/2, ACCENT, 0.9)
        self.create_text(70, h/2, text=self.text, anchor="w", fill=INK,
                         font=(FONT_UI, 12, "bold"))


class CompactControlButton(tk.Canvas):
    """Small premium icon button for controls inside the reading surface."""

    def __init__(self, master, icon, command, bg=CARD, width=38, height=34):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2")
        self.icon, self.command = icon, command
        self.hover = self.pressed = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _press(self, _event):
        self.focus_set()
        self.pressed = True
        self._draw()

    def _release(self, event):
        invoke = (self.pressed and 0 <= event.x <= self.winfo_width()
                  and 0 <= event.y <= self.winfo_height())
        self.pressed = False
        self._draw()
        if invoke:
            self.command()

    def _draw(self, _event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        fill = "#e2eaff" if self.pressed else (
            ACCENT_SOFT if self.hover else CARD_SOFT)
        outline = ACCENT_LIGHT if self.focus_get() == self else CARD_LINE
        self.create_polygon(_round_pts(2, 2, w - 2, h - 2, 10), smooth=True,
                            fill=fill, outline=outline, width=1)
        draw_icon(self, self.icon, w / 2, h / 2, ACCENT_DARK, 0.78)


class PremiumSlider(tk.Canvas):
    """Custom slider with a live value pill and vector endpoint icons."""

    def __init__(self, master, variable, from_, to, formatter, bg=CARD,
                 start_icon="slow", end_icon="fast"):
        super().__init__(master, height=70, bg=bg, highlightthickness=0,
                         bd=0, takefocus=1, cursor="hand2")
        self.variable, self.minimum, self.maximum = variable, from_, to
        self.formatter = formatter
        self.start_icon, self.end_icon = start_icon, end_icon
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", self._set_from_event)
        self.bind("<B1-Motion>", self._set_from_event)
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<Home>", lambda e: self._set_value(self.minimum))
        self.bind("<End>", lambda e: self._set_value(self.maximum))
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self.variable.trace_add("write", lambda *_: self._draw())

    def _bounds(self):
        return 35, max(36, self.winfo_width() - 35)

    def _set_value(self, value):
        self.variable.set(max(self.minimum, min(self.maximum, value)))
        self._draw()

    def _nudge(self, direction):
        self._set_value(self.variable.get() +
                        direction * (self.maximum - self.minimum) / 30)

    def _set_from_event(self, event):
        self.focus_set()
        x0, x1 = self._bounds()
        ratio = max(0.0, min(1.0, (event.x - x0) / max(1, x1 - x0)))
        self._set_value(self.minimum + ratio * (self.maximum - self.minimum))

    def _draw(self, _event=None):
        self.delete("all")
        w = self.winfo_width()
        if w < 80:
            return
        value = float(self.variable.get())
        ratio = (value - self.minimum) / max(0.0001,
                                             self.maximum - self.minimum)
        x0, x1 = self._bounds()
        x = x0 + ratio * (x1 - x0)
        y = 47
        if self.focus_get() == self:
            self.create_polygon(_round_pts(1, 1, w - 1, 68, 10), smooth=True,
                                fill="", outline=ACCENT_LIGHT, width=1)
        self.create_line(x0, y, x1, y, fill=TRACK, width=5,
                         capstyle="round")
        self.create_line(x0, y, x, y, fill=ACCENT, width=5,
                         capstyle="round")
        self.create_oval(x - 11, y - 9, x + 11, y + 13,
                         fill=SHADOW, outline="")
        self.create_oval(x - 10, y - 11, x + 10, y + 9,
                         fill=CARD, outline=CARD_LINE_STRONG, width=1)
        label = self.formatter(value)
        pill_w = max(52, 11 + len(label) * 7)
        left = max(2, min(w - pill_w - 2, x - pill_w/2))
        self.create_polygon(_round_pts(left + 1, 4, left + pill_w + 1, 31, 9),
                            smooth=True, fill=SHADOW, outline="")
        self.create_polygon(_round_pts(left, 2, left + pill_w, 29, 9),
                            smooth=True, fill=CARD, outline=CARD_LINE, width=1)
        self.create_text(left + pill_w/2, 15, text=label, fill=INK,
                         font=(FONT_UI, 10))
        draw_icon(self, self.start_icon, 16, y, INK_SOFT, 0.75)
        draw_icon(self, self.end_icon, w - 16, y, INK_SOFT, 0.75)


class ToggleTile(tk.Canvas):
    def __init__(self, master, variable, bg=CARD, width=220, height=72):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2")
        self.variable = variable
        self.hover = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self._toggle())
        self.bind("<Return>", lambda e: self._toggle())
        self.bind("<space>", lambda e: self._toggle())
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self.variable.trace_add("write", lambda *_: self._draw())

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _toggle(self):
        self.focus_set()
        self.variable.set(not self.variable.get())

    def _draw(self, _event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        fill = ACCENT_SOFT if self.hover else CARD_SOFT
        outline = ACCENT_LIGHT if self.focus_get() == self else CARD_LINE
        self.create_polygon(_round_pts(3, 4, w - 2, h - 2, 14), smooth=True,
                            fill=SHADOW, outline="")
        self.create_polygon(_round_pts(2, 2, w - 4, h - 5, 14), smooth=True,
                            fill=fill, outline=outline, width=1)
        checked = bool(self.variable.get())
        self.create_polygon(_round_pts(16, 19, 38, 41, 6), smooth=True,
                            fill=ACCENT if checked else CARD,
                            outline=ACCENT if checked else CARD_LINE_STRONG,
                            width=1)
        if checked:
            self.create_line(21, 30, 26, 35, 34, 25, fill=CARD, width=2,
                             joinstyle="round")
        self.create_text(50, 25, text="Auto-read clipboard", anchor="w",
                         fill=INK, font=(FONT_UI, 11, "bold"))
        self.create_text(50, 47, text="Read new clipboard content", anchor="w",
                         fill=INK_MUTED, font=(FONT_UI, 9))


class ActionTile(tk.Canvas):
    """Large action card with consistent vector icon and supporting text."""

    def __init__(self, master, title, subtitle, icon, command, bg=APP_BG,
                 primary=False):
        super().__init__(master, width=190, height=100, bg=bg,
                         highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2")
        self.title, self.subtitle, self.icon = title, subtitle, icon
        self.command, self.primary = command, primary
        self.hover = self.pressed = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)

    def set_content(self, title=None, subtitle=None, icon=None):
        """Update a tile's visible state without rebuilding the action bar."""
        if title is not None:
            self.title = title
        if subtitle is not None:
            self.subtitle = subtitle
        if icon is not None:
            self.icon = icon
        self._draw()

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _press(self, _event):
        self.focus_set()
        self.pressed = True
        self._draw()

    def _release(self, event):
        invoke = (self.pressed and 0 <= event.x <= self.winfo_width()
                  and 0 <= event.y <= self.winfo_height())
        self.pressed = False
        self._draw()
        if invoke:
            self.command()

    def _draw(self, _event=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        if self.primary:
            fill = ACCENT_PRESSED if self.pressed else (
                ACCENT_DARK if self.hover else ACCENT)
            text_color, sub_color = CARD, "#dce5ff"
            outline, icon_bg, icon_color = fill, "#5c7af5", CARD
        else:
            fill = "#e8efff" if self.pressed else (
                CARD_BLUE if self.hover else CARD)
            text_color, sub_color = INK, INK_MUTED
            outline = ACCENT_LIGHT if self.focus_get() == self else CARD_LINE
            icon_bg, icon_color = ACCENT_SOFT, ACCENT_DARK
        if not self.pressed:
            self.create_polygon(_round_pts(4, 6, w - 2, h - 2, 17),
                                smooth=True, fill=SHADOW, outline="")
        self.create_polygon(_round_pts(2, 2, w - 4, h - 6, 17), smooth=True,
                            fill=fill, outline=outline, width=1)
        self.create_polygon(_round_pts(20, 24, 62, 66, 12), smooth=True,
                            fill=icon_bg, outline="")
        draw_icon(self, self.icon, 41, 45, icon_color, 1.0)
        compact_titles = {
            "Open / OCR...": "Open / OCR",
            "Open webpage...": "Web page",
            "Read clipboard": "Clipboard",
            "Save to WAV...": "Save WAV...",
        }
        tiny_titles = {
            "Open / OCR...": "File/OCR",
            "Open webpage...": "Web",
            "Read clipboard": "Clip",
            "Save to WAV...": "Save",
        }
        if w < 150:
            title = tiny_titles.get(self.title, self.title)
        elif w < 175:
            title = compact_titles.get(self.title, self.title)
        else:
            title = self.title
        self.create_text(76, 37, text=title, anchor="w",
                         fill=text_color,
                         font=(FONT_UI, 10 if w < 155 else 11, "bold"))
        if w >= 165:
            self.create_text(76, 60, text=self.subtitle, anchor="w",
                             fill=sub_color, font=(FONT_UI, 9))


# label -> (engine, voice_id, lang)
VOICES = {
    # ---- Microsoft online: text is sent to Microsoft only when reading
    **{label: ("edge", voice_id, lang)
       for label, (voice_id, lang) in ONLINE_VOICES.items()},
    # ---- Kokoro: most natural (~330 MB one-time, covers all Kokoro voices)
    "Heart (US female) - Kokoro, most natural": ("kokoro", "af_heart", "en-us"),
    "Bella (US female) - Kokoro": ("kokoro", "af_bella", "en-us"),
    "Sarah (US female) - Kokoro": ("kokoro", "af_sarah", "en-us"),
    "Nicole (US female, soft) - Kokoro": ("kokoro", "af_nicole", "en-us"),
    "Michael (US male) - Kokoro": ("kokoro", "am_michael", "en-us"),
    "Adam (US male) - Kokoro": ("kokoro", "am_adam", "en-us"),
    "Emma (British female) - Kokoro": ("kokoro", "bf_emma", "en-gb"),
    "George (British male) - Kokoro": ("kokoro", "bm_george", "en-gb"),
    # ---- Piper: instant, lightweight (~60 MB per voice)
    "Lessac (US female) - Piper, fastest": ("piper", "en_US-lessac-medium", None),
    "Amy (US female) - Piper": ("piper", "en_US-amy-medium", None),
    "HFC Female (US) - Piper": ("piper", "en_US-hfc_female-medium", None),
    "HFC Male (US) - Piper": ("piper", "en_US-hfc_male-medium", None),
    "Ryan (US male, HQ) - Piper": ("piper", "en_US-ryan-high", None),
    "Joe (US male) - Piper": ("piper", "en_US-joe-medium", None),
    "Cori (British female, HQ) - Piper": ("piper", "en_GB-cori-high", None),
    "Alba (Scottish female) - Piper": ("piper", "en_GB-alba-medium", None),
    # ---- Arabic: official lightweight Piper voice (~60 MB one time)
    "Kareem (Arabic male, Jordan) - Piper": (
        "piper", "ar_JO-kareem-medium", "ar"),
    # ---- Hausa: Murya multi-speaker Piper voice (~74 MB one time)
    "Murya F1 (Hausa female, community) - Piper": (
        "piper", "ha_NG-murya-medium#5", "ha"),
    "Murya M1 (Hausa male, community) - Piper": (
        "piper", "ha_NG-murya-medium#6", "ha"),
    # ---- Windows built-in (no download, speaks instantly)
    "David (US male) - Windows built-in": ("sapi", "David", None),
    "Zira (US female) - Windows built-in": ("sapi", "Zira", None),
}
DEFAULT_VOICE = "Lessac (US female) - Piper, fastest"
DEFAULT_CLIPBOARD_VOICE = "Lessac (US female) - Piper, fastest"

OFFICIAL_PIPER_REVISION = "ea046e8458f6acd997706d6e6066a022b42f6fb1"
# Immutable catalog metadata from rhasspy/piper-voices at the revision above.
# Size plus checksum validation also repairs incomplete legacy downloads.
OFFICIAL_PIPER_FILES = {
    "en_US-lessac-medium": {
        ".onnx": (63201294, "2fc642b535197b6305c7c8f92dc8b24f"),
        ".onnx.json": (4885, "c1f2b7bddefe113f3255ff9ef234cfd3"),
    },
    "en_US-amy-medium": {
        ".onnx": (63201294, "778d28aeb95fcdf8a882344d9df142fc"),
        ".onnx.json": (4882, "7f37dadb26340c90ebc8088e0b252310"),
    },
    "en_US-hfc_female-medium": {
        ".onnx": (63201294, "7abec91f1d6e19e913fbc4a333f62787"),
        ".onnx.json": (5033, "c3d00f54dac3b4068f2576c15c5da3bc"),
    },
    "en_US-hfc_male-medium": {
        ".onnx": (63201294, "cd2fda1933f0653d3ddc85e5f30ebdd2"),
        ".onnx.json": (5033, "9b4849dbc1e72f35de7391528dea60d9"),
    },
    "en_US-ryan-high": {
        ".onnx": (120786792, "5d879a17bddf5007f76655b445ba78b4"),
        ".onnx.json": (4166, "444ff9d6c17218a0eb1d12a20559d869"),
    },
    "en_US-joe-medium": {
        ".onnx": (63201294, "74fd6a4dc39e0aa9dce145d7f5acd4f6"),
        ".onnx.json": (4794, "811036b9c1451545f9495fdc1baa0754"),
    },
    "en_GB-cori-high": {
        ".onnx": (114219352, "3474a80133d9a03e6870d2ac42c18806"),
        ".onnx.json": (4963, "0f7d42e77a99193006aa34a34442f5e0"),
    },
    "en_GB-alba-medium": {
        ".onnx": (63201294, "c07f313752bb3aba8061041666251654"),
        ".onnx.json": (4888, "dbb6f2ede31082710665221417906e13"),
    },
    "ar_JO-kareem-medium": {
        ".onnx": (63201294, "c0697df8a7fb180079cc5ac523f91a8e"),
        ".onnx.json": (5024, "dd70b31eb5a395907241b1e5367ace3a"),
    },
}

KOKORO_FILES = {
    "kokoro-v1.0.onnx": {
        "url": (
            "https://github.com/thewh1teagle/kokoro-onnx/releases/"
            "download/model-files-v1.0/kokoro-v1.0.onnx"
        ),
        "size": 325532387,
        "sha256": (
            "7d5df8ecf7d4b1878015a32686053fd0"
            "eebe2bc377234608764cc0ef3636a6c5"
        ),
    },
    "voices-v1.0.bin": {
        "url": (
            "https://github.com/thewh1teagle/kokoro-onnx/releases/"
            "download/model-files-v1.0/voices-v1.0.bin"
        ),
        "size": 28214398,
        "sha256": (
            "bca610b8308e8d99f32e6fe4197e7ec"
            "01679264efed0cac9140fe9c29f1fbf7d"
        ),
    },
}

HAUSA_MODEL_REVISION = "7eccd91ec0dc154c789b778c924b89f59a0dbccb"
CUSTOM_PIPER_FILES = {
    "ha_NG-murya-medium": {
        ".onnx": {
            "url": (
                "https://huggingface.co/adab-tech/"
                "murya-piper-hausa-tts/resolve/"
                f"{HAUSA_MODEL_REVISION}/model.onnx"
            ),
            "sha256": (
                "7889e1a9e07cabf6e1cbec4ce09d8eed"
                "49fc63bb729770f60dcb7de2f1d34c74"
            ),
            "size": 77063376,
            "label": "Hausa Murya voice (~74 MB)",
        },
        ".onnx.json": {
            "url": (
                "https://huggingface.co/adab-tech/"
                "murya-piper-hausa-tts/resolve/"
                f"{HAUSA_MODEL_REVISION}/model.onnx.json"
            ),
            "sha256": (
                "65317cec0b5c13d5ad38b5444caf37a9"
                "3cbfb98b3b3d3117479c5aa642ee85bb"
            ),
            "size": 2340,
            "label": "Hausa Murya voice settings",
        },
    },
}

VOICE_PREVIEW_TEXT = {
    "ar": (
        "مرحباً! هذا صوت عربي محلي من بايبر. "
        "يمكنني قراءة النصوص العربية بصوت عالٍ دون إرسالها إلى الإنترنت."
    ),
    "ha": (
        "Sannu! Wannan muryar Hausa ce ta cikin na'ura. "
        "Zan iya karanta rubutun Hausa ba tare da aika shi intanet ba."
    ),
}

HAUSA_DIGIT_WORDS = {
    "0": "sifili", "1": "ɗaya", "2": "biyu", "3": "uku",
    "4": "huɗu", "5": "biyar", "6": "shida", "7": "bakwai",
    "8": "takwas", "9": "tara",
}


# ------------------------------------------------------------- install helpers
def module_ok(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def pip_install(pkg, status_cb):
    """Install a package into THE SAME Python that is running this app."""
    if IS_FROZEN:
        status_cb(
            f"{pkg} is missing from this installation. "
            "Please reinstall Piper Reader.")
        return False
    status_cb(f"Installing {pkg} (one time)...")
    r = None
    for extra in ([], ["--user"]):
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg, *extra],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            status_cb(f"{pkg} installed.")
            return True
    status_cb("Install failed: " + (r.stderr.strip()[-300:] if r else "unknown"))
    return False


def download_file(url, dest, status_cb, label, expected_sha256=None,
                  expected_md5=None, expected_size=None):
    tmp = dest + ".part"
    def hook(blocks, bs, total):
        if total > 0:
            pct = min(100, blocks * bs * 100 // total)
            status_cb(f"Downloading {label}... {pct}% (one time)")
    try:
        urllib.request.urlretrieve(url, tmp, reporthook=hook)
        if (expected_size is not None and
                os.path.getsize(tmp) != int(expected_size)):
            raise RuntimeError(
                f"Downloaded {label} has an unexpected file size")
        if expected_sha256:
            digest = hashlib.sha256()
            with open(tmp, "rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            if not hmac.compare_digest(
                    digest.hexdigest().casefold(),
                    expected_sha256.casefold()):
                raise RuntimeError(
                    f"Downloaded {label} did not pass its safety check")
        if expected_md5:
            try:
                digest = hashlib.md5(usedforsecurity=False)
            except TypeError:
                digest = hashlib.md5()
            with open(tmp, "rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            if not hmac.compare_digest(
                    digest.hexdigest().casefold(),
                    expected_md5.casefold()):
                raise RuntimeError(
                    f"Downloaded {label} did not pass its catalog check")
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- audio (winmm)
class WavPlayer:
    """Plays WAV files via Windows MCI with stop and pause/resume support."""

    def __init__(self):
        self._mci = ctypes.windll.winmm.mciSendStringW
        self._mci_error = ctypes.windll.winmm.mciGetErrorStringW
        self._alias = "piperreader_snd"
        self._lock = threading.RLock()
        self._opened = False
        self._paused = False

    def _cmd(self, command, check=False):
        buf = ctypes.create_unicode_buffer(256)
        code = self._mci(command, buf, 255, 0)
        if code and check:
            error = ctypes.create_unicode_buffer(256)
            self._mci_error(code, error, 255)
            detail = error.value or f"MCI error {code}"
            raise RuntimeError(f"Audio command failed ({command}): {detail}")
        return buf.value

    def _close_locked(self):
        stop_error = close_error = None
        if self._opened:
            try:
                self._cmd(f'stop {self._alias}', check=True)
                self._paused = False
            except Exception as e:
                stop_error = e
            try:
                self._cmd(f'close {self._alias}', check=True)
                self._opened = False
                self._paused = False
            except Exception as e:
                close_error = e
        return close_error or stop_error

    def play_blocking(self, path, stop_event, pause_event=None):
        try:
            with self._lock:
                close_error = self._close_locked()
                if close_error is not None:
                    raise close_error
                self._cmd(
                    f'open "{path}" type waveaudio alias {self._alias}',
                    check=True)
                self._opened = True
                self._cmd(f'play {self._alias}', check=True)
            while not stop_event.is_set():
                wants_pause = bool(pause_event and pause_event.is_set())
                if stop_event.is_set():
                    break
                if wants_pause:
                    self.pause()
                else:
                    if stop_event.is_set():
                        break
                    self.resume()
                with self._lock:
                    if not self._opened:
                        break
                    mode = self._cmd(f'status {self._alias} mode', check=True)
                if mode not in ("playing", "paused"):
                    break
                time.sleep(0.05)
        finally:
            had_error = sys.exc_info()[0] is not None
            with self._lock:
                close_error = self._close_locked()
            if close_error is not None and not had_error:
                raise close_error

    def pause(self):
        with self._lock:
            if not self._opened or self._paused:
                return self._opened
            self._cmd(f'pause {self._alias}', check=True)
            self._paused = True
            return True

    def resume(self):
        with self._lock:
            if not self._opened or not self._paused:
                return self._opened
            self._cmd(f'resume {self._alias}', check=True)
            self._paused = False
            return True

    def stop(self):
        with self._lock:
            return self._close_locked()


# ---------------------------------------------------------------- Piper engine
class PiperEngine:
    name = "Piper"
    package = "piper-tts"
    module = "piper"
    chunk_len = 600

    def __init__(self, status_cb):
        self.status = status_cb
        self._voices = {}
        self._voice_lock = threading.RLock()
        os.makedirs(VOICES_DIR, exist_ok=True)

    def ready(self):
        return module_ok(self.module)

    def install(self):
        return pip_install(self.package, self.status) and self.ready()

    @staticmethod
    def _voice_details(voice_id):
        """Return the shared model name and optional multi-speaker ID."""
        model_id, separator, speaker = voice_id.rpartition("#")
        if not separator:
            return voice_id, None
        try:
            return model_id, int(speaker)
        except ValueError as exc:
            raise RuntimeError("The selected Piper speaker is invalid") from exc

    def _download_custom_voice(self, model_id):
        files = CUSTOM_PIPER_FILES[model_id]
        for suffix, metadata in files.items():
            destination = os.path.join(VOICES_DIR, model_id + suffix)
            if (os.path.exists(destination) and
                    (metadata.get("size") is None or
                     os.path.getsize(destination) == metadata["size"])):
                continue
            download_file(
                metadata["url"], destination, self.status,
                metadata["label"], metadata["sha256"],
                expected_size=metadata.get("size"))

    @staticmethod
    def _official_voice_url(model_id, suffix):
        match = re.fullmatch(
            r"(?P<language>[a-z]{2,3}_[A-Z]{2})-"
            r"(?P<name>.+)-(?P<quality>[^-]+)", model_id)
        if not match:
            raise RuntimeError("The selected Piper voice name is invalid")
        language = match.group("language")
        family = language.split("_", 1)[0]
        relative = "/".join((
            family, language, match.group("name"), match.group("quality"),
            model_id + suffix,
        ))
        return (
            "https://huggingface.co/rhasspy/piper-voices/resolve/"
            f"{OFFICIAL_PIPER_REVISION}/{relative}?download=true"
        )

    def _download_official_voice(self, model_id):
        files = OFFICIAL_PIPER_FILES.get(model_id)
        if files is None:
            raise RuntimeError(
                "This Piper voice is not in the verified download catalog")
        for suffix, (expected_size, expected_md5) in files.items():
            destination = os.path.join(VOICES_DIR, model_id + suffix)
            if (os.path.exists(destination) and
                    os.path.getsize(destination) == expected_size):
                continue
            label = (f"{model_id} voice" if suffix == ".onnx" else
                     f"{model_id} voice settings")
            download_file(
                self._official_voice_url(model_id, suffix),
                destination, self.status, label,
                expected_md5=expected_md5,
                expected_size=expected_size)

    @staticmethod
    def _voice_files_ready(model_id):
        catalog = CUSTOM_PIPER_FILES.get(model_id)
        if catalog is not None:
            for suffix, metadata in catalog.items():
                path = os.path.join(VOICES_DIR, model_id + suffix)
                if (not os.path.exists(path) or
                        (metadata.get("size") is not None and
                         os.path.getsize(path) != metadata["size"])):
                    return False
            return True
        catalog = OFFICIAL_PIPER_FILES.get(model_id)
        if catalog is None:
            return False
        for suffix, (expected_size, _expected_md5) in catalog.items():
            path = os.path.join(VOICES_DIR, model_id + suffix)
            if (not os.path.exists(path) or
                    os.path.getsize(path) != expected_size):
                return False
        return True

    def _get(self, voice_id, quiet=False, allow_download=True):
        model_id, _speaker_id = self._voice_details(voice_id)
        with self._voice_lock:
            if model_id not in self._voices:
                from piper import PiperVoice
                path = os.path.join(VOICES_DIR, f"{model_id}.onnx")
                config_path = path + ".json"
                if not self._voice_files_ready(model_id):
                    if not allow_download:
                        return None
                    if model_id in CUSTOM_PIPER_FILES:
                        self._download_custom_voice(model_id)
                    else:
                        self.status(
                            f"Downloading voice {model_id} "
                            "(~60 MB, one time)...")
                        try:
                            self._download_official_voice(model_id)
                        except Exception as exc:
                            raise RuntimeError(
                                f"Voice download failed: {exc}") from exc
                        if not self._voice_files_ready(model_id):
                            raise RuntimeError(
                                "Voice download did not create its model files")
                if not quiet:
                    self.status(f"Loading voice {model_id}...")
                self._voices[model_id] = PiperVoice.load(
                    path, config_path=config_path)
            return self._voices[model_id]

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path):
        return self._synth_to_wav(text, voice_id, lang, speed, volume, out_path)

    def synth_to_wav_local(self, text, voice_id, lang, speed, volume, out_path):
        """Clipboard recovery may only use existing files, without global status."""
        return self._synth_to_wav(
            text, voice_id, lang, speed, volume, out_path, local_only=True)

    def _synth_to_wav(self, text, voice_id, lang, speed, volume, out_path,
                      local_only=False):
        from piper import SynthesisConfig
        model_id, speaker_id = self._voice_details(voice_id)
        voice = self._get(voice_id, quiet=local_only, allow_download=not local_only)
        if voice is None:
            raise RuntimeError("The offline voice files are not available.")
        if model_id == "ha_NG-murya-medium":
            # Murya's alphabet is lowercase, has no digits, and expects NFD.
            text = text.translate(str.maketrans({
                "’": "'", "‘": "'", "ʼ": "'",
            }))
            text = re.sub(
                r"\d+",
                lambda match: " ".join(
                    HAUSA_DIGIT_WORDS[digit] for digit in match.group()),
                text,
            )
            text = unicodedata.normalize("NFD", text).casefold()
        cfg = SynthesisConfig(
            speaker_id=speaker_id, volume=volume,
            length_scale=1.0 / max(speed, 0.1))
        with wave.open(out_path, "wb") as f:
            voice.synthesize_wav(text, f, syn_config=cfg)

    def warm(self, voice_id, lang=None):
        """Load an already-downloaded voice without playing or downloading."""
        return self._get(
            voice_id, quiet=True, allow_download=False) is not None


# --------------------------------------------------------------- Kokoro engine
class KokoroEngine:
    name = "Kokoro"
    package = "kokoro-onnx"
    module = "kokoro_onnx"
    # Start with a small block, then return to longer blocks for smoother
    # prosody. One-block lookahead in ReaderApp hides later synthesis.
    initial_chunk_len = KOKORO_INITIAL_CHUNK_LEN
    chunk_len = 180

    def __init__(self, status_cb):
        self.status = status_cb
        self._kokoro = None
        self._runtime_warmed = False
        self._lock = threading.RLock()
        os.makedirs(VOICES_DIR, exist_ok=True)

    def ready(self):
        return module_ok(self.module)

    def install(self):
        return pip_install(self.package, self.status) and self.ready()

    def _get(self, quiet=False, allow_download=True):
        with self._lock:
            if self._kokoro is None:
                paths = {}
                for fname, metadata in KOKORO_FILES.items():
                    path = os.path.join(VOICES_DIR, fname)
                    if (not os.path.exists(path) or
                            os.path.getsize(path) != metadata["size"]):
                        if not allow_download:
                            return None
                        size = ("~310 MB" if fname.endswith(".onnx")
                                else "~27 MB")
                        download_file(
                            metadata["url"], path, self.status,
                            f"Kokoro model ({size})",
                            expected_sha256=metadata["sha256"],
                            expected_size=metadata["size"])
                    paths[fname] = path
                if not quiet:
                    self.status("Loading Kokoro voices...")
                from kokoro_onnx import Kokoro
                self._kokoro = Kokoro(
                    paths["kokoro-v1.0.onnx"],
                    paths["voices-v1.0.bin"])
            return self._kokoro

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path):
        import numpy as np
        with self._lock:
            kokoro = self._get()
            samples, rate = kokoro.create(
                text, voice=voice_id, speed=max(speed, 0.1),
                lang=lang or "en-us")
            self._runtime_warmed = True
        samples = np.clip(samples * volume, -1.0, 1.0)
        pcm = (samples * 32767).astype(np.int16)
        with wave.open(out_path, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(rate)
            f.writeframes(pcm.tobytes())

    def warm(self, voice_id, lang=None, force=False):
        """Load installed files and optionally re-prime ONNX silently."""
        if any(not os.path.exists(os.path.join(VOICES_DIR, filename))
               for filename in KOKORO_FILES):
            return False
        with self._lock:
            kokoro = self._get(quiet=True, allow_download=False)
            if kokoro is None:
                return False
            if force or not self._runtime_warmed:
                kokoro.create(
                    "Ready.", voice=voice_id, speed=1.0,
                    lang=lang or "en-us")
                self._runtime_warmed = True
        return True


# ----------------------------------------------------- Windows built-in (SAPI)
class SapiEngine:
    """Bridge to the voices built into Windows (David, Zira, ...)."""
    name = "Windows"
    package = "pywin32"
    module = "win32com.client"
    chunk_len = 1000

    def __init__(self, status_cb):
        self.status = status_cb

    def ready(self):
        return module_ok(self.module)

    def install(self):
        return pip_install(self.package, self.status) and self.ready()

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path):
        import math
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        try:
            v = Dispatch("SAPI.SpVoice")
            stream = Dispatch("SAPI.SpFileStream")
            for cand in v.GetVoices():
                if voice_id.lower() in cand.GetDescription().lower():
                    v.Voice = cand
                    break
            else:
                raise RuntimeError(f"{voice_id} voice not found on this PC")
            v.Rate = max(-10, min(10, round(9 * math.log2(max(speed, 0.1)))))
            v.Volume = int(max(0.0, min(volume, 1.0)) * 100)
            stream.Open(out_path, 3)          # create for write
            v.AudioOutputStream = stream
            v.Speak(text)
            stream.Close()
        finally:
            pythoncom.CoUninitialize()


def normalize_speed(value):
    """Return a finite, supported speed while preserving saved precision."""
    if isinstance(value, bool):
        value = 1.0
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = 1.0
    if not math.isfinite(speed):
        speed = 1.0
    return round(max(SPEED_MIN, min(SPEED_MAX, speed)), 2)


def quantize_speed(value, step=SPEED_FINE_STEP):
    """Snap a speed to an exact UI increment without floating-point drift."""
    if isinstance(value, bool):
        value = 1.0
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = 1.0
    if not math.isfinite(speed):
        speed = 1.0
    speed = max(SPEED_MIN, min(SPEED_MAX, speed))
    steps = math.floor((speed / step) + 0.5 + 1e-9)
    return normalize_speed(steps * step)


def stepped_speed(value, direction):
    """Move to the next clean tenth in the requested direction."""
    speed = normalize_speed(value)
    scaled = speed / SPEED_BUTTON_STEP
    if direction > 0:
        target = (math.floor(scaled + 1e-9) + 1) * SPEED_BUTTON_STEP
    elif direction < 0:
        target = (math.ceil(scaled - 1e-9) - 1) * SPEED_BUTTON_STEP
    else:
        target = speed
    return normalize_speed(target)


def speed_description(value):
    """Short, friendly category displayed beside the exact multiplier."""
    speed = normalize_speed(value)
    if speed < 0.8:
        return "Very slow"
    if speed < 0.95:
        return "Slow"
    if speed <= 1.1:
        return "Normal"
    if speed <= 1.5:
        return "Faster"
    if speed <= 2.0:
        return "Fast"
    return "Very fast"


def format_speed(value):
    """Format whole tenths compactly while retaining useful custom values."""
    speed = normalize_speed(value)
    if math.isclose(speed, round(speed, 1), abs_tol=0.001):
        return f"{speed:.1f}\u00d7"
    return f"{speed:.2f}\u00d7"


def normalize_repeat_mode(value):
    """Return a stable repeat-mode key for settings and queued requests."""
    candidate = str(value or "off").strip().casefold()
    if candidate in REPEAT_MODE_LABELS:
        return candidate
    return REPEAT_LABEL_MODES.get(candidate, "off")


def normalize_repeat_count(value):
    """Clamp the user-entered total play count to the supported range."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = REPEAT_COUNT_DEFAULT
    return max(REPEAT_COUNT_MIN, min(REPEAT_COUNT_MAX, count))


def split_repeat_sentences(text):
    """Split text into sentence units without merging short sentences."""
    text = str(text or "").strip()
    if not text:
        return []
    units = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in "\r\n":
            sentence = text[start:index].strip()
            if sentence:
                units.append(sentence)
            while index < length and text[index].isspace():
                index += 1
            start = index
            continue
        if character in SENTENCE_END_CHARS:
            end = index + 1
            while end < length and text[end] in SENTENCE_END_CHARS:
                end += 1
            while end < length and text[end] in SENTENCE_CLOSERS:
                end += 1
            if end == length or text[end].isspace():
                sentence = text[start:end].strip()
                if sentence:
                    units.append(sentence)
                while end < length and text[end].isspace():
                    end += 1
                start = end
                index = end
                continue
        index += 1
    remainder = text[start:].strip()
    if remainder:
        units.append(remainder)
    return units


def split_repeat_paragraphs(text):
    """Split plain text at blank lines, the conventional paragraph boundary."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in
            re.split(r"\n[ \t]*\n+", normalized.strip()) if part.strip()]


def build_repeat_units(kind, payload, repeat_mode, max_len,
                       first_max_len=None):
    """Build logical playback units while retaining highlight metadata.

    Each returned unit is a list of ``(engine-sized text, metadata)`` atoms.
    A long sentence or paragraph may contain several atoms, but it remains one
    repeat unit so all of its parts are replayed together.
    """
    mode = normalize_repeat_mode(repeat_mode)
    sources = []
    if kind in ("browser", "doc"):
        for index, item in enumerate(payload or []):
            try:
                text, meta = item
            except (TypeError, ValueError):
                continue
            text = str(text or "").strip()
            if text:
                sources.append((text, meta, index))
    else:
        text = str(payload or "").strip()
        if mode == "sentence":
            parts = split_repeat_sentences(text)
        elif mode == "paragraph":
            parts = split_repeat_paragraphs(text)
        else:
            parts = [text] if text else []
        sources = [(part, None, index) for index, part in enumerate(parts)]

    grouped_sources = []
    for text, meta, fallback_index in sources:
        if mode == "passage":
            group_key = ("passage", 0)
        elif mode in ("sentence", "paragraph") and isinstance(meta, dict):
            identity = meta.get(f"{mode}_id")
            group_key = ((mode, identity) if identity is not None else
                         ("source", fallback_index))
        else:
            group_key = ("source", fallback_index)
        if grouped_sources and grouped_sources[-1][0] == group_key:
            grouped_sources[-1][1].append((text, meta))
        else:
            grouped_sources.append((group_key, [(text, meta)]))

    units = []
    startup_chars = 0
    for _group_key, group in grouped_sources:
        atoms = []
        for text, meta in group:
            startup_limit = (
                first_max_len if first_max_len is not None and
                startup_chars < first_max_len else None)
            parts = split_chunks(
                text, max_len, first_max_len=startup_limit)
            atoms.extend((part, meta) for part in parts)
            startup_chars += sum(len(part) for part in parts)
        if atoms:
            units.append(atoms)
    if mode == "off":
        # Normal playback does not need to retain previously played WAVs.
        # One atom per unit lets the worker delete each file immediately and
        # keeps long documents at the old bounded temporary-disk footprint.
        return [[atom] for unit in units for atom in unit]
    return units


def split_chunks(text, max_len=500, first_max_len=None):
    """Split text into sentence-grouped chunks so playback starts quickly."""
    max_len = max(40, int(max_len))
    if first_max_len is not None:
        first_max_len = max(40, min(int(first_max_len), max_len))
    sentences = re.split(r"(?<=[.!?:;؟؛۔\n])\s+", text.strip())
    pieces = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) <= max_len:
            pieces.append(sentence)
            continue
        current = ""
        for word in sentence.split():
            if len(word) > max_len:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(
                    word[offset:offset + max_len]
                    for offset in range(0, len(word), max_len))
            elif current and len(current) + len(word) + 1 > max_len:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            pieces.append(current)
    chunks, cur = [], ""
    for s in pieces:
        if not s.strip():
            continue
        if cur and len(cur) + len(s) + 1 > max_len:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    if (first_max_len is not None and chunks and
            len(chunks[0]) > first_max_len):
        first = chunks.pop(0)
        # The first atom is a latency budget, so even a convenient sentence
        # ending just beyond it must not make startup synthesis larger. Use
        # the last useful natural boundary inside the hard cap, otherwise a
        # word boundary, and only split a word as the final fallback.
        natural_cut = 0
        for match in re.finditer(
                r"[.!?,;:؟؛۔](?:\s+|$)",
                first[:first_max_len]):
            if match.end() >= max(16, first_max_len // 4):
                natural_cut = match.end()
        cut = natural_cut
        if not cut:
            cut = first.rfind(" ", 0, first_max_len + 1)
        if cut < max(16, first_max_len // 4):
            cut = first_max_len
        head, remainder = first[:cut].strip(), first[cut:].strip()
        if remainder:
            chunks.insert(0, remainder)
        if head:
            chunks.insert(0, head)
    return chunks


# ------------------------------------------------------------- webpage loading
class _WebRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects to load_webpage without draining their response body."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


_WEB_POSITIVE_RE = re.compile(
    r"(?:^|[-_\s])(article|post|story|entry|main|content|body|reader)"
    r"(?:$|[-_\s])", re.I)
_WEB_JUNK_RE = re.compile(
    r"(?:^|[-_\s])(cookie|consent|banner|modal|popup|advert(?:isement)?s?|"
    r"ads?|social|share|subscribe|newsletter|comments?|related|recommended|"
    r"breadcrumb|toolbar|navigation|nav|menu|sidebar|footer|promo|paywall|toc)"
    r"(?:$|[-_\s])", re.I)


def normalize_web_url(value):
    """Normalize a pasted address and allow only explicit web URLs."""
    value = (value or "").strip().strip("\"'")
    if not value:
        raise ValueError("Enter a webpage address.")
    if len(value) > 4096:
        raise ValueError("That webpage address is too long.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "https://" + value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.casefold() not in ("http", "https"):
        raise ValueError("Only http:// and https:// webpages can be opened.")
    if not parts.hostname:
        raise ValueError("That webpage address is incomplete.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Web addresses containing a username or password are not allowed.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("That webpage address has an invalid port.") from exc
    hostname = parts.hostname
    try:
        parsed_ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("That webpage address has an invalid hostname.") from exc
        netloc = hostname
    else:
        netloc = f"[{hostname}]" if parsed_ip.version == 6 else hostname
    if port is not None:
        netloc += f":{port}"
    path = urllib.parse.quote(
        parts.path or "/", safe="/:@!$&'()*+,;=-._~%")
    query = urllib.parse.quote(
        parts.query, safe="=&?/:;+,%@!$'()*-._~[]")
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), netloc, path, query, ""))


def _validate_public_web_target(url):
    """Reject URLs that could make a public page contact local services."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise ValueError("That webpage address is incomplete.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        port = parts.port or (443 if parts.scheme.casefold() == "https" else 80)
        try:
            records = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise RuntimeError(f"could not resolve {host}") from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except ValueError:
                continue
    if not addresses:
        raise RuntimeError(f"could not resolve {host}")
    if any(not address.is_global or address.is_multicast
           for address in addresses):
        raise ValueError(
            "For safety, Reader View can open only public webpages, not "
            "localhost or private-network addresses.")


def _decode_web_bytes(data, charset=None):
    """Decode HTML using its transport/meta hints, then safe fallbacks."""
    candidates = []
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    if charset:
        candidates.append(charset)
    sample = data[:8192].decode("ascii", errors="ignore")
    match = re.search(
        r"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)",
        sample, re.I)
    if match:
        candidates.append(match.group(1))
    candidates.extend(("utf-8", "windows-1252"))
    tried = set()
    for encoding in candidates:
        key = str(encoding).casefold()
        if key in tried:
            continue
        tried.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _read_web_body(response, deadline):
    pieces, wire_length = [], 0
    reader = getattr(response, "read1", response.read)
    while True:
        if time.monotonic() > deadline:
            raise RuntimeError("webpage download took longer than 20 seconds")
        chunk = reader(min(65536, WEB_WIRE_LIMIT + 1 - wire_length))
        if not chunk:
            break
        pieces.append(chunk)
        wire_length += len(chunk)
        if wire_length > WEB_WIRE_LIMIT:
            raise RuntimeError("webpage download is larger than 3 MB")
    wire = b"".join(pieces)
    content_encoding = (response.headers.get("Content-Encoding") or "").split(",")[0]
    content_encoding = content_encoding.strip().casefold()
    if content_encoding in ("", "identity"):
        data = wire
    elif content_encoding in ("gzip", "x-gzip"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
                data = stream.read(WEB_CONTENT_LIMIT + 1)
        except (OSError, EOFError) as exc:
            raise RuntimeError("webpage returned invalid gzip content") from exc
    elif content_encoding == "deflate":
        data = None
        for window_bits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decoder = zlib.decompressobj(window_bits)
                candidate = decoder.decompress(wire, WEB_CONTENT_LIMIT + 1)
                if len(candidate) <= WEB_CONTENT_LIMIT and not decoder.unconsumed_tail:
                    candidate += decoder.flush(WEB_CONTENT_LIMIT + 1 - len(candidate))
                if not decoder.eof:
                    raise zlib.error("incomplete deflate stream")
                data = candidate
                break
            except zlib.error:
                continue
        if data is None:
            raise RuntimeError("webpage returned invalid deflate content")
    else:
        raise RuntimeError(f"unsupported webpage compression: {content_encoding}")
    if len(data) > WEB_CONTENT_LIMIT:
        raise RuntimeError("expanded webpage is larger than 8 MB")
    return data


def _clean_web_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _web_meta(doc, *names):
    wanted = {name.casefold() for name in names}
    for node in doc.xpath("//meta[@content]"):
        keys = (node.get("property"), node.get("name"), node.get("itemprop"))
        if any(key and key.casefold() in wanted for key in keys):
            text = _clean_web_text(node.get("content"))
            if text:
                return text
    return ""


def _jsonld_author(value):
    if isinstance(value, str):
        return _clean_web_text(value)
    if isinstance(value, dict):
        return _clean_web_text(value.get("name", ""))
    if isinstance(value, list):
        names = [_jsonld_author(item) for item in value]
        return ", ".join(name for name in names if name)
    return ""


def _web_jsonld_article(doc):
    """Return the most complete schema.org article embedded in the page."""
    best = {}
    article_types = {
        "article", "newsarticle", "blogposting", "report", "techarticle",
        "scholarlyarticle", "socialmediaposting",
    }

    def visit(value):
        nonlocal best
        if isinstance(value, dict):
            raw_type = value.get("@type", "")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            types = {str(item).casefold() for item in types}
            raw_body = value.get("articleBody", "")
            if isinstance(raw_body, str):
                paragraphs = [
                    _clean_web_text(item) for item in
                    re.split(r"\n\s*\n|\r?\n", raw_body)]
                body = "\n\n".join(item for item in paragraphs if item)
            else:
                body = ""
            if types & article_types and len(body) > len(best.get("body", "")):
                best = {
                    "body": body,
                    "title": _clean_web_text(value.get("headline", "")),
                    "author": _jsonld_author(value.get("author")),
                }
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for node in doc.xpath("//script[contains(translate(@type, 'LDJSON', 'ldjson'), 'ld+json')]"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            visit(json.loads(raw))
        except (TypeError, ValueError, RecursionError):
            continue
    return best


def _drop_web_noise(doc):
    unwanted = (
        "//script|//style|//noscript|//template|//svg|//canvas|//iframe|"
        "//object|//embed|//form|//nav|//footer|//aside"
    )
    for node in list(doc.xpath(unwanted)):
        try:
            node.drop_tree()
        except Exception:
            pass
    for node in list(doc.xpath("//*[@hidden or @aria-hidden='true']")):
        try:
            node.drop_tree()
        except Exception:
            pass
    for node in list(doc.xpath("//*[@class or @id or @style]")):
        if node.tag in ("html", "body"):
            continue
        marker = f"{node.get('id', '')} {node.get('class', '')}"
        style = re.sub(r"\s+", "", node.get("style", "")).casefold()
        if (_WEB_JUNK_RE.search(marker) or "display:none" in style or
                "visibility:hidden" in style):
            try:
                node.drop_tree()
            except Exception:
                pass


def _sample_web_node_text(node, limit=120000):
    pieces, length = [], 0
    for value in node.itertext():
        value = _clean_web_text(value)
        if not value:
            continue
        remaining = limit - length
        if remaining <= 0:
            break
        pieces.append(value[:remaining])
        length += min(len(value), remaining) + 1
    return " ".join(pieces)


def _web_candidate_score(node):
    text = _sample_web_node_text(node)
    if len(text) < 120:
        return float("-inf")
    paragraphs = []
    for item in node.iter("p"):
        paragraphs.append(_sample_web_node_text(item, 2000))
        if len(paragraphs) >= 200:
            break
    substantial = sum(len(item) >= 80 for item in paragraphs)
    punctuation = min(80, sum(text.count(mark) for mark in ".!?"))
    link_length = 0
    for number, link in enumerate(node.iter("a")):
        link_length += len(_sample_web_node_text(link, 1000))
        if number >= 499:
            break
    link_density = min(0.95, link_length / max(len(text), 1))
    tag = str(node.tag).casefold()
    boost = {"article": 550, "main": 450, "body": -250}.get(tag, 0)
    marker = f"{node.get('id', '')} {node.get('class', '')}"
    if _WEB_POSITIVE_RE.search(marker):
        boost += 260
    return (len(text) + substantial * 150 + punctuation * 9 + boost) * \
        (1.0 - link_density) ** 2


def _web_content_root(doc):
    # Candidate scoring calls text_content(), so keep it deliberately bounded
    # even when hostile HTML contains thousands of nested "content" divs.
    unique, seen = [], set()

    def add(node):
        marker = id(node)
        if marker not in seen:
            seen.add(marker)
            unique.append(node)

    for node in doc.xpath("//article|//main|//*[@role='main']")[:8]:
        add(node)
    containers = doc.xpath("//section|//div")
    for node in containers:
        marker = f"{node.get('id', '')} {node.get('class', '')}"
        if _WEB_POSITIVE_RE.search(marker):
            add(node)
            if len(unique) >= 12:
                break
    if len(unique) < 16:
        for node in containers:
            direct_paragraphs = sum(
                1 for child in node if str(child.tag).casefold() == "p")
            if direct_paragraphs >= 2:
                add(node)
                if len(unique) >= 16:
                    break
    bodies = doc.xpath("//body")
    if bodies:
        add(bodies[0])
    if not unique:
        return doc
    return max(unique, key=_web_candidate_score)


def _web_blocks(root):
    xpath = (
        ".//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or "
        "self::h6 or self::p or self::blockquote or self::pre or self::li or "
        "self::figcaption or self::tr]"
    )
    nodes = list(root.xpath(xpath))
    if str(getattr(root, "tag", "")).casefold() in {
            "h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote",
            "pre", "li", "figcaption", "tr"}:
        nodes.insert(0, root)
    blocks, seen, truncated = [], set(), False
    for node in nodes:
        tag = str(node.tag).casefold()
        if tag in ("li", "blockquote", "tr") and node.xpath(
                ".//*[self::p or self::li or self::blockquote]"):
            continue
        if tag == "tr":
            cells = [_clean_web_text(cell.text_content()) for cell in
                     node.xpath("./th|./td")]
            text = ". ".join(cell for cell in cells if cell)
        else:
            text = _clean_web_text(node.text_content())
        if not text:
            continue
        links = sum(len(_clean_web_text(link.text_content()))
                    for link in node.xpath(".//a"))
        if len(text) < 250 and links / max(len(text), 1) > 0.8:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        ancestor_tags = {
            str(ancestor.tag).casefold() for ancestor in node.iterancestors()}
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            kind = "heading"
        elif tag == "blockquote" or "blockquote" in ancestor_tags:
            kind = "quote"
        elif tag == "pre" or "pre" in ancestor_tags:
            kind = "pre"
        elif tag == "li" or "li" in ancestor_tags:
            kind = "list"
        elif tag == "figcaption":
            kind = "caption"
        else:
            kind = "body"
        blocks.append((kind, text))
        if len(blocks) >= WEB_BLOCK_LIMIT:
            truncated = True
            break
    if not blocks:
        text = _clean_web_text(root.text_content())
        if text:
            blocks.append(("body", text))
    else:
        # Some pages put prose directly in div/span/br structures alongside
        # normal paragraphs. If structured blocks cover too little of the
        # selected article, prefer the complete clean text over omitting prose.
        full_text = _clean_web_text(root.text_content())
        extracted_length = sum(len(text) for _, text in blocks)
        if full_text and extracted_length < len(full_text) * 0.75:
            blocks = [("body", full_text)]
            truncated = False
    return blocks, truncated


def extract_webpage_content(data, final_url, charset=None, content_type="text/html"):
    """Extract a clean, ordered Reader View from downloaded page bytes."""
    if content_type == "text/plain":
        text = _clean_web_text(_decode_web_bytes(data, charset))
        if len(text.split()) < 5:
            raise RuntimeError("no readable text was found on that page")
        title = urllib.parse.urlsplit(final_url).path.rsplit("/", 1)[-1]
        title = urllib.parse.unquote(title) or urllib.parse.urlsplit(final_url).hostname
        return {
            "url": final_url, "title": title, "site": urllib.parse.urlsplit(
                final_url).hostname or "Webpage", "byline": "",
            "language": "", "blocks": [("body", text[:WEB_TEXT_LIMIT])],
            "word_count": len(text[:WEB_TEXT_LIMIT].split()),
            "truncated": len(text) > WEB_TEXT_LIMIT,
        }

    if not module_ok("lxml"):
        raise RuntimeError("the webpage reader needs lxml; installation failed")
    from lxml import html as lxml_html
    html_text = _decode_web_bytes(data, charset)
    html_text = re.sub(
        r"^\s*<\?xml[^>]*\?>", "", html_text, count=1, flags=re.I)
    parser = lxml_html.HTMLParser(
        recover=True, no_network=True, remove_comments=True, huge_tree=False)
    try:
        doc = lxml_html.document_fromstring(
            html_text, parser=parser, base_url=final_url)
    except Exception as exc:
        raise RuntimeError("the webpage HTML could not be read") from exc
    if doc.xpath("count(//*)") > 40000:
        raise RuntimeError("the webpage structure is too large for Reader View")

    jsonld = _web_jsonld_article(doc)
    title = (_web_meta(doc, "og:title", "twitter:title", "headline") or
             jsonld.get("title", ""))
    if not title:
        headings = doc.xpath("//h1[normalize-space()]")
        if headings:
            title = _clean_web_text(headings[0].text_content())
    if not title:
        titles = doc.xpath("//title[normalize-space()]")
        if titles:
            title = _clean_web_text(titles[0].text_content())
    host = urllib.parse.urlsplit(final_url).hostname or "Webpage"
    site = _web_meta(doc, "og:site_name", "application-name") or host.removeprefix("www.")
    byline = (_web_meta(doc, "author", "article:author", "byl") or
              jsonld.get("author", ""))
    language = _clean_web_text(doc.get("lang", ""))

    _drop_web_noise(doc)
    root = _web_content_root(doc)
    blocks, structure_truncated = _web_blocks(root)
    visible_length = sum(len(text) for _, text in blocks)
    visible_words = sum(len(text.split()) for _, text in blocks)
    json_body = jsonld.get("body", "")
    if json_body and (visible_words < 20 or
                      len(json_body) > max(200, int(visible_length * 1.35))):
        paragraphs = [
            _clean_web_text(item) for item in re.split(r"\n\s*\n|\r\n", json_body)]
        clean_paragraphs = [item for item in paragraphs if item]
        structure_truncated = len(clean_paragraphs) > WEB_BLOCK_LIMIT
        blocks = [("body", item) for item in
                  clean_paragraphs[:WEB_BLOCK_LIMIT]]

    title = (_clean_web_text(title) or site)[:500]
    site = _clean_web_text(site)[:200]
    byline = _clean_web_text(byline)[:300]
    language = language[:40]
    while blocks and blocks[0][1].casefold() == title.casefold():
        blocks.pop(0)
    if byline and any(byline.casefold() in text.casefold()
                      for _, text in blocks[:3]):
        byline = ""

    limited, total, truncated = [], 0, structure_truncated
    for kind, text in blocks:
        remaining = WEB_TEXT_LIMIT - total
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[:remaining].rsplit(" ", 1)[0].strip()
            truncated = True
        if text:
            limited.append((kind, text))
            total += len(text) + 2
    readable = " ".join(text for _, text in limited)
    word_count = len(readable.split())
    if word_count < 5:
        raise RuntimeError(
            "no readable article text was found; the page may require sign-in "
            "or JavaScript")
    return {
        "url": final_url, "title": title, "site": site, "byline": byline,
        "language": language, "blocks": limited, "word_count": word_count,
        "truncated": truncated,
    }


def load_webpage(url, status_cb):
    """Download one public page and return its local Reader View data."""
    url = normalize_web_url(url)
    host = urllib.parse.urlsplit(url).hostname or "webpage"
    status_cb(f"Loading webpage from {host}...")
    if not module_ok("lxml") and not pip_install("lxml", status_cb):
        raise RuntimeError("could not install the webpage reader (lxml)")
    deadline = time.monotonic() + WEB_TOTAL_TIMEOUT
    _validate_public_web_target(url)
    headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.8",
    }
    opener = urllib.request.build_opener(_WebRedirectHandler())
    current_url, redirects = url, 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("webpage request took longer than 20 seconds")
        request = urllib.request.Request(current_url, headers=headers)
        try:
            response = opener.open(
                request, timeout=min(WEB_REQUEST_TIMEOUT, remaining))
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise RuntimeError(
                        f"webpage redirect {code} had no destination")
                if redirects >= 5:
                    raise RuntimeError("webpage redirected too many times")
                target = normalize_web_url(
                    urllib.parse.urljoin(current_url, location))
                old_scheme = urllib.parse.urlsplit(
                    current_url).scheme.casefold()
                new_scheme = urllib.parse.urlsplit(target).scheme.casefold()
                if old_scheme == "https" and new_scheme != "https":
                    raise RuntimeError(
                        "secure webpage redirected to an insecure HTTP address")
                _validate_public_web_target(target)
                current_url = target
                redirects += 1
                continue
            exc.close()
            if code in (401, 403):
                raise RuntimeError(
                    "the webpage blocked access or requires sign-in") from exc
            if code == 404:
                raise RuntimeError("the webpage was not found (404)") from exc
            if code == 429:
                raise RuntimeError(
                    "the webpage asked Piper Reader to slow down (429)") from exc
            raise RuntimeError(f"webpage request failed (HTTP {code})") from exc
        except urllib.error.URLError as exc:
            reason = _clean_web_text(str(exc.reason))
            raise RuntimeError(
                f"could not connect to the webpage: {reason}") from exc

        with response:
            final_url = normalize_web_url(response.geturl())
            _validate_public_web_target(final_url)
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "webpage request took longer than 20 seconds")
            declared_content_type = response.headers.get("Content-Type")
            content_type = response.headers.get_content_type().casefold()
            allowed = {"text/html", "application/xhtml+xml", "text/plain"}
            if content_type not in allowed:
                raise RuntimeError(
                    f"that address returned {content_type}, not a webpage")
            data = _read_web_body(response, deadline)
            charset = response.headers.get_content_charset()
            if not declared_content_type and content_type == "text/plain":
                prefix = data[:4096].lstrip().lower()
                if (prefix.startswith((b"<!doctype html", b"<html", b"<?xml"))
                        or b"<head" in prefix or b"<body" in prefix):
                    content_type = "text/html"
        break
    return extract_webpage_content(data, final_url, charset, content_type)


# ------------------------------------------------------------ document loading
class OcrCancelled(RuntimeError):
    """Internal signal used when a newer content request supersedes OCR."""


_OCR_MODEL_PREP_LOCK = threading.Lock()


def normalize_ocr_mode(value):
    """Return a saved OCR mode key from either its key or UI label."""
    text = str(value or "auto").strip().casefold()
    if text in OCR_MODE_LABELS:
        return text
    return OCR_LABEL_MODES.get(text, "auto")


def resolve_ocr_profile(mode, voice_language=None):
    """Resolve Auto against the reading voice without touching Tk in a worker."""
    selected = normalize_ocr_mode(mode)
    language = str(voice_language or "").strip().casefold()
    if selected == "auto":
        if language.startswith("ar"):
            selected = "arabic"
        elif language.startswith("ha"):
            selected = "hausa"
        else:
            selected = "english"
    if selected == "arabic":
        return {
            "mode": selected,
            "language": "ara+eng",
            "label": "Arabic + English",
            "warning": "",
        }
    if selected == "hausa":
        return {
            "mode": selected,
            "language": "eng",
            "label": "Hausa (basic Latin fallback)",
            "warning": (
                "Hausa OCR uses a basic-Latin fallback; hooked letters such "
                "as ɓ, ɗ, ƙ, and ƴ may be misread."
            ),
        }
    return {
        "mode": "english",
        "language": "eng",
        "label": "English",
        "warning": "",
    }


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _ocr_model_is_valid(directory, code):
    metadata = OCR_MODEL_FILES.get(code)
    if not metadata:
        return False
    path = os.path.join(directory, metadata["filename"])
    try:
        if os.path.getsize(path) != metadata["size"]:
            return False
        return hmac.compare_digest(
            _sha256_file(path).casefold(), metadata["sha256"].casefold())
    except OSError:
        return False


def ensure_ocr_tessdata(language, status_cb=lambda _message: None,
                         is_current=lambda: True):
    """Return one verified tessdata folder, repairing from pinned sources."""
    codes = [code for code in str(language).split("+") if code]
    if not codes or any(code not in OCR_MODEL_FILES for code in codes):
        raise RuntimeError(f"unsupported OCR language: {language}")

    candidates = []
    for directory in (BUNDLED_OCR_TESSDATA_DIR, OCR_TESSDATA_DIR):
        normalized = os.path.normcase(os.path.abspath(directory))
        if normalized not in {item[0] for item in candidates}:
            candidates.append((normalized, directory))
    for _, directory in candidates:
        if all(_ocr_model_is_valid(directory, code) for code in codes):
            return directory

    with _OCR_MODEL_PREP_LOCK:
        if not is_current():
            raise OcrCancelled()
        os.makedirs(OCR_TESSDATA_DIR, exist_ok=True)
        for code in codes:
            if not is_current():
                raise OcrCancelled()
            if _ocr_model_is_valid(OCR_TESSDATA_DIR, code):
                continue
            metadata = OCR_MODEL_FILES[code]
            destination = os.path.join(
                OCR_TESSDATA_DIR, metadata["filename"])
            bundled = os.path.join(
                BUNDLED_OCR_TESSDATA_DIR, metadata["filename"])
            if _ocr_model_is_valid(BUNDLED_OCR_TESSDATA_DIR, code):
                temporary = destination + f".copy-{os.getpid()}.part"
                try:
                    shutil.copyfile(bundled, temporary)
                    if (os.path.getsize(temporary) != metadata["size"] or
                            not hmac.compare_digest(
                                _sha256_file(temporary).casefold(),
                                metadata["sha256"].casefold())):
                        raise RuntimeError(
                            f"Bundled {metadata['label']} is damaged")
                    os.replace(temporary, destination)
                finally:
                    try:
                        os.remove(temporary)
                    except OSError:
                        pass
                continue
            try:
                download_file(
                    metadata["url"], destination, status_cb,
                    metadata["label"],
                    expected_sha256=metadata["sha256"],
                    expected_size=metadata["size"])
            except Exception as exc:
                raise RuntimeError(
                    f"Could not prepare {metadata['label']}. Reinstall Piper "
                    f"Reader or connect once and retry. ({exc})") from exc
        if not all(_ocr_model_is_valid(OCR_TESSDATA_DIR, code)
                   for code in codes):
            raise RuntimeError("OCR language data did not pass its safety check")
        return OCR_TESSDATA_DIR


def page_needs_ocr(words):
    """Identify pages whose ordinary PDF text is blank or only incidental."""
    texts = [
        str(word[4]).strip() for word in words
        if len(word) >= 5 and str(word[4]).strip()
    ]
    readable_characters = sum(
        1 for character in "".join(texts)
        if character.isalnum())
    return (len(texts) < OCR_NATIVE_TEXT_MIN_WORDS or
            readable_characters < OCR_NATIVE_TEXT_MIN_CHARS)


def bounded_ocr_dpi(page_width, page_height, requested=OCR_DPI):
    """Keep a single OCR render within a predictable memory envelope."""
    width = max(float(page_width), 1.0)
    height = max(float(page_height), 1.0)
    requested = max(int(requested), OCR_MIN_DPI)
    pixels = width * height * (requested / 72.0) ** 2
    if pixels <= OCR_MAX_RENDER_PIXELS:
        return requested
    reduced = int(requested * (OCR_MAX_RENDER_PIXELS / pixels) ** 0.5)
    if reduced < OCR_MIN_DPI:
        raise RuntimeError(
            "a document page is too large to OCR safely; resize the scan and "
            "try again")
    return reduced


def image_to_pdf(path, output_path):
    """Convert a supported image (including multi-page TIFF) to a PDF view."""
    if os.path.getsize(path) > OCR_MAX_IMAGE_FILE_BYTES:
        raise RuntimeError("image is larger than the 500 MB safety limit")
    import fitz
    try:
        image_document = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"could not open that image: {exc}") from exc
    temporary = output_path + ".part.pdf"
    try:
        if len(image_document) < 1:
            raise RuntimeError("image has no readable pages")
        if len(image_document) > OCR_MAX_PAGES_PER_JOB:
            raise RuntimeError(
                f"image has more than {OCR_MAX_PAGES_PER_JOB} frames")
        converted = image_document.convert_to_pdf()
        with fitz.open("pdf", converted) as source_pdf, fitz.open() as output_pdf:
            for page_number in range(len(source_pdf)):
                source_page = source_pdf[page_number]
                longest = max(source_page.rect.width,
                              source_page.rect.height, 1.0)
                scale = min(1.0, OCR_IMAGE_PAGE_MAX_POINTS / longest)
                target_page = output_pdf.new_page(
                    width=max(1.0, source_page.rect.width * scale),
                    height=max(1.0, source_page.rect.height * scale))
                target_page.show_pdf_page(
                    target_page.rect, source_pdf, page_number,
                    keep_proportion=True)
            output_pdf.save(temporary, garbage=4, deflate=True)
        os.replace(temporary, output_path)
        return output_path
    finally:
        image_document.close()
        try:
            os.remove(temporary)
        except OSError:
            pass


def recognize_pdf_pages(path, language, language_label, status_cb,
                        is_current=lambda: True, before_page=None,
                        confirm_pages=None, force=False, dpi=OCR_DPI):
    """OCR image-only PDF pages and return serializable PyMuPDF word tuples."""
    import fitz
    if not is_current():
        raise OcrCancelled()
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise RuntimeError(f"could not open document: {exc}") from exc
    with document:
        if document.needs_pass:
            raise RuntimeError(
                "password-protected PDFs must be unlocked before OCR")
        page_count = len(document)
        if not page_count:
            raise RuntimeError("document has no pages")

        targets = []
        native_word_count = 0
        for page_number in range(page_count):
            if not is_current():
                raise OcrCancelled()
            words = document[page_number].get_text("words", sort=False)
            native_word_count += sum(
                1 for word in words
                if len(word) >= 5 and str(word[4]).strip())
            if force or page_needs_ocr(words):
                targets.append(page_number)

        if not targets:
            return {
                "words_by_page": {},
                "attempted_pages": 0,
                "recognized_pages": 0,
                "word_count": native_word_count,
                "language_label": language_label,
                "page_count": page_count,
                "forced": bool(force),
            }
        if len(targets) > OCR_MAX_PAGES_PER_JOB:
            raise RuntimeError(
                f"OCR is limited to {OCR_MAX_PAGES_PER_JOB} scanned pages per "
                "document; split this file into smaller parts")
        if confirm_pages is not None and not confirm_pages(
                len(targets), page_count):
            raise OcrCancelled()

        status_cb(f"Preparing local {language_label} OCR...")
        tessdata = ensure_ocr_tessdata(language, status_cb, is_current)
        words_by_page = {}
        recognized_word_count = 0
        for ordinal, page_number in enumerate(targets, 1):
            if not is_current():
                raise OcrCancelled()
            if before_page is not None and not before_page(
                    page_number, ordinal, len(targets)):
                raise OcrCancelled()
            status_cb(
                f"OCR: page {page_number + 1}/{page_count} "
                f"({ordinal}/{len(targets)}) - {language_label}...")
            page = document[page_number]
            page_dpi = bounded_ocr_dpi(
                page.rect.width, page.rect.height, dpi)
            try:
                textpage = page.get_textpage_ocr(
                    language=language, dpi=page_dpi, full=True,
                    tessdata=tessdata)
                extracted = page.get_text(
                    "words", textpage=textpage, sort=False)
            except Exception as exc:
                raise RuntimeError(
                    f"OCR could not read page {page_number + 1}: {exc}") from exc
            finally:
                try:
                    del textpage
                except UnboundLocalError:
                    pass
            clean_words = [
                tuple(word[:8]) for word in extracted
                if len(word) >= 8 and str(word[4]).strip()
            ]
            if clean_words:
                words_by_page[page_number] = clean_words
                recognized_word_count += len(clean_words)

        return {
            "words_by_page": words_by_page,
            "attempted_pages": len(targets),
            "recognized_pages": len(words_by_page),
            "word_count": native_word_count + recognized_word_count,
            "language_label": language_label,
            "page_count": page_count,
            "forced": bool(force),
        }


def _open_shared_binary(path):
    """Open a file for reading even while Word has it open for editing."""
    if os.name != "nt":
        return open(path, "rb")

    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.path.abspath(path),
        0x80000000,                  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x00000080 | 0x08000000, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


def _docx_review_content(path):
    """Return final/current DOCX text plus a review-markup summary."""
    import zipfile
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with _open_shared_binary(path) as source:
        package = source.read()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        document_root = ET.fromstring(archive.read("word/document.xml"))
        settings_root = None
        comments_root = None
        if "word/settings.xml" in archive.namelist():
            settings_root = ET.fromstring(archive.read("word/settings.xml"))
        if "word/comments.xml" in archive.namelist():
            comments_root = ET.fromstring(archive.read("word/comments.xml"))

    excluded = {W + "del", W + "moveFrom"}

    def paragraph_text(paragraph):
        pieces = []

        def visit(node, hidden=False):
            hidden = hidden or node.tag in excluded
            if not hidden:
                if node.tag == W + "t" and node.text:
                    pieces.append(node.text)
                elif node.tag == W + "tab":
                    pieces.append("\t")
                elif node.tag == W + "br":
                    pieces.append("\n")
            for child in node:
                visit(child, hidden)

        visit(paragraph)
        return "".join(pieces).strip()

    paragraphs = []
    for paragraph in document_root.iter(W + "p"):
        text = paragraph_text(paragraph)
        if text:
            paragraphs.append(text)

    insertions = sum(1 for node in document_root.iter()
                     if node.tag in {W + "ins", W + "moveTo"})
    deletions = sum(1 for node in document_root.iter()
                    if node.tag in {W + "del", W + "moveFrom"})
    format_changes = sum(
        1 for node in document_root.iter()
        if node.tag.rsplit("}", 1)[-1].endswith("PrChange"))
    comments = (sum(1 for node in comments_root.iter(W + "comment"))
                if comments_root is not None else 0)
    tracking_enabled = bool(
        settings_root is not None and
        settings_root.find(".//" + W + "trackRevisions") is not None)
    tracked_change_count = insertions + deletions + format_changes
    summary = {
        "insertions": insertions,
        "deletions": deletions,
        "format_changes": format_changes,
        "comments": comments,
        "tracking_enabled": tracking_enabled,
        "tracked_change_count": tracked_change_count,
        "has_markup": bool(tracked_change_count or comments),
    }
    return "\n\n".join(paragraphs), summary


def _normalize_alignment_token(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in value
                   if character.isalnum())


def align_spoken_text_to_pdf_words(spoken_text, words_by_page):
    """Map final DOCX words onto a markup PDF, excluding deleted/comments."""
    import bisect
    import difflib

    source_words = []
    for paragraph_id, paragraph in enumerate(
            re.split(r"\n{2,}", str(spoken_text or "").strip())):
        for token_id, token in enumerate(re.findall(r"\S+", paragraph)):
            source_words.append((token, paragraph_id, token_id))

    pdf_words = []
    for page_number in sorted(words_by_page):
        words = [word for word in words_by_page[page_number]
                 if len(word) >= 8 and str(word[4]).strip()]
        words.sort(key=lambda word: (word[5], word[6], word[7]))
        pdf_words.extend((page_number, word) for word in words)
    if not source_words or not pdf_words:
        return {}, {
            "source_words": len(source_words),
            "matched_words": 0,
            "confidence": 0.0,
        }

    source_alignment = [
        (index, _normalize_alignment_token(word[0]))
        for index, word in enumerate(source_words)
        if _normalize_alignment_token(word[0])
    ]
    pdf_alignment = [
        (index, _normalize_alignment_token(word[1][4]))
        for index, word in enumerate(pdf_words)
        if _normalize_alignment_token(word[1][4])
    ]
    source_values = [value for _index, value in source_alignment]
    pdf_values = [value for _index, value in pdf_alignment]
    matcher = difflib.SequenceMatcher(
        None, source_values, pdf_values, autojunk=True)
    mapping = {}

    for tag, source_start, source_end, pdf_start, pdf_end in (
            matcher.get_opcodes()):
        if tag == "equal":
            for source_pos, pdf_pos in zip(
                    range(source_start, source_end),
                    range(pdf_start, pdf_end)):
                mapping[source_alignment[source_pos][0]] = (
                    pdf_alignment[pdf_pos][0])
            continue
        if tag != "replace":
            continue
        cursor = pdf_start
        for source_pos in range(source_start, source_end):
            source_index, source_value = source_alignment[source_pos]
            best = None
            for pdf_pos in range(cursor, pdf_end):
                pdf_index, pdf_value = pdf_alignment[pdf_pos]
                if source_value == pdf_value:
                    score = 4.0
                elif (min(len(source_value), len(pdf_value)) >= 2 and
                      source_value in pdf_value):
                    score = 3.0 + len(source_value) / len(pdf_value)
                elif (min(len(source_value), len(pdf_value)) >= 2 and
                      pdf_value in source_value):
                    score = 2.0 + len(pdf_value) / len(source_value)
                else:
                    continue
                candidate = (score, -pdf_pos, pdf_pos, pdf_index)
                if best is None or candidate > best:
                    best = candidate
            if best is not None:
                _score, _negative_position, pdf_pos, pdf_index = best
                mapping[source_index] = pdf_index
                cursor = pdf_pos

    matched_words = sum(
        1 for index, normalized in source_alignment
        if normalized and index in mapping)
    mapped_source_indexes = sorted(mapping)
    if mapped_source_indexes:
        for source_index in range(len(source_words)):
            if source_index in mapping:
                continue
            position = bisect.bisect_left(
                mapped_source_indexes, source_index)
            candidates = []
            if position:
                candidates.append(mapped_source_indexes[position - 1])
            if position < len(mapped_source_indexes):
                candidates.append(mapped_source_indexes[position])
            nearest = min(candidates,
                          key=lambda item: abs(item - source_index))
            mapping[source_index] = mapping[nearest]
    else:
        denominator = max(1, len(source_words) - 1)
        for source_index in range(len(source_words)):
            mapping[source_index] = round(
                source_index * (len(pdf_words) - 1) / denominator)

    # SequenceMatcher and the local replacement search are monotonic. Clamp
    # defensive fallbacks so source order can never move backward a page.
    previous_pdf_index = 0
    for source_index in range(len(source_words)):
        pdf_index = max(previous_pdf_index, mapping[source_index])
        mapping[source_index] = min(pdf_index, len(pdf_words) - 1)
        previous_pdf_index = mapping[source_index]

    aligned = {}
    for source_index, (token, paragraph_id, token_id) in enumerate(
            source_words):
        page_number, pdf_word = pdf_words[mapping[source_index]]
        x0, y0, x1, y1 = (float(value) for value in pdf_word[:4])
        source_value = _normalize_alignment_token(token)
        pdf_value = _normalize_alignment_token(pdf_word[4])
        if (source_value and source_value != pdf_value and
                source_value in pdf_value and x1 > x0):
            start = pdf_value.find(source_value)
            end = start + len(source_value)
            width = x1 - x0
            first_character = token.lstrip()[:1]
            rtl = bool(first_character and unicodedata.bidirectional(
                first_character) in {"R", "AL", "AN"})
            if rtl:
                new_x1 = x1 - width * start / len(pdf_value)
                new_x0 = x1 - width * end / len(pdf_value)
            else:
                new_x0 = x0 + width * start / len(pdf_value)
                new_x1 = x0 + width * end / len(pdf_value)
            x0, x1 = new_x0, new_x1
        aligned.setdefault(page_number, []).append((
            x0, y0, x1, y1, token, paragraph_id, 0, token_id))

    alignment_total = len(source_alignment)
    return aligned, {
        "source_words": len(source_words),
        "matched_words": matched_words,
        "confidence": (matched_words / alignment_total
                       if alignment_total else 1.0),
    }


def extract_document(path, status_cb):
    """Return the plain text of a .pdf / .docx / text file."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        if not module_ok("pypdf") and not pip_install("pypdf", status_cb):
            raise RuntimeError("could not install the PDF reader (pypdf)")
        from pypdf import PdfReader
        reader = PdfReader(path)
        n = len(reader.pages)
        out = []
        for i, page in enumerate(reader.pages, 1):
            if n > 20 and i % 10 == 0:
                status_cb(f"Reading PDF pages... {i}/{n}")
            out.append(page.extract_text() or "")
        text = "\n\n".join(out)
        if not text.strip():
            raise RuntimeError(
                "no readable text found; reopen with the OCR reader installed")
        return text

    if ext == ".docx":
        text, _summary = _docx_review_content(path)
        if not text.strip():
            raise RuntimeError("no text found in document")
        return text

    if ext == ".doc":
        raise RuntimeError("old .doc format - open it in Word, save as .docx")

    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def docx_to_pdf(path, output_dir=None):
    """Convert a read-only DOCX snapshot to PDF with Word review markup."""
    import pythoncom
    from win32com.client import DispatchEx
    pythoncom.CoInitialize()
    word = None
    document = None
    source_snapshot = None
    out = None
    try:
        output_dir = output_dir or tempfile.gettempdir()
        os.makedirs(output_dir, exist_ok=True)
        unique = f"{os.getpid()}-{time.time_ns()}"
        out = os.path.join(
            output_dir, f"word-{unique}.pdf")
        source_snapshot = os.path.join(
            output_dir, f"word-source-{unique}.docx")
        with _open_shared_binary(path) as source, open(
                source_snapshot, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)

        word = DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception:
            pass
        document = word.Documents.Open(
            os.path.abspath(source_snapshot), False, True, False)
        document.ShowRevisions = True
        document.PrintRevisions = True
        try:
            view = document.ActiveWindow.View
            view.ShowRevisionsAndComments = True
            view.ShowInsertionsAndDeletions = True
            view.ShowFormatChanges = True
            view.ShowComments = True
            view.RevisionsView = 0        # wdRevisionsViewFinal
            view.MarkupMode = 1           # wdInLineRevisions
        except Exception:
            # Item=7 below is the authoritative export switch. Older Word
            # versions may not expose every optional View property.
            pass
        document.ExportAsFixedFormat(
            out, 17, False, 0, 0, 1, 1, 7,
            True, True, 1, True, True, False)
        return out
    except Exception:
        if out and os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        raise
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        if source_snapshot and os.path.exists(source_snapshot):
            try:
                os.remove(source_snapshot)
            except OSError:
                pass
        pythoncom.CoUninitialize()


def merge_pdf_highlight_rectangles(rects):
    """Join neighboring words on one line into smoother highlight bands."""
    cleaned = []
    for rect in rects or ():
        if len(rect) < 4:
            continue
        try:
            x0, y0, x1, y1 = (float(value) for value in rect[:4])
        except (TypeError, ValueError):
            continue
        if x1 > x0 and y1 > y0:
            cleaned.append((x0, y0, x1, y1))
    cleaned.sort(key=lambda item: ((item[1] + item[3]) / 2.0, item[0]))
    merged = []
    for rect in cleaned:
        if merged:
            previous = merged[-1]
            overlap = min(previous[3], rect[3]) - max(previous[1], rect[1])
            min_height = min(previous[3] - previous[1], rect[3] - rect[1])
            center_gap = abs(
                (previous[1] + previous[3]) / 2.0 -
                (rect[1] + rect[3]) / 2.0)
            same_line = (overlap >= min_height * 0.45 or
                         center_gap <= max(2.0, min_height * 0.35))
            word_gap = rect[0] - previous[2]
            max_gap = max(4.0, min_height * 0.75)
            if same_line and word_gap <= max_gap:
                merged[-1] = (
                    min(previous[0], rect[0]), min(previous[1], rect[1]),
                    max(previous[2], rect[2]), max(previous[3], rect[3]))
                continue
        merged.append(rect)
    return merged


def create_pdf_highlight_overlay(rects, zoom):
    """Return a compact antialiased RGBA highlight and its page-pixel origin."""
    from PIL import Image, ImageDraw

    zoom = max(0.01, float(zoom))
    merged = merge_pdf_highlight_rectangles(rects)
    if not merged:
        return None, 0, 0
    padding = max(2, int(round(2.0 * zoom)))
    left = math.floor(min(rect[0] for rect in merged) * zoom) - padding
    top = math.floor(min(rect[1] for rect in merged) * zoom) - padding
    right = math.ceil(max(rect[2] for rect in merged) * zoom) + padding
    bottom = math.ceil(max(rect[3] for rect in merged) * zoom) + padding
    overlay = Image.new(
        "RGBA", (max(1, right - left), max(1, bottom - top)),
        (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    radius = max(2, int(round(2.0 * zoom)))
    stroke_width = max(1, int(round(0.8 * zoom)))
    for x0, y0, x1, y1 in merged:
        box = (
            int(round(x0 * zoom)) - left,
            int(round(y0 * zoom)) - top,
            int(round(x1 * zoom)) - left,
            int(round(y1 * zoom)) - top,
        )
        draw.rounded_rectangle(
            box, radius=radius, fill=(76, 124, 255, 64),
            outline=(60, 99, 243, 104), width=stroke_width)
    return overlay, left, top


class PdfViewer(tk.Frame):
    """Speechify-style view: real PDF pages + highlight that follows the voice."""

    def __init__(self, master, path, title, close_cb, click_cb=None,
                 ocr_cb=None, ocr_language_var=None,
                 ocr_language_changed_cb=None, ocr_result=None,
                 spoken_text=None, review_summary=None):
        super().__init__(master, bg=CARD)
        self.path = path
        self.title = title
        self.click_cb = click_cb
        self.ocr_cb = ocr_cb
        self.ocr_result = ocr_result or {}
        self.ocr_words_by_page = dict(
            self.ocr_result.get("words_by_page") or {})
        self.spoken_text = spoken_text
        self.review_summary = dict(review_summary or {})
        self.speech_alignment = {}
        import fitz
        self.fitz = fitz
        self.doc = fitz.open(path)
        self.chunks = []
        self.repeat_groups = []
        self.images = {}
        self.focused_index = None
        self.page_widths = []
        self.page_heights = []
        self.page_x_offsets = []
        self.page_rectangles = []
        self.layout_width = 0.0
        self._recenter_after_id = None
        self._dpi_refreshing = False
        self._highlight_photo = None

        hdr = tk.Frame(self, bg=CARD)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Button(hdr, text="Close document", command=close_cb, bd=0,
                  bg=ACCENT_SOFT, fg=ACCENT_DARK,
                  activebackground=BTN_HOVER,
                  activeforeground=ACCENT_DARK, cursor="hand2",
                  highlightthickness=0, padx=10, pady=4,
                  font=(FONT_UI, 9, "bold")).pack(side="right")
        if ocr_cb is not None and ocr_language_var is not None:
            used_ocr = bool(self.ocr_result.get("attempted_pages"))
            self.ocr_button = tk.Button(
                hdr, text="Retry OCR" if used_ocr else "Recognize text",
                command=ocr_cb, bd=0, bg=CARD_SOFT, fg=ACCENT_DARK,
                activebackground=BTN_HOVER, activeforeground=ACCENT_DARK,
                cursor="hand2", highlightthickness=0, padx=10, pady=4,
                font=(FONT_UI, 9, "bold"))
            self.ocr_button.pack(side="right", padx=(6, 0))
            self.ocr_language_combo = ttk.Combobox(
                hdr, textvariable=ocr_language_var,
                values=list(OCR_MODE_LABELS.values()), state="readonly",
                width=25, style="CompactVoice.TCombobox",
                font=(FONT_UI, 9))
            self.ocr_language_combo.pack(side="right", padx=(6, 0))
            if ocr_language_changed_cb is not None:
                self.ocr_language_combo.bind(
                    "<<ComboboxSelected>>", ocr_language_changed_cb)
            tk.Label(hdr, text="OCR", bg=CARD, fg=INK_MUTED,
                     font=(FONT_UI, 9)).pack(side="right", padx=(8, 0))
        tk.Label(hdr, text=title, bg=CARD, fg=INK, anchor="w",
                 font=(FONT_UI, 10, "bold")).pack(
                     side="left", fill="x", expand=True)

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True)
        self.vsb = ttk.Scrollbar(body, orient="vertical")
        self.vsb.pack(side="right", fill="y")
        self.hsb = ttk.Scrollbar(body, orient="horizontal")
        self.canvas = tk.Canvas(body, bg=CARD_BLUE, highlightthickness=0, bd=0,
                                yscrollcommand=self._scrollset,
                                xscrollcommand=self.hsb.set)
        self.canvas.pack(fill="both", expand=True)
        self.vsb.configure(command=self._yview)
        self.hsb.configure(command=self.canvas.xview)

        first = self.doc[0]
        self.display_dpi = get_window_dpi(master)
        self.base_zoom = pdf_fit_zoom(first.rect.width, self.display_dpi)
        self.zoom = self.base_zoom
        self.offsets = []
        self.total_h = 0
        self._layout_pages()
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._horizontal_wheel)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.configure(cursor="hand2")
        self.after(80, self._render_visible)

    def go_to_destination(self, page, y=0.0):
        """Navigate an internal outline target at the current display zoom."""
        if type(page) is not int or not 0 <= page < len(self.doc):
            return False
        try:
            y = float(y)
        except (TypeError, ValueError):
            y = 0.0
        if not math.isfinite(y):
            y = 0.0
        y = max(0.0, min(y, self.doc[page].rect.height))
        self.focused_index = None
        self.canvas.delete("hl")
        self._highlight_photo = None
        target = self.offsets[page] + y * self.zoom - DOCUMENT_PAGE_MARGIN
        self.canvas.yview_moveto(max(0.0, min(1.0, target / max(self.total_h, 1))))
        self._render_visible()
        return True

    def _scrollset(self, a, b):
        self.vsb.set(a, b)
        self.after_idle(self._render_visible)

    def _yview(self, *args):
        self.canvas.yview(*args)
        self.after_idle(self._render_visible)

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
        self.after_idle(self._render_visible)

    def _horizontal_wheel(self, e):
        self.canvas.xview_scroll(int(-e.delta / 120) * 3, "units")

    def _canvas_resized(self, _event=None):
        if self._refresh_display_dpi():
            return
        if self._recenter_after_id is not None:
            try:
                self.after_cancel(self._recenter_after_id)
            except Exception:
                pass
        self._recenter_after_id = self.after_idle(self._recenter_pages)

    def _refresh_display_dpi(self):
        """Rerender cached pages if the window crosses to another monitor."""
        if self._dpi_refreshing:
            return False
        self._dpi_refreshing = True
        try:
            new_dpi = get_window_dpi(self)
            old_dpi = float(getattr(self, "display_dpi", new_dpi))
            if abs(new_dpi - old_dpi) < 0.5:
                return False
            old_base = max(float(self.base_zoom), 0.0001)
            zoom_ratio = float(self.zoom) / old_base
            old_y = self.canvas.yview()[0] if self.total_h else 0.0
            old_x = self.canvas.xview()[0] if self.content_width else 0.0
            focused = self.focused_index
            self.display_dpi = new_dpi
            self.base_zoom = pdf_fit_zoom(
                self.doc[0].rect.width, self.display_dpi)
            self.zoom = self.base_zoom * max(0.7, min(2.0, zoom_ratio))
            self._layout_pages()
            self.canvas.yview_moveto(old_y)
            self.canvas.xview_moveto(old_x)
            self._render_visible()
            if focused is not None:
                self.focus_chunk(focused, scroll=False)
            return True
        finally:
            self._dpi_refreshing = False

    @staticmethod
    def centered_page_layout(page_widths, viewport_width,
                             margin=DOCUMENT_PAGE_MARGIN):
        """Return scroll width and X offsets that center every PDF page."""
        widths = [max(0.0, float(width)) for width in page_widths]
        viewport_width = max(1.0, float(viewport_width))
        natural_width = (max(widths, default=0.0) + margin * 2.0)
        layout_width = max(natural_width, viewport_width)
        offsets = [max(margin, (layout_width - width) / 2.0)
                   for width in widths]
        return natural_width, layout_width, offsets

    def _page_x(self, page_number):
        try:
            return self.page_x_offsets[page_number]
        except (IndexError, TypeError):
            return DOCUMENT_PAGE_MARGIN

    def _recenter_pages(self):
        self._recenter_after_id = None
        if not self.winfo_exists() or not self.page_widths:
            return
        content_width, layout_width, page_x_offsets = (
            self.centered_page_layout(
                self.page_widths, self.canvas.winfo_width()))
        self.content_width = content_width
        self.layout_width = layout_width
        self.page_x_offsets = page_x_offsets
        for index, (width, height, x, y) in enumerate(zip(
                self.page_widths, self.page_heights,
                self.page_x_offsets, self.offsets)):
            if index < len(self.page_rectangles):
                self.canvas.coords(
                    self.page_rectangles[index], x, y,
                    x + width, y + height)
            if index in self.images:
                self.canvas.coords(f"img{index}", x, y)
        self.canvas.configure(
            scrollregion=(0, 0, self.layout_width, self.total_h))
        self._sync_horizontal_scrollbar()
        if self.focused_index is not None:
            self.focus_chunk(self.focused_index, scroll=False)
        else:
            self._render_visible()

    def _sync_horizontal_scrollbar(self):
        if not self.winfo_exists():
            return
        needs_scroll = self.content_width > self.canvas.winfo_width() + 2
        if needs_scroll and not self.hsb.winfo_ismapped():
            self.hsb.pack(side="bottom", fill="x", before=self.canvas)
        elif not needs_scroll and self.hsb.winfo_ismapped():
            self.hsb.pack_forget()
            self.canvas.xview_moveto(0.0)

    def _layout_pages(self):
        self.canvas.delete("all")
        self.images.clear()
        self._highlight_photo = None
        self.offsets = []
        self.page_widths = []
        self.page_heights = []
        self.page_x_offsets = []
        self.page_rectangles = []
        y, gap = DOCUMENT_PAGE_MARGIN, 14
        for p in self.doc:
            width = p.rect.width * self.zoom
            height = p.rect.height * self.zoom
            self.offsets.append(y)
            self.page_widths.append(width)
            self.page_heights.append(height)
            y += height + gap
        self.total_h = y
        self.content_width, self.layout_width, self.page_x_offsets = (
            self.centered_page_layout(
                self.page_widths, self.canvas.winfo_width()))
        for index, (width, height, x, page_y) in enumerate(zip(
                self.page_widths, self.page_heights,
                self.page_x_offsets, self.offsets)):
            rectangle = self.canvas.create_rectangle(
                x, page_y, x + width, page_y + height, fill=CARD,
                outline=CARD_LINE, tags=("page", f"page{index}"))
            self.page_rectangles.append(rectangle)
        self.canvas.configure(
            scrollregion=(0, 0, self.layout_width, self.total_h))
        self.after_idle(self._sync_horizontal_scrollbar)

    def zoom_by(self, direction):
        """Zoom PDF pages relative to the initial fit-width scale."""
        old_fraction = self.canvas.yview()[0] if self.total_h else 0.0
        old_x_fraction = self.canvas.xview()[0] if self.content_width else 0.0
        factor = 1.15 if direction > 0 else 1.0 / 1.15
        minimum, maximum = self.base_zoom * 0.7, self.base_zoom * 2.0
        new_zoom = max(minimum, min(maximum, self.zoom * factor))
        if abs(new_zoom - self.zoom) < 0.0001:
            return round(self.zoom / self.base_zoom * 100)
        focused = self.focused_index
        self.zoom = new_zoom
        self._layout_pages()
        self.canvas.yview_moveto(old_fraction)
        self.canvas.xview_moveto(old_x_fraction)
        self._render_visible()
        if focused is not None:
            self.focus_chunk(focused, scroll=False)
        return round(self.zoom / self.base_zoom * 100)

    def reset_zoom(self):
        if abs(self.zoom - self.base_zoom) < 0.0001:
            return 100
        focused = self.focused_index
        old_fraction = self.canvas.yview()[0] if self.total_h else 0.0
        self.zoom = self.base_zoom
        self._layout_pages()
        self.canvas.yview_moveto(old_fraction)
        self._render_visible()
        if focused is not None:
            self.focus_chunk(focused, scroll=False)
        return 100

    def _visible_pages(self):
        top = self.canvas.canvasy(0)
        bot = top + max(self.canvas.winfo_height(), 100)
        lo = hi = None
        for i, off in enumerate(self.offsets):
            h = self.doc[i].rect.height * self.zoom
            if off + h >= top and off <= bot:
                if lo is None:
                    lo = i
                hi = i
        return (lo or 0), (hi if hi is not None else 0)

    def _render_visible(self):
        if not self.winfo_exists() or not getattr(self, "offsets", None):
            return
        lo, hi = self._visible_pages()
        for i in range(max(0, lo - 1), min(len(self.offsets), hi + 2)):
            if i not in self.images:
                pm = self.doc[i].get_pixmap(
                    matrix=self.fitz.Matrix(self.zoom, self.zoom))
                img = tk.PhotoImage(data=pm.tobytes("ppm"))
                self.images[i] = img
                self.canvas.create_image(
                    self._page_x(i), self.offsets[i], image=img,
                    anchor="nw", tags=f"img{i}")
                self.canvas.tag_raise("hl")
        for i in list(self.images):
            if i < lo - 3 or i > hi + 4:
                self.canvas.delete(f"img{i}")
                del self.images[i]

    def set_ocr_busy(self, busy):
        button = getattr(self, "ocr_button", None)
        combo = getattr(self, "ocr_language_combo", None)
        if button is not None:
            button.configure(
                state="disabled" if busy else "normal",
                text="Recognizing..." if busy else
                     ("Retry OCR" if self.ocr_words_by_page else
                      "Recognize text"))
        if combo is not None:
            combo.configure(state="disabled" if busy else "readonly")

    def build_chunks(self, max_len, words_by_page=None):
        if words_by_page is not None:
            self.ocr_words_by_page = dict(words_by_page)
        base_words_by_page = {}
        for page_number in range(len(self.doc)):
            if page_number in self.ocr_words_by_page:
                base_words_by_page[page_number] = list(
                    self.ocr_words_by_page[page_number])
            else:
                base_words_by_page[page_number] = list(
                    self.doc[page_number].get_text("words"))
        speech_words_by_page = None
        spoken_text = getattr(self, "spoken_text", None)
        if spoken_text:
            speech_words_by_page, self.speech_alignment = (
                align_spoken_text_to_pdf_words(
                    spoken_text, base_words_by_page))

        chunks = []
        repeat_groups = []
        sentence_id = 0
        for pno in range(len(self.doc)):
            if speech_words_by_page is not None:
                words = list(speech_words_by_page.get(pno, ()))
            else:
                words = list(base_words_by_page[pno])
            words.sort(key=lambda w: (w[5], w[6], w[7]))
            buf, rects, paragraph_id = [], [], None

            def flush(sentence_end=False):
                nonlocal buf, rects, sentence_id
                if not buf:
                    return
                chunks.append((" ".join(buf), pno, rects))
                repeat_groups.append({
                    "sentence_id": sentence_id,
                    "paragraph_id": paragraph_id,
                })
                buf, rects = [], []
                if sentence_end:
                    sentence_id += 1

            for w in words:
                if len(w) < 8 or not str(w[4]).strip():
                    continue
                word_paragraph = f"{pno}:{w[5]}"
                if paragraph_id is not None and word_paragraph != paragraph_id:
                    flush(sentence_end=True)
                paragraph_id = word_paragraph
                buf.append(str(w[4]).strip())
                rects.append(w[:4])
                joined = " ".join(buf)
                sentence_end = (
                    w[4].rstrip(SENTENCE_CLOSERS)[-1:] in
                    SENTENCE_END_CHARS)
                if sentence_end or len(joined) >= max_len:
                    flush(sentence_end=sentence_end)
            flush(sentence_end=True)
        self.chunks = chunks
        self.repeat_groups = repeat_groups
        return chunks

    def _on_click(self, e):
        """Start reading from the sentence the user clicked."""
        if not self.chunks or self.click_cb is None:
            return
        cx = self.canvas.canvasx(e.x)
        cy = self.canvas.canvasy(e.y)
        pno = None
        for i, off in enumerate(self.offsets):
            if off <= cy <= off + self.doc[i].rect.height * self.zoom:
                pno = i
                break
        if pno is None:
            return
        page_x = self._page_x(pno)
        if not page_x <= cx <= page_x + self.page_widths[pno]:
            return
        x = (cx - page_x) / self.zoom
        y = (cy - self.offsets[pno]) / self.zoom
        best, best_d = None, 1e18
        for idx, (txt, p, rects) in enumerate(self.chunks):
            if p != pno:
                continue
            for x0, y0, x1, y1 in rects:
                if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                    self.click_cb(idx)
                    return
                dx = max(x0 - x, 0.0, x - x1)
                dy = max(y0 - y, 0.0, y - y1)
                d = dx * dx + 9 * dy * dy
                if d < best_d:
                    best_d, best = d, idx
        if best is not None and best_d < 40000:
            self.click_cb(best)

    def focus_chunk(self, idx, scroll=True):
        if not self.winfo_exists() or idx >= len(self.chunks):
            return
        self.focused_index = idx
        _, pno, rects = self.chunks[idx]
        z = self.zoom
        oy = self.offsets[pno]
        ox = self._page_x(pno)
        self.canvas.delete("hl")
        self._highlight_photo = None
        used_alpha_overlay = False
        if isinstance(self.canvas, tk.Canvas):
            try:
                from PIL import ImageTk
                overlay, left, top = create_pdf_highlight_overlay(rects, z)
                if overlay is not None:
                    self._highlight_photo = ImageTk.PhotoImage(
                        overlay, master=self.canvas)
                    self.canvas.create_image(
                        ox + left, oy + top, image=self._highlight_photo,
                        anchor="nw", tags="hl")
                    used_alpha_overlay = True
            except (ImportError, RuntimeError, tk.TclError, ValueError):
                self._highlight_photo = None
        if not used_alpha_overlay:
            for x0, y0, x1, y1 in merge_pdf_highlight_rectangles(rects):
                self.canvas.create_rectangle(
                    ox + x0 * z, oy + y0 * z,
                    ox + x1 * z, oy + y1 * z,
                    fill="", outline=ACCENT_LIGHT,
                    width=max(1, int(round(z))), tags="hl")
        self.canvas.tag_raise("hl")
        if scroll:
            ytop = oy + min(r[1] for r in rects) * z
            vh = max(self.canvas.winfo_height(), 100)
            self.canvas.yview_moveto(
                max(0.0, (ytop - vh * 0.3) / self.total_h))
        self._render_visible()

    def text(self):
        return " ".join(c[0] for c in self.chunks)

    def close(self):
        try:
            self.doc.close()
        except Exception:
            pass
        self.destroy()


class WebArticleViewer(tk.Frame):
    """Clean webpage Reader View with sentence highlighting and navigation."""

    def __init__(self, master, article, close_cb, click_cb=None):
        super().__init__(master, bg=CARD)
        self.article = article
        self.click_cb = click_cb
        self.chunks = []
        self.repeat_groups = []
        self.focused_index = None
        self.body_font_size = 13

        header = tk.Frame(self, bg=CARD)
        header.pack(fill="x", pady=(0, 5))
        source_text = f"{article['site']}  •  Reader View"
        tk.Label(header, text=source_text, bg=CARD, fg=INK_MUTED,
                 font=(FONT_UI, 9, "bold"), anchor="w").pack(
                     side="left", fill="x", expand=True)
        tk.Button(
            header, text="Open original", command=self._open_original, bd=0,
            bg=CARD_SOFT, fg=ACCENT_DARK, activebackground=BTN_HOVER,
            activeforeground=ACCENT_DARK, cursor="hand2",
            highlightthickness=0, padx=10, pady=4,
            font=(FONT_UI, 9, "bold")).pack(side="right", padx=(6, 0))
        tk.Button(
            header, text="Close webpage", command=close_cb, bd=0,
            bg=ACCENT_SOFT, fg=ACCENT_DARK, activebackground=BTN_HOVER,
            activeforeground=ACCENT_DARK, cursor="hand2",
            highlightthickness=0, padx=10, pady=4,
            font=(FONT_UI, 9, "bold")).pack(side="right", padx=(6, 0))

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.reader = tk.Text(
            body, wrap="word", bd=0, relief="flat", highlightthickness=0,
            bg=CARD, fg=INK, selectbackground="#cfe0fb",
            selectforeground=INK, padx=22, pady=12, spacing1=4,
            spacing2=2, spacing3=8, cursor="hand2",
            yscrollcommand=scrollbar.set)
        self.reader.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.reader.yview)
        self.reader.bind("<Button-1>", self._on_click)

        self.body_font = tkfont.Font(
            self, family=FONT_UI, size=self.body_font_size)
        self.title_font = tkfont.Font(
            self, family=FONT_TITLE, size=23, weight="bold")
        self.heading_font = tkfont.Font(
            self, family=FONT_UI, size=17, weight="bold")
        self.byline_font = tkfont.Font(
            self, family=FONT_UI, size=10, slant="italic")
        self.quote_font = tkfont.Font(
            self, family=FONT_UI, size=self.body_font_size, slant="italic")
        self.pre_font = tkfont.Font(
            self, family="Consolas", size=max(9, self.body_font_size - 1))
        self._configure_tags()

    def _open_original(self):
        webbrowser.open(self.article["url"], new=2)

    def _configure_tags(self):
        self.reader.tag_configure(
            "title", font=self.title_font, foreground=INK, spacing1=3,
            spacing3=12)
        self.reader.tag_configure(
            "heading", font=self.heading_font, foreground=INK, spacing1=10,
            spacing3=6)
        self.reader.tag_configure(
            "byline", font=self.byline_font, foreground=INK_MUTED,
            spacing3=10)
        self.reader.tag_configure(
            "body", font=self.body_font, foreground=INK, lmargin1=0, lmargin2=0)
        self.reader.tag_configure(
            "list", font=self.body_font, foreground=INK, lmargin1=14,
            lmargin2=25)
        self.reader.tag_configure(
            "quote", font=self.quote_font, foreground=INK_SOFT,
            lmargin1=18, lmargin2=18)
        self.reader.tag_configure(
            "pre", font=self.pre_font, foreground=INK_SOFT,
            background=CARD_SOFT, lmargin1=14, lmargin2=14)
        self.reader.tag_configure(
            "caption", font=self.byline_font, foreground=INK_MUTED)
        self.reader.tag_configure(
            "speaking", background="#b9ccff", foreground=INK)

    def _display_blocks(self):
        blocks = [("title", self.article["title"])]
        if self.article.get("byline"):
            blocks.append(("byline", f"By {self.article['byline']}"))
        blocks.extend(self.article["blocks"])
        return blocks

    def build_chunks(self, max_len):
        previous_focus = self.focused_index
        self.reader.configure(state="normal")
        self.reader.delete("1.0", "end")
        self.chunks = []
        self.repeat_groups = []
        self.block_starts = {}
        block_prefix = 1 + bool(self.article.get("byline"))
        chunk_limit_reached = False
        sentence_id = 0
        for paragraph_id, (kind, block) in enumerate(self._display_blocks()):
            if len(self.chunks) >= WEB_CHUNK_LIMIT:
                chunk_limit_reached = True
                break
            if paragraph_id >= block_prefix:
                self.block_starts[paragraph_id - block_prefix] = self.reader.index("end-1c")
            style = kind if kind in {
                "title", "heading", "byline", "body", "list", "quote",
                "pre", "caption"} else "body"
            if kind == "list":
                self.reader.insert("end", "• ", style)
            sentences = split_repeat_sentences(block) or [block]
            piece_number = 0
            for sentence in sentences:
                pieces = split_chunks(sentence, max_len) or [sentence]
                for piece in pieces:
                    if len(self.chunks) >= WEB_CHUNK_LIMIT:
                        chunk_limit_reached = True
                        break
                    if piece_number:
                        self.reader.insert("end", " ", style)
                    start = self.reader.index("end-1c")
                    self.reader.insert("end", piece, style)
                    end = self.reader.index("end-1c")
                    self.chunks.append((piece, start, end))
                    self.repeat_groups.append({
                        "sentence_id": sentence_id,
                        "paragraph_id": paragraph_id,
                    })
                    piece_number += 1
                sentence_id += 1
                if chunk_limit_reached:
                    break
            self.reader.insert("end", "\n\n", style)
            if chunk_limit_reached:
                break
        if chunk_limit_reached:
            self.article["truncated"] = True
        if self.article.get("truncated"):
            self.reader.insert(
                "end", "Reader View was shortened for performance.\n",
                "caption")
        self.reader.configure(state="disabled")
        if previous_focus is not None and previous_focus < len(self.chunks):
            self.focus_chunk(previous_focus, scroll=False)
        return self.chunks

    def go_to_section(self, block_index):
        index = getattr(self, "block_starts", {}).get(block_index)
        if index is None:
            return False
        self.reader.see(index)
        return True

    def _on_click(self, event):
        if self.click_cb is None:
            return
        index = self.reader.index(f"@{event.x},{event.y}")
        for number, (_text, start, end) in enumerate(self.chunks):
            if (self.reader.compare(start, "<=", index) and
                    self.reader.compare(index, "<", end)):
                self.click_cb(number)
                return "break"
        return None

    def focus_chunk(self, idx, scroll=True):
        if not self.winfo_exists() or idx >= len(self.chunks):
            return
        self.focused_index = idx
        _text, start, end = self.chunks[idx]
        self.reader.configure(state="normal")
        self.reader.tag_remove("speaking", "1.0", "end")
        self.reader.tag_add("speaking", start, end)
        self.reader.tag_raise("speaking")
        self.reader.configure(state="disabled")
        if scroll:
            self.reader.see(start)

    def _set_font_sizes(self):
        ratio = self.body_font_size / 13.0
        self.body_font.configure(size=self.body_font_size)
        self.title_font.configure(size=max(17, round(23 * ratio)))
        self.heading_font.configure(size=max(13, round(17 * ratio)))
        self.byline_font.configure(size=max(8, round(10 * ratio)))
        self.quote_font.configure(size=self.body_font_size)
        self.pre_font.configure(size=max(8, self.body_font_size - 1))

    def zoom_by(self, direction):
        step = 1 if direction > 0 else -1
        self.body_font_size = max(9, min(26, self.body_font_size + step))
        self._set_font_sizes()
        return round(self.body_font_size / 13 * 100)

    def reset_zoom(self):
        self.body_font_size = 13
        self._set_font_sizes()
        return 100

    def text(self):
        return "\n\n".join(text for _, text in self._display_blocks())

    def close(self):
        self.destroy()


class LibraryView(tk.Frame):
    """Integrated browser for documents and text saved on this computer."""

    SORT_LABELS = {
        "Recently opened": "recent",
        "Title A-Z": "title",
    }

    def __init__(self, master, app):
        super().__init__(master, bg=CARD)
        self.app = app
        self.search_var = tk.StringVar()
        self.sort_var = tk.StringVar(value="Recently opened")
        self.count_var = tk.StringVar(value="0 items")
        self._search_after_id = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        heading = tk.Frame(self, bg=CARD)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        heading.grid_columnconfigure(0, weight=1)
        title_group = tk.Frame(heading, bg=CARD)
        title_group.grid(row=0, column=0, sticky="w")
        tk.Label(title_group, text="Library", bg=CARD, fg=INK,
                 font=(FONT_UI, 19, "bold")).pack(side="left")
        tk.Label(title_group, textvariable=self.count_var, bg=CARD,
                 fg=INK_MUTED, font=(FONT_UI, 10)).pack(
                     side="left", padx=(12, 0), pady=(6, 0))
        self._button(heading, "Back to reader", app._close_library).grid(
            row=0, column=1, sticky="e")

        filters = tk.Frame(self, bg=CARD_SOFT, highlightthickness=1,
                           highlightbackground=CARD_LINE)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        filters.grid_columnconfigure(1, weight=1)
        tk.Label(filters, text="Search", bg=CARD_SOFT, fg=INK_SOFT,
                 font=(FONT_UI, 10, "bold")).grid(
                     row=0, column=0, padx=(12, 8), pady=9)
        self.search_entry = tk.Entry(
            filters, textvariable=self.search_var, relief="flat", bd=0,
            bg=CARD, fg=INK, insertbackground=ACCENT,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, font=(FONT_UI, 10))
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=7)
        self.search_entry.bind("<KeyRelease>", self._schedule_refresh)
        tk.Label(filters, text="Sort", bg=CARD_SOFT, fg=INK_SOFT,
                 font=(FONT_UI, 10, "bold")).grid(
                     row=0, column=2, padx=(16, 7))
        self.sort_combo = ttk.Combobox(
            filters, textvariable=self.sort_var,
            values=list(self.SORT_LABELS), state="readonly", width=17,
            style="CompactVoice.TCombobox", font=(FONT_UI, 9))
        self.sort_combo.grid(row=0, column=3, padx=(0, 9), pady=7)
        self.sort_combo.bind("<<ComboboxSelected>>", self._refresh_event)

        table_host = tk.Frame(self, bg=CARD)
        table_host.grid(row=2, column=0, sticky="nsew")
        table_host.grid_columnconfigure(0, weight=1)
        table_host.grid_rowconfigure(0, weight=1)
        columns = ("title", "type", "opened", "details")
        self.tree = ttk.Treeview(
            table_host, columns=columns, show="headings",
            selectmode="browse", style="Library.Treeview")
        self.tree.heading("title", text="Title", anchor="w")
        self.tree.heading("type", text="Type", anchor="w")
        self.tree.heading("opened", text="Last opened", anchor="w")
        self.tree.heading("details", text="Details", anchor="e")
        self.tree.column("title", width=430, minwidth=180, stretch=True,
                         anchor="w")
        self.tree.column("type", width=95, minwidth=80, stretch=False,
                         anchor="w")
        self.tree.column("opened", width=185, minwidth=145, stretch=False,
                         anchor="w")
        self.tree.column("details", width=120, minwidth=95, stretch=False,
                         anchor="e")
        scrollbar = ttk.Scrollbar(
            table_host, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-Button-1>", self._open_event)
        self.tree.bind("<Return>", self._open_event)

        self.empty_label = tk.Label(
            table_host,
            text="Your Library is empty\n\nImport a file, or save the text "
                 "currently in the reader.",
            bg=CARD, fg=INK_MUTED, justify="center",
            font=(FONT_UI, 12))

        footer = tk.Frame(self, bg=CARD)
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self._button(
            footer, "Import file...", app._library_import,
            primary=True).pack(side="left")
        self._button(
            footer, "Save current text", app._save_current_text_to_library
        ).pack(side="left", padx=(9, 0))
        self.remove_button = self._button(
            footer, "Remove", app._remove_library_selection)
        self.remove_button.pack(side="right")
        self.open_button = self._button(
            footer, "Open selected", app._open_library_selection,
            primary=True)
        self.open_button.pack(side="right", padx=(0, 9))
        self._selection_changed()

    @staticmethod
    def _button(master, text, command, primary=False):
        background = ACCENT if primary else CARD_SOFT
        foreground = "#ffffff" if primary else INK
        return tk.Button(
            master, text=text, command=command, bg=background, fg=foreground,
            activebackground=ACCENT_DARK if primary else ACCENT_SOFT,
            activeforeground=foreground, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, cursor="hand2", takefocus=True,
            font=(FONT_UI, 10, "bold"), padx=15, pady=8)

    @staticmethod
    def _format_timestamp(value):
        try:
            return time.strftime("%b %d, %Y  %I:%M %p", time.localtime(value))
        except (OverflowError, OSError, TypeError, ValueError):
            return "Unknown"

    @staticmethod
    def _format_details(item):
        words = int(item.get("word_count") or 0)
        if words:
            return f"{words:,} words"
        size = int(item.get("size_bytes") or 0)
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _schedule_refresh(self, _event=None):
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except tk.TclError:
                pass
        self._search_after_id = self.after(140, self.refresh)

    def _refresh_event(self, _event=None):
        self.refresh()

    def refresh(self):
        self._search_after_id = None
        selected = self.selected_id()
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        items = self.app._list_library_items(
            self.search_var.get(),
            self.SORT_LABELS.get(self.sort_var.get(), "recent"))
        for item in items:
            kind = (item.get("extension") or "Text").lstrip(".").upper()
            self.tree.insert(
                "", "end", iid=item["id"], values=(
                    item["title"], kind,
                    self._format_timestamp(item.get("last_opened_at")),
                    self._format_details(item)))
        count = len(items)
        self.count_var.set(f"{count:,} item" + ("" if count == 1 else "s"))
        if count:
            self.empty_label.place_forget()
            if selected in self.tree.get_children():
                self.tree.selection_set(selected)
            else:
                first = self.tree.get_children()[0]
                self.tree.selection_set(first)
                self.tree.focus(first)
        else:
            message = ("No matching Library items"
                       if self.search_var.get().strip() else
                       "Your Library is empty\n\nImport a file, or save the "
                       "text currently in the reader.")
            self.empty_label.configure(text=message)
            self.empty_label.place(relx=0.5, rely=0.48, anchor="center")
        self._selection_changed()

    def selected_id(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _selection_changed(self, _event=None):
        state = "normal" if self.selected_id() else "disabled"
        self.open_button.configure(state=state)
        self.remove_button.configure(state=state)

    def _open_event(self, _event=None):
        if self.selected_id():
            self.app._open_library_selection()
        return "break"


# ---------------------------------------------------------------- app
class ReaderApp:
    def __init__(self, root):
        self.root = root
        self._shutdown_event = threading.Event()
        self._clipboard_after_id = None
        self._clipboard_events = queue.Queue()
        self._clipboard_pending_updates = deque()
        self._studio_clipboard_queue = deque()
        self._clipboard_sensitive_input = threading.Event()
        self._clipboard_sensitive_depth = 0
        self._clipboard_sensitive_sequence = None
        self._clipboard_sensitive_baseline = False
        self._clipboard_listener_ready = threading.Event()
        self._clipboard_listener_stop = threading.Event()
        self._clipboard_listener_hwnd = None
        self._clipboard_listener_thread_id = None
        self._clipboard_listener_failed = None
        self._clipboard_wndproc = None
        self._last_clip_sequence = None
        self._tray_after_id = None
        self._tray_hide_after_id = None
        self._tray_commands = queue.Queue()
        self._tray_ready = threading.Event()
        self._tray_icon = None
        self._tray_starting = False
        self._tray_failed = None
        self._pending_tray_hide = False
        self._tray_notice_shown = False
        self._window_state_before_hide = "normal"
        self._fullscreen_before_hide = False
        self._browser_after_id = None
        self._browser_commands = queue.Queue(maxsize=16)
        self._browser_bridge = None
        self._browser_active_session = None
        self._browser_request_cancel = None
        self._instance_after_id = None
        self._exiting = False
        root.title("Piper Reader - Free Natural TTS")
        # Compact surrounding controls give the reading surface about 50%
        # more visible area, primarily as useful document height.
        display_dpi = get_window_dpi(root)
        place_window_in_workarea(
            root, scaled_ui_pixels(1160, display_dpi),
            scaled_ui_pixels(870, display_dpi))
        root.minsize(scaled_ui_pixels(900, display_dpi),
                     scaled_ui_pixels(650, display_dpi))
        root.configure(bg=APP_BG)

        self.engines = {"piper": PiperEngine(self.set_status),
                        "kokoro": KokoroEngine(self.set_status),
                        "edge": EdgeEngine(self.set_status, pip_install),
                        "sapi": SapiEngine(self.set_status)}
        self.player = WavPlayer()
        self.speak_q = queue.Queue()
        self._playback_lock = threading.RLock()
        self._active_cancel = None
        self._active_pause = None
        self._pause_requested = threading.Event()
        self._speech_requests = set()
        self._clipboard_enqueued_at = {}
        self._clipboard_online_retry_after = 0.0
        self._active_request_phase = "preparing"
        self._active_request_message = ""
        self._document_read_cancel = None
        self._document_read_viewer = None
        self._contents_browsing_viewer = None
        self._speech_diagnostics = SpeechDiagnostics(
            os.path.join(DATA_DIR, "speech_diagnostics.jsonl"))
        self._save_wav_jobs = 0
        self._online_export_cancels = set()
        self.playback_state = "idle"
        self._last_speaking_status = "Speaking..."
        self.text_font_size = 13
        self.tmpdir = tempfile.mkdtemp(prefix="piper_reader_")
        self._last_clip = None
        self._asked_install = set()
        self._clipboard_warm_lock = threading.Lock()
        self._clipboard_warming = set()
        self._clipboard_warmed = set()
        self._clipboard_warm_after_id = None
        self._runtime_probe_last = time.monotonic()
        self._runtime_user_was_idle = False
        self._runtime_reprime_pending = False
        self._runtime_reprime_inflight = False
        self._runtime_reprime_last = 0.0
        self._runtime_reprime_not_before = 0.0
        self._speed_panel = None
        self._speed_panel_buttons = {}
        self._speed_panel_scale_var = None
        self.viewer = None
        self.library_view = None
        self.ai_studio = None
        self.textbook_studio = None
        self._studio_audio_owner = None
        self.current_library_item_id = None
        self.current_text_name = None
        self.library_store = None
        self.library_error = None
        self._content_load_serial = 0
        self._web_load_lock = threading.Lock()
        # PyMuPDF is deliberately serialized: its OCR/rendering internals are
        # not supported from concurrent threads in one process.
        self._ocr_load_lock = threading.Lock()

        try:
            self.library_store = LibraryStore(
                LIBRARY_DIR, open_binary=_open_shared_binary)
        except Exception as exc:
            # Never discard or recreate a Library that this version cannot
            # read. The reader remains usable and reports the exact problem
            # when the user opens Library.
            self.library_error = str(exc)

        self._load_settings()
        saved_ocr_mode = normalize_ocr_mode(
            self.settings.get("ocr_language", "auto"))
        self.ocr_language_var = tk.StringVar(
            value=OCR_MODE_LABELS[saved_ocr_mode])
        self._build_ui()
        self._tray_autoread = bool(self.auto_var.get())
        self.auto_var.trace_add("write", self._sync_tray_autoread)

        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(300, self._check_engine)
        self._prime_clipboard()
        self._start_clipboard_listener()
        self._poll_clipboard()
        self._poll_tray_commands()
        self._start_tray_support()
        self._start_browser_bridge()
        self._poll_browser_commands()
        self._poll_instance_commands()
        self._clipboard_warm_after_id = self.root.after(
            450, self._warm_clipboard_voice_async)
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        root.bind("<F11>", self.toggle_fullscreen)
        root.bind("<Escape>", self._handle_escape)
        root.bind("<Control-q>", self._exit_app)
        root.bind("<Control-plus>", self.zoom_in)
        root.bind("<Control-equal>", self.zoom_in)
        root.bind("<Control-KP_Add>", self.zoom_in)
        root.bind("<Control-minus>", self.zoom_out)
        root.bind("<Control-KP_Subtract>", self.zoom_out)
        root.bind("<Control-Key-0>", self.reset_zoom)
        root.bind("<Control-l>", self.toggle_library)
        root.resizable(True, True)

    # ---------------- engine bootstrap
    def _voice_snapshot(self, label):
        engine_key, voice_id, lang = VOICES.get(label, VOICES[DEFAULT_VOICE])
        return engine_key, voice_id, lang

    def current_engine(self):
        engine_key, voice_id, lang = self._voice_snapshot(
            self.voice_var.get())
        return self.engines[engine_key], voice_id, lang

    def _on_clipboard_voice_selected(self, *_):
        self._save_settings()
        self._update_voice_notice()
        if self._voice_snapshot(self.clipboard_voice_var.get())[0] == "edge":
            self.set_status("Clipboard " + ONLINE_NOTICE)
        self._cancel_after("_clipboard_warm_after_id")
        self._clipboard_warm_after_id = self.root.after(
            250, self._warm_clipboard_voice_async)

    def _warm_clipboard_voice_async(self):
        self._clipboard_warm_after_id = None
        if self._shutdown_event.is_set():
            return
        labels = dict.fromkeys((
            self.voice_var.get(), self.clipboard_voice_var.get()))
        for label in labels:
            self._warm_voice_label_async(label)

    def _synthesis_busy_locked(self):
        """Return whether speech or a WAV export owns a synthesis engine."""
        return bool(self._speech_requests or
                    getattr(self, "_save_wav_jobs", 0))

    def _warm_voice_label_async(self, label, force=False, completion=None):
        engine_key, voice_id, lang = self._voice_snapshot(label)
        if engine_key == "piper":
            model_id, _speaker_id = PiperEngine._voice_details(voice_id)
            required = (os.path.join(VOICES_DIR, f"{model_id}.onnx"),)
        elif engine_key == "kokoro":
            required = tuple(
                os.path.join(VOICES_DIR, filename)
                for filename in KOKORO_FILES)
        else:
            return False
        if any(not os.path.exists(path) for path in required):
            return False
        with self._clipboard_warm_lock:
            if (label in self._clipboard_warming or
                    (not force and label in self._clipboard_warmed)):
                return False
            self._clipboard_warming.add(label)

        def job():
            warmed = False
            blocked = False
            try:
                with self._playback_lock:
                    blocked = self._synthesis_busy_locked()
                if not blocked and not self._shutdown_event.is_set():
                    engine = self.engines[engine_key]
                    if engine.ready():
                        if engine_key == "kokoro":
                            warmed = engine.warm(
                                voice_id, lang, force=force)
                        else:
                            warmed = engine.warm(voice_id, lang)
            except Exception:
                pass
            finally:
                with self._clipboard_warm_lock:
                    self._clipboard_warming.discard(label)
                    if warmed:
                        self._clipboard_warmed.add(label)
                if warmed and engine_key == "kokoro":
                    self._runtime_reprime_last = time.monotonic()
                if completion is not None:
                    try:
                        self.root.after(
                            0, completion, bool(warmed), bool(blocked))
                    except Exception:
                        pass

        try:
            threading.Thread(target=job, daemon=True).start()
        except Exception:
            with self._clipboard_warm_lock:
                self._clipboard_warming.discard(label)
            return False
        return True

    @staticmethod
    def _seconds_since_user_input():
        """Best-effort Windows user-idle duration; None on other systems."""
        if os.name != "nt":
            return None

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = (("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD))

        try:
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            # Both values are DWORD ticks, so subtraction remains correct
            # across the roughly 49-day GetTickCount wrap.
            now = ctypes.windll.kernel32.GetTickCount()
            return ((int(now) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0
        except Exception:
            return None

    def _request_runtime_reprime(self, now=None):
        """Request one silent Kokoro refresh after resume/user return."""
        if self._shutdown_event.is_set():
            return
        now = time.monotonic() if now is None else float(now)
        if (self._runtime_reprime_last and
                now - self._runtime_reprime_last <
                RUNTIME_REPRIME_COOLDOWN_SECONDS):
            return
        self._runtime_reprime_pending = True
        # Returning users commonly copy something within the next few seconds.
        # Give that real speech an uncontested window before low-priority
        # maintenance is allowed to claim Kokoro's non-cancellable inference.
        self._runtime_reprime_not_before = max(
            getattr(self, "_runtime_reprime_not_before", 0.0),
            now + RUNTIME_REPRIME_GRACE_SECONDS)

    def _try_runtime_reprime(self, now=None, user_idle_seconds=None):
        """Launch a pending refresh only while all synthesis paths are idle."""
        if (self._shutdown_event.is_set() or
                not self._runtime_reprime_pending or
                self._runtime_reprime_inflight):
            return
        now = time.monotonic() if now is None else float(now)
        if now < getattr(self, "_runtime_reprime_not_before", 0.0):
            return
        idle = (self._seconds_since_user_input()
                if user_idle_seconds is None else float(user_idle_seconds))
        if idle is not None and idle < RUNTIME_REPRIME_QUIET_SECONDS:
            return
        with self._playback_lock:
            if self._synthesis_busy_locked():
                return
        labels = dict.fromkeys((
            self.clipboard_voice_var.get(), self.voice_var.get()))
        label = next((candidate for candidate in labels
                      if self._voice_snapshot(candidate)[0] == "kokoro"),
                     None)
        if label is None:
            self._runtime_reprime_pending = False
            return
        self._runtime_reprime_inflight = True
        launched = self._warm_voice_label_async(
            label, force=True, completion=self._finish_runtime_reprime)
        if launched:
            self._runtime_reprime_pending = False
            self._runtime_reprime_not_before = 0.0
            return
        self._runtime_reprime_inflight = False
        # A startup warm may already own this label. Keep the request pending
        # only in that transient case; missing model files should not spin.
        with self._clipboard_warm_lock:
            already_warming = label in self._clipboard_warming
        self._runtime_reprime_pending = already_warming

    def _finish_runtime_reprime(self, warmed, blocked):
        self._runtime_reprime_inflight = False
        if warmed:
            self._runtime_reprime_last = time.monotonic()
            self._runtime_reprime_not_before = 0.0
        elif blocked and not self._shutdown_event.is_set():
            # Speech or Save WAV won the small launch race. Their completion
            # paths will retry this pending refresh when the engine is idle.
            self._runtime_reprime_pending = True

    def _probe_runtime_reprime(self, now=None, user_idle_seconds=None):
        """Detect resume or return-from-idle without periodic inference."""
        now = time.monotonic() if now is None else float(now)
        elapsed = now - self._runtime_probe_last
        if elapsed < RUNTIME_PROBE_INTERVAL_SECONDS:
            return
        self._runtime_probe_last = now
        resumed = elapsed >= RUNTIME_RESUME_GAP_SECONDS
        idle = (self._seconds_since_user_input()
                if user_idle_seconds is None else float(user_idle_seconds))
        returned = False
        if idle is not None:
            if idle >= RUNTIME_USER_IDLE_SECONDS:
                self._runtime_user_was_idle = True
            elif (self._runtime_user_was_idle and
                  idle <= RUNTIME_USER_ACTIVE_SECONDS):
                self._runtime_user_was_idle = False
                returned = True
        if resumed or returned:
            self._request_runtime_reprime(now)
        elif self._runtime_reprime_pending:
            self._try_runtime_reprime(
                now=now, user_idle_seconds=idle)

    def _update_voice_notice(self):
        if not hasattr(self, "voice_privacy_var"):
            return
        online = any(self._voice_snapshot(value.get())[0] == "edge"
                     for value in (self.voice_var, self.clipboard_voice_var))
        self.voice_privacy_var.set(
            "Microsoft online \u2022 text sent to Microsoft" if online else
            "free \u2022 local voices available")

    def _check_engine(self, *_):
        self._save_settings()
        self._update_voice_notice()
        self._cancel_after("_clipboard_warm_after_id")
        self._clipboard_warm_after_id = self.root.after(
            250, self._warm_clipboard_voice_async)
        engine, _, _ = self.current_engine()
        if isinstance(engine, EdgeEngine):
            self.set_status(ONLINE_NOTICE)
        if engine.ready() or engine.name in self._asked_install:
            return
        self._asked_install.add(engine.name)
        size = {"Kokoro": "~30 MB", "Windows": "~10 MB",
                "Microsoft online": "~10 MB"}.get(engine.name, "~14 MB")
        if messagebox.askyesno(
                "First-time setup",
                f"The free {engine.name} voice engine is not installed yet.\n\n"
                f"Install it now? ({size}, one time)"):
            def job():
                if engine.install():
                    self.set_status("Ready. Copy some text to hear it!")
                else:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Install failed",
                        f"Could not install {engine.package} automatically.\n"
                        f"Open Command Prompt and run:\n\n"
                        f'    py -3 -m pip install "{engine.package}"'))
            threading.Thread(target=job, daemon=True).start()

    # ---------------- UI
    def _build_ui(self):
        self.frost = APP_BG
        self.root.configure(bg=APP_BG)
        self._configure_styles()

        self.shell = tk.Frame(self.root, bg=APP_BG)
        self.shell.pack(fill="both", expand=True, padx=30, pady=(4, 4))
        self.shell.grid_columnconfigure(0, weight=1)
        self.shell.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_controls_card()
        self._build_editor_card()
        self._build_action_bar()
        self._build_status_bar()

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Voice.TCombobox", fieldbackground=CARD, background=CARD,
            foreground=INK, arrowcolor=INK_SOFT, borderwidth=0,
            bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
            selectbackground=CARD, selectforeground=INK, padding=(8, 9))
        style.map(
            "Voice.TCombobox",
            fieldbackground=[("readonly", CARD), ("focus", CARD)],
            foreground=[("readonly", INK)],
            bordercolor=[("focus", CARD)])
        style.configure(
            "CompactVoice.TCombobox", fieldbackground=CARD,
            background=CARD, foreground=INK, arrowcolor=INK_SOFT,
            borderwidth=0, bordercolor=CARD, lightcolor=CARD,
            darkcolor=CARD, selectbackground=CARD, selectforeground=INK,
            padding=(5, 3))
        style.map(
            "CompactVoice.TCombobox",
            fieldbackground=[("readonly", CARD), ("focus", CARD)],
            foreground=[("readonly", INK)],
            bordercolor=[("focus", CARD)])
        style.configure(
            "Repeat.TSpinbox", fieldbackground=CARD, background=CARD,
            foreground=INK, arrowcolor=INK_SOFT, borderwidth=1,
            bordercolor=CARD_LINE, lightcolor=CARD, darkcolor=CARD,
            selectbackground=ACCENT_SOFT, selectforeground=INK,
            padding=(4, 3))
        style.map(
            "Repeat.TSpinbox",
            fieldbackground=[("disabled", CARD_SOFT), ("focus", CARD)],
            foreground=[("disabled", INK_MUTED)],
            bordercolor=[("focus", ACCENT_LIGHT)])
        style.configure(
            "Speed.Horizontal.TScale", background=CARD,
            troughcolor=TRACK, bordercolor=CARD, lightcolor=ACCENT_LIGHT,
            darkcolor=ACCENT, sliderlength=24)
        style.configure(
            "Library.Treeview", background=CARD, fieldbackground=CARD,
            foreground=INK, rowheight=34, borderwidth=0,
            font=(FONT_UI, 10))
        style.map(
            "Library.Treeview", background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", INK)])
        style.configure(
            "Library.Treeview.Heading", background=CARD_SOFT,
            foreground=INK_SOFT, relief="flat", borderwidth=0,
            font=(FONT_UI, 9, "bold"), padding=(8, 8))
        style.map(
            "Library.Treeview.Heading",
            background=[("active", ACCENT_SOFT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", INK)
        self.root.option_add("*TCombobox*Listbox.font", (FONT_UI, 10))

    def _build_header(self):
        head = tk.Frame(self.shell, bg=APP_BG, height=58)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        head.grid_columnconfigure(1, weight=1)
        head.grid_propagate(False)

        LogoBadge(head, APP_BG).grid(row=0, column=0, sticky="w",
                                     padx=(2, 16))
        tk.Label(head, text="Piper Reader", bg=APP_BG, fg=INK,
                 font=(FONT_TITLE, 27, "bold")).grid(row=0, column=1,
                                                      sticky="w")
        self.ai_studio_button = tk.Button(
            head, text="AI Studio", command=self.open_ai_studio,
            bg=CARD, fg=INK, activebackground=ACCENT_SOFT,
            activeforeground=INK, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, cursor="hand2", takefocus=True,
            font=(FONT_UI, 10, "bold"), padx=14, pady=7)
        self.ai_studio_button.grid(row=0, column=2, sticky="e", padx=(8, 6))
        self.textbook_studio_button = tk.Button(
            head, text="Textbook", command=self.open_textbook_studio,
            bg=CARD, fg=INK, activebackground=ACCENT_SOFT,
            activeforeground=INK, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, cursor="hand2", takefocus=True,
            font=(FONT_UI, 10, "bold"), padx=14, pady=7)
        self.textbook_studio_button.grid(row=0, column=3, sticky="e", padx=(6, 6))
        self.library_button = tk.Button(
            head, text="Library", command=self.toggle_library,
            bg=CARD, fg=INK, activebackground=ACCENT_SOFT,
            activeforeground=INK, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, cursor="hand2", takefocus=True,
            font=(FONT_UI, 10, "bold"), padx=14, pady=7)
        self.library_button.grid(row=0, column=4, sticky="e", padx=(6, 6))
        self.voice_privacy_var = tk.StringVar(
            value="free \u2022 local voices available")
        tk.Label(head, textvariable=self.voice_privacy_var,
                 bg=APP_BG, fg=INK_SOFT,
                 font=(FONT_UI, 11)).grid(row=0, column=5, sticky="e",
                                          padx=(20, 8))

    @staticmethod
    def _make_speed_button(master, command, text=None, textvariable=None,
                           width=4, font_size=14, primary=False):
        """Create an accessible, keyboard-friendly speed control button."""
        background = ACCENT if primary else CARD_SOFT
        foreground = "#ffffff" if primary else INK
        active_background = ACCENT_DARK if primary else ACCENT_SOFT
        return tk.Button(
            master, text=text, textvariable=textvariable, command=command,
            width=width, bg=background, fg=foreground,
            activebackground=active_background,
            activeforeground=foreground, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=CARD_LINE,
            highlightcolor=ACCENT_LIGHT, cursor="hand2", takefocus=True,
            font=(FONT_UI, font_size, "bold"), padx=4, pady=4)

    def _build_controls_card(self):
        self.controls_card = GlassCard(
            self.shell, APP_BG, height=212, radius=20, pad=14)
        self.controls_card.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        card = self.controls_card.inner
        card.grid_columnconfigure(0, minsize=86)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(2, minsize=225)

        label_font = (FONT_UI, 12)
        tk.Label(card, text="Voice", bg=CARD, fg=INK,
                 font=label_font).grid(row=0, column=0, sticky="w",
                                       padx=(4, 12))

        self.voice_var = tk.StringVar(
            value=self.settings.get("voice", DEFAULT_VOICE))
        if self.voice_var.get() not in VOICES:
            self.voice_var.set(DEFAULT_VOICE)

        voice_surface = GlassCard(card, CARD, height=60, radius=14, pad=4,
                                  shadow=True)
        voice_surface.grid(row=0, column=1, sticky="ew")
        voice_icon = tk.Canvas(voice_surface.inner, width=38, height=40,
                               bg=CARD, highlightthickness=0, bd=0)
        voice_icon.pack(side="left", padx=(6, 2))
        draw_icon(voice_icon, "wave", 19, 20, ACCENT_LIGHT, 0.85)
        self.voice_combo = ttk.Combobox(
            voice_surface.inner, textvariable=self.voice_var,
            values=list(VOICES), state="readonly", style="Voice.TCombobox",
            font=(FONT_UI, 11))
        self.voice_combo.pack(side="left", fill="both", expand=True,
                              padx=(0, 5))
        self.voice_combo.bind("<<ComboboxSelected>>", self._check_engine)

        self.preview_button = SurfaceButton(
            card, "Preview voice", self.preview_voice, CARD, icon="play",
            width=220, height=60)
        self.preview_button.grid(row=0, column=2, sticky="ew", padx=(18, 0))

        speed_label = tk.Frame(card, bg=CARD)
        speed_label.grid(row=1, column=0, sticky="w", padx=(4, 12),
                         pady=(14, 0))
        tk.Label(speed_label, text="Speed", bg=CARD, fg=INK,
                 font=label_font).pack(anchor="w")
        tk.Button(
            speed_label, text="Presets", command=self._show_speed_panel,
            bg=CARD, fg=ACCENT_DARK, activebackground=CARD,
            activeforeground=ACCENT_PRESSED, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2", takefocus=True,
            font=(FONT_UI, 8, "underline"), padx=0, pady=0).pack(anchor="w")

        self.speed_var = tk.DoubleVar(
            value=normalize_speed(self.settings.get("speed", 1.0)))
        self.speed_readout_var = tk.StringVar()
        self._sync_speed_readout()
        self.vol_var = tk.DoubleVar(value=self.settings.get("volume", 1.0))
        audio = tk.Frame(card, bg=CARD)
        audio.grid(row=1, column=1, sticky="nsew", pady=(10, 0))
        audio.grid_columnconfigure(0, weight=1)
        audio.grid_columnconfigure(3, weight=1)

        speed_control = tk.Frame(audio, bg=CARD, height=70)
        speed_control.grid(row=0, column=0, sticky="ew")
        speed_control.grid_columnconfigure(1, weight=1)
        self.speed_minus_button = self._make_speed_button(
            speed_control, lambda: self._adjust_speed(-1), text="\u2212",
            width=2, font_size=16)
        self.speed_minus_button.grid(row=0, column=0, sticky="ns",
                                     padx=(0, 7), pady=10)
        self.speed_value_button = self._make_speed_button(
            speed_control, self._show_speed_panel,
            textvariable=self.speed_readout_var, width=12, font_size=10)
        self.speed_value_button.grid(row=0, column=1, sticky="nsew", pady=6)
        self.speed_plus_button = self._make_speed_button(
            speed_control, lambda: self._adjust_speed(1), text="+",
            width=2, font_size=16)
        self.speed_plus_button.grid(row=0, column=2, sticky="ns",
                                    padx=(7, 0), pady=10)
        tk.Frame(audio, bg=CARD_LINE, width=1, height=48).grid(
            row=0, column=1, padx=16)
        tk.Label(audio, text="Volume", bg=CARD, fg=INK,
                 font=label_font).grid(row=0, column=2, padx=(0, 8))
        self.volume_slider = PremiumSlider(
            audio, self.vol_var, 0.1, 1.0, lambda v: f"{round(v * 100):d}%",
            start_icon="speaker", end_icon="speaker_loud")
        self.volume_slider.grid(row=0, column=3, sticky="ew")

        self.auto_var = tk.BooleanVar(
            value=self.settings.get("autoread", True))
        self.clipboard_backup_var = tk.BooleanVar(
            value=self.settings.get("clipboard_offline_backup", True))
        self._clipboard_offline_backup_enabled = self.clipboard_backup_var.get()
        self.clipboard_voice_var = tk.StringVar(
            value=self.settings.get(
                "clipboard_voice", DEFAULT_CLIPBOARD_VOICE))
        if self.clipboard_voice_var.get() not in VOICES:
            self.clipboard_voice_var.set(DEFAULT_CLIPBOARD_VOICE)

        self.clipboard_voice_panel = GlassCard(
            card, CARD, width=220, height=100, radius=14, pad=4,
            fill=CARD_SOFT, shadow=True)
        self.clipboard_voice_panel.grid(
            row=1, column=2, sticky="ew", padx=(18, 0), pady=(10, 0))
        clipboard_inner = self.clipboard_voice_panel.inner
        clipboard_top = tk.Frame(clipboard_inner, bg=CARD_SOFT)
        clipboard_top.pack(side="top", fill="x", pady=(0, 1))
        tk.Label(
            clipboard_top, text="Clipboard voice", bg=CARD_SOFT, fg=INK,
            font=(FONT_UI, 9, "bold")).pack(side="left")
        self.auto_check = tk.Checkbutton(
            clipboard_top, text="Auto-read", variable=self.auto_var,
            bg=CARD_SOFT, activebackground=CARD_SOFT,
            fg=INK_SOFT, activeforeground=INK, selectcolor=CARD,
            font=(FONT_UI, 8), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor="hand2")
        self.auto_check.pack(side="right")
        self.clipboard_voice_combo = ttk.Combobox(
            clipboard_inner, textvariable=self.clipboard_voice_var,
            values=list(VOICES), state="readonly",
            style="CompactVoice.TCombobox", font=(FONT_UI, 9))
        self.clipboard_voice_combo.pack(
            side="top", fill="x", expand=True, pady=(1, 0))
        self.clipboard_voice_combo.bind(
            "<<ComboboxSelected>>", self._on_clipboard_voice_selected)
        tk.Checkbutton(
            clipboard_inner, text="Offline backup", variable=self.clipboard_backup_var,
            command=self._save_settings, bg=CARD_SOFT,
            activebackground=CARD_SOFT, fg=INK_SOFT, activeforeground=INK,
            selectcolor=CARD, font=(FONT_UI, 8), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor="hand2").pack(side="top", anchor="w")

    def _build_editor_card(self):
        self.card2 = GlassCard(self.shell, APP_BG, radius=20, pad=14)
        self.card2.grid(row=2, column=0, sticky="nsew", pady=(0, 5))

        toolbar = tk.Frame(self.card2.inner, bg=CARD)
        toolbar.pack(side="top", fill="x", pady=(0, 5))
        self.contents_button = tk.Button(
            toolbar, text="Table of Contents", command=self.toggle_contents,
            bg=CARD_SOFT, fg=ACCENT_DARK, activebackground=BTN_HOVER,
            activeforeground=ACCENT_DARK, bd=0, highlightthickness=0,
            padx=10, pady=4, font=(FONT_UI, 9, "bold"), cursor="hand2")
        self.contents_button.pack(side="left")
        tk.Button(
            toolbar, text="About & licenses", command=self.show_about,
            bg=CARD, fg=INK_SOFT, activebackground=BTN_HOVER,
            bd=0, highlightthickness=0, padx=10, pady=4,
            font=(FONT_UI, 9), cursor="hand2").pack(side="right")
        self.reading_area = tk.Frame(self.card2.inner, bg=CARD)
        self.reading_area.pack(side="top", fill="both", expand=True)
        self.reading_area.grid_columnconfigure(1, weight=1)
        self.reading_area.grid_rowconfigure(0, weight=1)
        self._build_contents_panel(self.reading_area)
        self.editor_host = tk.Frame(self.reading_area, bg=CARD)
        self.editor_host.grid(row=0, column=1, sticky="nsew")
        self.editor_host.grid_columnconfigure(0, weight=1)
        self.editor_host.grid_rowconfigure(0, weight=1)

        self.editor_view = tk.Frame(self.editor_host, bg=CARD)
        self.editor_view.grid(row=0, column=0, sticky="nsew")
        self.editor_view.grid_columnconfigure(1, weight=1)
        self.editor_view.grid_rowconfigure(0, weight=1)

        badge = tk.Canvas(self.editor_view, width=52, height=52, bg=CARD,
                          highlightthickness=0, bd=0)
        badge.grid(row=0, column=0, sticky="nw", padx=(0, 12), pady=(2, 0))
        badge.create_oval(3, 3, 49, 49, fill=ACCENT_SOFT, outline="")
        draw_icon(badge, "sparkle", 26, 26, ACCENT, 0.8)

        self.text_font = tkfont.Font(
            family=FONT_UI, size=self.text_font_size)
        self.text = tk.Text(
            self.editor_view, wrap="word", bd=0, relief="flat",
            highlightthickness=0, bg=CARD, fg=INK,
            insertbackground=ACCENT, selectbackground="#cfe0fb",
            selectforeground=INK, font=self.text_font, padx=4, pady=5,
            spacing1=6, spacing2=2, spacing3=10, undo=True, maxundo=100)
        self.text.grid(row=0, column=1, sticky="nsew")
        self.text.bind("<Double-Button-1>", self._text_click_read)
        self.text.insert(
            "1.0", "Paste or type text here, open a PDF, Word document, "
            "scanned page, or photo with 'Open / OCR...', open an article "
            "with 'Open webpage...', or "
            "just copy text anywhere in Windows and it will be read out "
            "loud.\n\nScanned PDFs and images are recognized locally. Documents "
            "and webpages open with read-along highlighting. "
            "Click any sentence to start reading from there; double-click in "
            "this box to do the same.\n\nMicrosoft online voices send "
            "text to Microsoft when reading. Piper, Kokoro, and Windows "
            "voices read locally after setup.")
        self._welcome_text = self.text.get("1.0", "end").strip()
        self.text.edit_modified(False)
        self.text.bind("<<Modified>>", self._contents_text_changed, add="+")

    def show_about(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("About PiperReader")
        dialog.transient(self.root)
        dialog.configure(bg=CARD)
        dpi = get_window_dpi(self.root)
        place_window_in_workarea(dialog, scaled_ui_pixels(720, dpi),
                                 scaled_ui_pixels(520, dpi))
        dialog.minsize(scaled_ui_pixels(520, dpi), scaled_ui_pixels(360, dpi))
        tk.Label(dialog, text=f"PiperReader {APP_VERSION}", bg=CARD, fg=INK,
                 font=(FONT_UI, 17, "bold")).pack(anchor="w", padx=18, pady=(16, 8))
        tk.Label(
            dialog, text="Copyright (C) 2026 Shettima123\n"
            "Free software under AGPL-3.0-only. You may share and modify it "
            "under the license terms.\nProvided without warranty.",
            bg=CARD, fg=INK, justify="left", wraplength=scaled_ui_pixels(480, dpi),
            font=(FONT_UI, 10)).pack(anchor="w", padx=18, pady=(0, 12))
        # Reserve the actions before the expanding license text claims space.
        footer = tk.Frame(dialog, bg=CARD)
        footer.pack(side="bottom", fill="x", padx=18, pady=(0, 14))
        ttk.Button(footer, text="Source & downloads", command=lambda: webbrowser.open(
            f"https://github.com/Shettima123/PiperReader/releases/tag/v{APP_VERSION}"),
            ).pack(side="left")
        ttk.Button(footer, text="Close", command=dialog.destroy).pack(side="right")
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        for title, filename in (("License", "LICENSE"),
                                ("Third-party notices", "THIRD_PARTY_NOTICES.md"),
                                ("Source code", "SOURCE_CODE.md")):
            panel = tk.Frame(notebook, bg=CARD)
            notebook.add(panel, text=title)
            scroll = ttk.Scrollbar(panel)
            scroll.pack(side="right", fill="y")
            text = tk.Text(panel, wrap="word", bg=CARD, fg=INK,
                           font=(FONT_UI, 10), bd=0, padx=10, pady=10,
                           yscrollcommand=scroll.set)
            text.pack(fill="both", expand=True)
            scroll.configure(command=text.yview)
            try:
                with open(os.path.join(APP_DIR, filename), encoding="utf-8") as stream:
                    content = stream.read()
            except OSError:
                content = ("See the project source and license at:\n"
                           "https://github.com/Shettima123/PiperReader")
            text.insert("1.0", content)
            text.configure(state="disabled")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _build_contents_panel(self, parent):
        self._contents_open = False
        self._contents_entries = {}
        self._contents_viewer = None
        self._contents_refresh_after = None
        self.contents_panel = tk.Frame(parent, bg=CARD_SOFT, width=270)
        self.contents_panel.grid_propagate(False)
        self.contents_panel.grid_columnconfigure(0, weight=1)
        self.contents_panel.grid_rowconfigure(2, weight=1)
        heading = tk.Frame(self.contents_panel, bg=CARD_SOFT)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(7, 3))
        tk.Label(heading, text="Contents", bg=CARD_SOFT, fg=INK,
                 font=(FONT_UI, 10, "bold")).pack(side="left")
        tk.Button(heading, text="Hide", command=self._hide_contents,
                  bg=CARD_SOFT, fg=ACCENT_DARK, bd=0, padx=5, pady=2,
                  font=(FONT_UI, 9), cursor="hand2").pack(side="right")
        self.contents_hint = tk.StringVar(value="")
        tk.Label(self.contents_panel, textvariable=self.contents_hint,
                 bg=CARD_SOFT, fg=INK_SOFT, font=(FONT_UI, 9),
                 wraplength=245, justify="left", anchor="w").grid(
                     row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        style = ttk.Style(self.root)
        style.configure("Contents.Treeview", background=CARD, fieldbackground=CARD,
                        foreground=INK, font=(FONT_UI, 9), rowheight=26)
        self.contents_tree = ttk.Treeview(
            self.contents_panel, columns=("page",), show="tree headings",
            selectmode="browse", style="Contents.Treeview")
        self.contents_tree.heading("#0", text="Section", anchor="w")
        self.contents_tree.heading("page", text="Page", anchor="e")
        self.contents_tree.column("#0", width=195, minwidth=100, stretch=True)
        self.contents_tree.column("page", width=45, minwidth=40, stretch=False, anchor="e")
        self.contents_tree.grid(row=2, column=0, sticky="nsew", padx=(6, 0))
        scroll = ttk.Scrollbar(self.contents_panel, orient="vertical", command=self.contents_tree.yview)
        scroll.grid(row=2, column=1, sticky="ns", padx=(0, 4))
        self.contents_tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(self.contents_panel, orient="horizontal", command=self.contents_tree.xview)
        horizontal.grid(row=3, column=0, sticky="ew", padx=6)
        self.contents_tree.configure(xscrollcommand=horizontal.set)
        self.contents_tree.bind("<<TreeviewSelect>>", self._contents_selected)
        self.contents_tree.bind("<Return>", self._contents_selected)
        self.contents_tree.bind("<Escape>", lambda _event: self._hide_contents())
        self.contents_instruction = tk.StringVar(
            value="Select a section to jump. Click document text to read from there.")
        tk.Label(self.contents_panel, textvariable=self.contents_instruction,
                 bg=CARD_SOFT, fg=INK_SOFT, font=(FONT_UI, 9), wraplength=245,
                 justify="left").grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=8)

    def toggle_contents(self):
        if getattr(self, "_contents_open", False):
            self._hide_contents()
            return
        self._close_library()
        self._contents_open = True
        self.contents_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.contents_button.configure(bg=ACCENT_SOFT)
        self._refresh_contents()
        self.contents_tree.focus_set()

    def _hide_contents(self):
        self._contents_open = False
        self._contents_entries = {}
        self._contents_viewer = None
        panel = getattr(self, "contents_panel", None)
        if panel is not None:
            panel.grid_remove()
            self.contents_button.configure(bg=CARD_SOFT)

    def _refresh_contents(self):
        self._contents_refresh_after = None
        if not getattr(self, "_contents_open", False):
            return
        self._contents_entries = {}
        self.contents_tree.delete(*self.contents_tree.get_children())
        self._contents_viewer = self.viewer
        entries = []
        try:
            if isinstance(self.viewer, PdfViewer):
                if not hasattr(self.viewer, "_outline_cache"):
                    self.viewer._outline_cache = pdf_outline(self.viewer.doc)
                entries, source = self.viewer._outline_cache
                hint = ("Chapters and sections from this document." if source == "bookmarks"
                        else "No built-in contents found. Browse by page.")
            elif isinstance(self.viewer, WebArticleViewer):
                entries = [entry for entry in web_outline(self.viewer.article)
                           if entry["block_index"] in self.viewer.block_starts]
                hint = "Headings from this webpage." if entries else "This webpage has no section headings."
            else:
                text = self.text.get("1.0", "end-1c")
                entries = [] if text.strip() == getattr(self, "_welcome_text", "") else text_outline(text)
                hint = ("Headings from the current text." if entries else
                        "No section headings found. Open a document, or use Chapter headings or # headings in text.")
        except Exception:
            hint = "Contents could not be loaded for this document."
        self.contents_hint.set(hint)
        self.contents_instruction.set(
            "Select a section to jump. Double-click text to read from there."
            if self.viewer is None else
            "Select a section to jump. Click document text to read from there.")
        parents = []
        for number, entry in enumerate(entries):
            level = max(1, min(int(entry.get("level", 1)), len(parents) + 1))
            parents = parents[:level - 1]
            item = f"section-{number}"
            page = entry.get("page")
            self.contents_tree.insert(
                parents[-1] if parents else "", "end", iid=item,
                text=entry["title"], values=(page + 1 if page is not None else "",), open=True)
            parents.append(item)
            self._contents_entries[item] = entry

    def _contents_text_changed(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self.viewer is None and getattr(self, "_contents_open", False):
            # Invalidate immediately; do not navigate stale line numbers during debounce.
            self._contents_entries = {}
            if self._contents_refresh_after is not None:
                self.root.after_cancel(self._contents_refresh_after)
            self._contents_refresh_after = self.root.after(250, self._refresh_contents)

    def _contents_selected(self, _event=None):
        selected = self.contents_tree.selection()
        entry = self._contents_entries.get(selected[0]) if selected else None
        if entry is not None:
            self._navigate_contents_entry(entry, self._contents_viewer)
        return "break"

    def _navigate_contents_entry(self, entry, expected_viewer):
        if self.viewer is not expected_viewer or self._library_is_visible():
            return False
        with self._playback_lock:
            document_cancel = getattr(self, "_document_read_cancel", None)
            pause_document = (
                expected_viewer is not None and document_cancel is not None
                and getattr(self, "_document_read_viewer", None) is expected_viewer
                and document_cancel is self._active_cancel
                and document_cancel in self._speech_requests and not document_cancel.is_set())
            if pause_document:
                self._pause_requested.set()
                self._contents_browsing_viewer = expected_viewer
        if pause_document:
            self._set_playback_state("paused")
        if isinstance(expected_viewer, PdfViewer):
            navigated = expected_viewer.go_to_destination(entry.get("page"), entry.get("y", 0))
        elif isinstance(expected_viewer, WebArticleViewer):
            navigated = expected_viewer.go_to_section(entry.get("block_index"))
        elif expected_viewer is None and entry.get("index"):
            self.text.mark_set("insert", entry["index"])
            self.text.see(entry["index"])
            navigated = True
        else:
            return False
        if navigated:
            prefix = "Paused. " if pause_document else ""
            action = "double-click" if expected_viewer is None else "click"
            self.set_status(prefix + entry["title"] + f" — {action} text to read from here.")
        return navigated

    def _build_action_bar(self):
        actions = tk.Frame(self.shell, bg=APP_BG, height=76)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        actions.grid_propagate(False)
        definitions = (
            ("Open / OCR...", "PDF, Word, Image, Text", "folder",
             self.open_file, True),
            ("Open webpage...", "Paste a page URL", "globe",
             self.open_webpage, False),
            ("Play", "Start reading", "play", self.toggle_play_pause, False),
            ("Read clipboard", "Read clipboard content", "clipboard",
             self.read_clipboard, False),
            ("Stop", "Stop reading", "stop", self.stop, False),
            ("Save to WAV...", "Export audio file", "wave", self.save_wav,
             False),
        )
        for column in range(len(definitions)):
            actions.grid_columnconfigure(column, weight=1, uniform="actions")
        actions.grid_rowconfigure(0, weight=1)

        self.action_tiles = []
        for column, definition in enumerate(definitions):
            title, subtitle, icon, command, primary = definition
            tile = ActionTile(actions, title, subtitle, icon, command,
                              APP_BG, primary=primary)
            tile.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else 6,
                            0 if column == len(definitions) - 1 else 6))
            self.action_tiles.append(tile)
            if title == "Play":
                self.play_pause_tile = tile

    def _build_status_bar(self):
        status = GlassCard(self.shell, APP_BG, height=56, radius=15, pad=11,
                           fill=CARD_SOFT, shadow=False)
        status.grid(row=4, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="Ready.")
        self.status_dot = tk.Canvas(status.inner, width=18, height=18,
                                    bg=CARD_SOFT, highlightthickness=0, bd=0)
        self.status_dot.pack(side="left", padx=(2, 7))
        self.status_dot_id = self.status_dot.create_oval(
            3, 3, 15, 15, fill=SUCCESS, outline="")
        self.status_label = tk.Label(
            status.inner, textvariable=self.status_var, anchor="w",
            bg=CARD_SOFT, fg=INK_SOFT, font=(FONT_UI, 10))
        self.status_label.pack(side="left", fill="x", expand=True)

        self.zoom_controls = tk.Frame(status.inner, bg=CARD_SOFT)
        self.zoom_controls.pack(side="right")
        self.zoom_var = tk.StringVar(value="100%")
        tk.Label(self.zoom_controls, text="Zoom", bg=CARD_SOFT,
                 fg=INK_MUTED, font=(FONT_UI, 9)).pack(
                     side="left", padx=(0, 7))
        self.zoom_out_button = CompactControlButton(
            self.zoom_controls, "zoom_out", self.zoom_out, bg=CARD_SOFT)
        self.zoom_out_button.pack(side="left")
        self.zoom_label = tk.Label(
            self.zoom_controls, textvariable=self.zoom_var, bg=CARD_SOFT,
            fg=INK_SOFT, width=6, font=(FONT_UI, 9, "bold"))
        self.zoom_label.pack(side="left", padx=4)
        self.zoom_in_button = CompactControlButton(
            self.zoom_controls, "zoom_in", self.zoom_in, bg=CARD_SOFT)
        self.zoom_in_button.pack(side="left")

        # Memorization controls stay in the status strip so they do not take
        # height away from the document reading surface.
        self.repeat_controls = tk.Frame(status.inner, bg=CARD_SOFT)
        self.repeat_controls.pack(side="right", padx=(0, 16))
        tk.Frame(self.repeat_controls, bg=CARD_LINE, width=1, height=26).pack(
            side="left", padx=(0, 12))
        tk.Label(self.repeat_controls, text="Repeat", bg=CARD_SOFT,
                 fg=INK_MUTED, font=(FONT_UI, 9)).pack(
                     side="left", padx=(0, 7))
        repeat_mode = normalize_repeat_mode(
            self.settings.get("repeat_mode", "off"))
        self.repeat_mode_var = tk.StringVar(
            value=REPEAT_MODE_LABELS[repeat_mode])
        self.repeat_mode_combo = ttk.Combobox(
            self.repeat_controls, textvariable=self.repeat_mode_var,
            values=list(REPEAT_MODE_LABELS.values()), state="readonly",
            width=10, style="CompactVoice.TCombobox", font=(FONT_UI, 9))
        self.repeat_mode_combo.pack(side="left")
        self.repeat_mode_combo.bind(
            "<<ComboboxSelected>>", self._on_repeat_mode_changed)
        self.repeat_count_var = tk.StringVar(value=str(normalize_repeat_count(
            self.settings.get("repeat_count", REPEAT_COUNT_DEFAULT))))
        validate_count = (
            self.root.register(self._validate_repeat_count_input), "%P")
        self.repeat_count_spin = ttk.Spinbox(
            self.repeat_controls, from_=REPEAT_COUNT_MIN,
            to=REPEAT_COUNT_MAX, increment=1,
            textvariable=self.repeat_count_var, width=3,
            style="Repeat.TSpinbox", font=(FONT_UI, 9, "bold"),
            validate="key", validatecommand=validate_count,
            command=self._on_repeat_count_changed)
        self.repeat_count_spin.pack(side="left", padx=(7, 4))
        self.repeat_count_spin.bind("<FocusOut>", self._on_repeat_count_changed)
        self.repeat_count_spin.bind("<Return>", self._on_repeat_count_changed)
        tk.Label(self.repeat_controls, text="times", bg=CARD_SOFT,
                 fg=INK_MUTED, font=(FONT_UI, 9)).pack(side="left")
        self._sync_repeat_controls(save=False)

    @staticmethod
    def _validate_repeat_count_input(proposed):
        return (proposed == "" or
                (proposed.isdigit() and len(proposed) <= 2))

    def _repeat_snapshot(self):
        mode = normalize_repeat_mode(self.repeat_mode_var.get())
        count = normalize_repeat_count(self.repeat_count_var.get())
        return mode, (1 if mode == "off" else count)

    def _sync_repeat_controls(self, save=True):
        mode = normalize_repeat_mode(self.repeat_mode_var.get())
        count = normalize_repeat_count(self.repeat_count_var.get())
        if self.repeat_count_var.get() != str(count):
            self.repeat_count_var.set(str(count))
        self.repeat_count_spin.configure(
            state="disabled" if mode == "off" else "normal")
        if save:
            self._save_settings()

    def _on_repeat_mode_changed(self, *_):
        self._sync_repeat_controls(save=True)

    def _on_repeat_count_changed(self, *_):
        normalized = normalize_repeat_count(self.repeat_count_var.get())
        self.repeat_count_var.set(str(normalized))
        self._save_settings()

    def _go_glass(self):
        """Compatibility hook retained for old shortcuts and patch scripts."""
        self.root.configure(bg=APP_BG)

    def toggle_max(self):
        """Maximise to full screen, or restore to the previous size."""
        if self.root.state() == "zoomed":
            self.root.state("normal")
            if hasattr(self, "max_btn"):
                self.max_btn.configure(text="\u25a1")
        else:
            self.root.attributes("-fullscreen", False)
            self.root.state("zoomed")
            if hasattr(self, "max_btn"):
                self.max_btn.configure(text="\u2750")

    def toggle_fullscreen(self, *_):
        on = not bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", on)

    def set_status(self, msg):
        if self._shutdown_event.is_set():
            return
        try:
            self.root.after(0, self._apply_status, msg)
        except Exception:
            pass

    def _apply_status(self, msg):
        if not hasattr(self, "status_var"):
            return
        self.status_var.set(msg)
        low = msg.lower()
        if "error" in low or "failed" in low:
            color = DANGER
        elif low.startswith("ready") or "saved:" in low:
            color = SUCCESS
        elif low.startswith("stopped"):
            color = INK_MUTED
        else:
            color = ACCENT
        if hasattr(self, "status_dot_id"):
            self.status_dot.itemconfigure(self.status_dot_id, fill=color)

    def _set_request_status(self, cancel_event, message, phase=None):
        """Apply status only while this request still owns playback."""
        def apply():
            with self._playback_lock:
                if (self._shutdown_event.is_set() or cancel_event.is_set()
                        or cancel_event not in self._speech_requests
                        or self._active_cancel is not cancel_event):
                    return
                display_message, display_phase = message, phase
                if (phase == "preparing" and getattr(
                        self, "_audio_playing_cancel", None) is cancel_event):
                    display_phase = "playing"
                    display_message = (
                        self._active_audio_message + " Next audio: " + message)
                self._active_request_message = display_message
                if display_phase is not None:
                    self._active_request_phase = display_phase
                waiting = sum(
                    event is not cancel_event and not event.is_set()
                    for event in getattr(self, "_clipboard_enqueued_at", {}))
                status = display_message
                if waiting:
                    noun = "passage" if waiting == 1 else "passages"
                    status += f" {waiting} copied {noun} waiting."
                self._last_speaking_status = status
                if self._pause_requested.is_set():
                    return
            self._apply_status(status)
            self._apply_playback_state("playing")
        if not self._shutdown_event.is_set():
            try:
                self.root.after(0, apply)
            except Exception:
                pass

    def _engine_for_speech_request(self, kind, engine_key, cancel_event):
        if kind != "clipboard" or engine_key != "edge":
            return self.engines[engine_key]
        status = lambda message: self._set_request_status(
            cancel_event, message, phase="preparing")
        online = EdgeEngine(status, pip_install, max_attempts=2,
                            request_timeout=CLIPBOARD_ONLINE_TIMEOUT)
        enabled = getattr(self, "_clipboard_offline_backup_enabled", None)
        if enabled is None:
            variable = getattr(self, "clipboard_backup_var", None)
            enabled = variable.get() if variable is not None else True
        if not enabled:
            return online
        def fallback(_error):
            with self._playback_lock:
                self._clipboard_online_retry_after = (
                    time.monotonic() + CLIPBOARD_OFFLINE_COOLDOWN)
        offline = self.engines["piper"]
        with self._playback_lock:
            cooling_down = time.monotonic() < getattr(
                self, "_clipboard_online_retry_after", 0.0)
        return ClipboardSpeechEngine(
            online, offline, offline_voice_id=CLIPBOARD_BACKUP_VOICE_ID,
            offline_available=lambda: (
                offline.ready() and PiperEngine._voice_files_ready(
                    CLIPBOARD_BACKUP_VOICE_ID)),
            start_offline=cooling_down, on_fallback=fallback, status_cb=status)

    # ---------------- reading controls
    def _sync_speed_readout(self):
        speed = normalize_speed(self.speed_var.get())
        if hasattr(self, "speed_readout_var"):
            self.speed_readout_var.set(
                f"{format_speed(speed)}\n{speed_description(speed)}  \u25be")
        self._sync_speed_panel()

    def _publish_speed_setting(self, speed):
        bridge = getattr(self, "_browser_bridge", None)
        if bridge is not None:
            bridge.update_state(speed=round(normalize_speed(speed), 2))

    def _set_speed(self, value, save=True, snap=False):
        speed = quantize_speed(value) if snap else normalize_speed(value)
        self.speed_var.set(speed)
        self._sync_speed_readout()
        self._publish_speed_setting(speed)
        if save and hasattr(self, "auto_var"):
            self._save_settings()
        return speed

    def _adjust_speed(self, direction):
        speed = self._set_speed(
            stepped_speed(self.speed_var.get(), direction), save=True)
        self.set_status(f"Reading speed: {format_speed(speed)}.")
        return "break"

    def _on_speed_scale(self, value):
        self._set_speed(value, save=False, snap=True)

    def _create_speed_panel(self):
        panel = GlassCard(
            self.shell, APP_BG, width=540, height=390, radius=22, pad=20,
            fill=CARD, outline=CARD_LINE_STRONG, shadow=True)
        inner = panel.inner
        inner.grid_columnconfigure(0, weight=1)

        header = tk.Frame(inner, bg=CARD)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header, text="Reading speed", bg=CARD, fg=INK,
            font=(FONT_UI, 18, "bold")).grid(row=0, column=0, sticky="w")
        self._speed_panel_close = tk.Button(
            header, text="\u00d7", command=self._hide_speed_panel,
            bg=CARD, fg=INK, activebackground=ACCENT_SOFT,
            activeforeground=INK, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2", takefocus=True,
            font=(FONT_UI, 22), padx=8, pady=0)
        self._speed_panel_close.grid(row=0, column=1, sticky="e")

        self._speed_panel_category_var = tk.StringVar()
        self._speed_panel_value_var = tk.StringVar()
        tk.Label(
            inner, textvariable=self._speed_panel_category_var,
            bg=CARD, fg=INK_SOFT,
            font=(FONT_UI, 12)).grid(row=1, column=0, pady=(2, 8))

        stepper = tk.Frame(inner, bg=CARD)
        stepper.grid(row=2, column=0, pady=(0, 10))
        self._make_speed_button(
            stepper, lambda: self._adjust_speed(-1), text="\u2212",
            width=3, font_size=18).grid(row=0, column=0, padx=(0, 20))
        tk.Label(
            stepper, textvariable=self._speed_panel_value_var,
            bg=CARD, fg=INK, width=7,
            font=(FONT_UI, 23, "bold")).grid(row=0, column=1)
        self._make_speed_button(
            stepper, lambda: self._adjust_speed(1), text="+",
            width=3, font_size=18).grid(row=0, column=2, padx=(20, 0))

        self._speed_panel_scale_var = tk.DoubleVar()
        self._speed_panel_scale = ttk.Scale(
            inner, from_=SPEED_MIN, to=SPEED_MAX,
            variable=self._speed_panel_scale_var,
            command=self._on_speed_scale, orient="horizontal",
            style="Speed.Horizontal.TScale", takefocus=True)
        self._speed_panel_scale.grid(
            row=3, column=0, sticky="ew", padx=26, pady=(0, 12))
        self._speed_panel_scale.bind(
            "<ButtonRelease-1>", lambda _event: self._save_settings())
        self._speed_panel_scale.bind(
            "<KeyRelease>", lambda _event: self._save_settings())

        presets = tk.Frame(inner, bg=CARD)
        presets.grid(row=4, column=0, pady=(0, 12))
        for index, preset in enumerate(SPEED_PRESETS):
            button = self._make_speed_button(
                presets,
                lambda selected=preset: self._choose_speed_preset(selected),
                text=format_speed(preset), width=6, font_size=10)
            button.grid(row=index // 3, column=index % 3, padx=5, pady=4)
            self._speed_panel_buttons[preset] = button

        self._speed_panel_hint_var = tk.StringVar()
        tk.Label(
            inner, textvariable=self._speed_panel_hint_var,
            bg=CARD, fg=INK_MUTED, justify="center",
            font=(FONT_UI, 9)).grid(row=5, column=0, pady=(0, 2))
        self._speed_panel = panel

    def _sync_speed_panel(self):
        panel = getattr(self, "_speed_panel", None)
        if panel is None:
            return
        speed = normalize_speed(self.speed_var.get())
        self._speed_panel_value_var.set(format_speed(speed))
        self._speed_panel_category_var.set(speed_description(speed))
        if self._speed_panel_scale_var is not None:
            self._speed_panel_scale_var.set(speed)
        for preset, button in self._speed_panel_buttons.items():
            selected = math.isclose(speed, preset, abs_tol=0.001)
            button.configure(
                bg=ACCENT if selected else CARD_SOFT,
                fg="#ffffff" if selected else INK,
                activebackground=ACCENT_DARK if selected else ACCENT_SOFT,
                activeforeground="#ffffff" if selected else INK)
        active = getattr(self, "playback_state", "idle") in (
            "playing", "paused")
        self._speed_panel_hint_var.set(
            "Applies to the next reading; current and queued audio keep their speed."
            if active else
            "Saved for text, documents, web pages, clipboard reading, and WAV export.")

    def _choose_speed_preset(self, value):
        speed = self._set_speed(value, save=True)
        self.set_status(f"Reading speed: {format_speed(speed)}.")
        self._hide_speed_panel(save=False)

    def _show_speed_panel(self):
        if self._speed_panel is None:
            self._create_speed_panel()
        self._sync_speed_panel()
        self._speed_panel.place(
            relx=0.5, rely=0.12, anchor="n", width=540, height=390)
        self._speed_panel.lift()
        self._speed_panel_close.focus_set()

    def _hide_speed_panel(self, *_args, save=True):
        panel = getattr(self, "_speed_panel", None)
        if panel is None or not panel.winfo_ismapped():
            return False
        if save and hasattr(self, "auto_var"):
            self._save_settings()
        panel.place_forget()
        try:
            if self.root.state() != "withdrawn":
                self.speed_value_button.focus_set()
        except Exception:
            pass
        return True

    # ---------------- local Library
    def _library_is_visible(self):
        view = getattr(self, "library_view", None)
        try:
            return bool(view is not None and view.winfo_ismapped())
        except tk.TclError:
            return False

    def _list_library_items(self, search="", sort="recent"):
        if self.library_store is None:
            return []
        try:
            return self.library_store.list_items(search=search, sort=sort)
        except Exception as exc:
            self.library_error = str(exc)
            self.set_status(f"Library error: {exc}")
            return []

    def show_library(self, *_):
        self._hide_speed_panel()
        self._hide_contents()
        if self.library_store is None:
            detail = self.library_error or "local Library storage is unavailable"
            messagebox.showerror(
                "Cannot open Library",
                "Piper Reader left your existing Library untouched, but this "
                f"version could not open it:\n\n{detail}", parent=self.root)
            return "break"
        if self.library_view is None:
            self.library_view = LibraryView(self.editor_host, self)
        self.library_view.grid(row=0, column=0, sticky="nsew")
        self.library_view.lift()
        self.library_view.refresh()
        self.library_button.configure(bg=ACCENT_SOFT)
        try:
            self.library_view.search_entry.focus_set()
        except tk.TclError:
            pass
        self.set_status(
            "Library is stored privately on this computer. Clipboard text "
            "is never saved automatically.")
        return "break"

    def _close_library(self, *_):
        if not self._library_is_visible():
            return False
        self.library_view.grid_remove()
        self.library_button.configure(bg=CARD)
        try:
            if self.viewer:
                self.viewer.focus_set()
            else:
                self.text.focus_set()
        except tk.TclError:
            pass
        return True

    def toggle_library(self, *_):
        if self._library_is_visible():
            self._close_library()
        else:
            self.show_library()
        return "break"

    def _ai_source_snapshot(self):
        """Copy visible source text without changing the document or Library."""
        if self.viewer:
            return (self.viewer.text().strip(),
                    str(getattr(self.viewer, "title", "") or "Current document"))
        text = self.text.get("1.0", "end").strip()
        if text == getattr(self, "_welcome_text", None):
            text = ""
        return text, str(self.current_text_name or "Current text")

    def _ai_studio_is_open(self):
        studio = getattr(self, "ai_studio", None)
        try:
            return studio is not None and bool(studio.winfo_exists())
        except tk.TclError:
            return False

    def _open_studios(self):
        """Live editing windows, including each independent audio worker."""
        studios = []
        for name in ("ai_studio", "textbook_studio"):
            studio = getattr(self, name, None)
            try:
                if studio is not None and studio.winfo_exists():
                    studios.append(studio)
            except tk.TclError:
                pass
        return studios

    def open_textbook_studio(self, *_):
        studio = getattr(self, "textbook_studio", None)
        if studio in self._open_studios():
            studio.deiconify()
            studio.lift()
            return "break"
        try:
            source, title = self._ai_source_snapshot()
            from textbook_studio import TextbookStudioWindow
            self.textbook_studio = TextbookStudioWindow(
                self, source, title, data_dir=DATA_DIR)
            self.set_status("Textbook Studio opened. Clipboard auto-reading stays available.")
        except Exception as exc:
            messagebox.showerror("Cannot open Textbook Studio", str(exc), parent=self.root)
        return "break"

    def open_ai_studio(self, *_):
        if self._ai_studio_is_open():
            self.ai_studio.deiconify()
            self.ai_studio.lift()
            return "break"
        try:
            source, title = self._ai_source_snapshot()
            from ai_studio_ui import AIStudioWindow
            self.ai_studio = AIStudioWindow(
                self, source, title, data_dir=DATA_DIR)
            self.set_status(
                "AI Studio opened. Clipboard auto-reading stays available.")
        except Exception as exc:
            messagebox.showerror("Cannot open AI Studio", str(exc), parent=self.root)
        return "break"

    def _ai_studio_owns_audio(self):
        # The Studio reserves speakers only once audio is ready. Script and
        # audio preparation leave the independent clipboard reader available.
        return any(bool(getattr(studio, "owns_audio", False))
                   for studio in self._open_studios())

    def _try_reserve_studio_audio(self, studio):
        """Called on Tk's thread: one ready studio can reserve the speakers."""
        owner = getattr(self, "_studio_audio_owner", None)
        if owner is studio:
            return True
        if owner in self._open_studios() and bool(getattr(owner, "owns_audio", False)):
            return False
        self._studio_audio_owner = studio
        return True

    def _reader_audio_is_busy(self):
        with self._playback_lock:
            return bool(self._speech_requests)

    def _refresh_library_view(self):
        if self.library_view is not None:
            try:
                self.library_view.refresh()
            except tk.TclError:
                pass

    def _library_import(self):
        self._close_library()
        self.open_file()

    def _save_current_text_to_library(self):
        if self.library_store is None:
            self.show_library()
            return
        try:
            text = (self.viewer.text() if self.viewer
                    else self.text.get("1.0", "end")).strip()
        except Exception as exc:
            messagebox.showerror(
                "Cannot save text", str(exc), parent=self.root)
            return
        if not text:
            messagebox.showinfo(
                "Nothing to save", "There is no text in the reader yet.",
                parent=self.root)
            return
        default_title = ""
        if self.viewer:
            default_title = str(getattr(self.viewer, "title", "") or "")
        if not default_title:
            default_title = str(self.current_text_name or "")
        if not default_title:
            default_title = next(
                (line.strip() for line in text.splitlines() if line.strip()),
                "Saved text")[:80]
        title = simpledialog.askstring(
            "Save text to Library", "Give this text a title:",
            initialvalue=default_title, parent=self.root)
        if title is None:
            return
        try:
            item = self.library_store.add_text(text, title)
            self.current_library_item_id = item["id"]
            self.current_text_name = item["title"]
            self._refresh_library_view()
            self.set_status(f"Saved to Library: {item['title']}")
        except LibraryError as exc:
            messagebox.showerror(
                "Cannot save text", str(exc), parent=self.root)

    def _selected_library_id(self):
        if self.library_view is None:
            return None
        return self.library_view.selected_id()

    def _open_library_selection(self):
        item_id = self._selected_library_id()
        if item_id:
            self._open_library_item(item_id)

    def _open_library_item(self, item_id):
        if self.library_store is None:
            return
        try:
            item = self.library_store.get_item(item_id)
            if item is None:
                raise LibraryError("That Library item no longer exists")
            if item["kind"] == "text":
                text = self.library_store.read_text(item)
                self.library_store.touch(item_id)
                self._close_library()
                serial = self._begin_content_load()
                self._show_text_document(
                    text, item["title"], serial,
                    library_item_id=item_id)
            else:
                path = self.library_store.resolve_item_path(item)
                self.library_store.touch(item_id)
                self._close_library()
                self._open_file_path(
                    path, remember=False, library_item_id=item_id,
                    display_name=item["title"])
        except LibraryError as exc:
            messagebox.showerror(
                "Cannot open Library item", str(exc), parent=self.root)
            self._refresh_library_view()

    def _remove_library_selection(self):
        item_id = self._selected_library_id()
        if not item_id or self.library_store is None:
            return
        item = self.library_store.get_item(item_id)
        if item is None:
            self._refresh_library_view()
            return
        if not messagebox.askyesno(
                "Remove from Library?",
                f"Remove \"{item['title']}\" from Piper Reader Library?\n\n"
                "This deletes only Piper Reader's stored copy. The original "
                "file, if it still exists, will not be changed.",
                parent=self.root):
            return
        if self.current_library_item_id == item_id:
            if self.viewer:
                self._close_viewer(cancel_load=True)
            self.current_library_item_id = None
        try:
            self.library_store.remove(item_id)
            self._refresh_library_view()
            self.set_status(f"Removed from Library: {item['title']}")
        except LibraryError as exc:
            # The index removal is transactional and has already succeeded;
            # this warning is for a locked orphaned managed copy only.
            self._refresh_library_view()
            messagebox.showwarning(
                "Library item removed", str(exc), parent=self.root)

    def _handle_escape(self, *_):
        if self._hide_speed_panel():
            return "break"
        if self._close_library():
            return "break"
        self.root.attributes("-fullscreen", False)
        return "break"

    def _text_zoom_percent(self):
        return round(self.text_font_size / 13 * 100)

    def _adjust_zoom(self, direction):
        if self._library_is_visible():
            return "break"
        if self.viewer:
            percent = self.viewer.zoom_by(direction)
        else:
            step = 1 if direction > 0 else -1
            self.text_font_size = max(9, min(26,
                                            self.text_font_size + step))
            self.text_font.configure(size=self.text_font_size)
            percent = self._text_zoom_percent()
        self.zoom_var.set(f"{percent}%")
        return "break"

    def zoom_in(self, *_):
        return self._adjust_zoom(1)

    def zoom_out(self, *_):
        return self._adjust_zoom(-1)

    def reset_zoom(self, *_):
        if self._library_is_visible():
            return "break"
        if self.viewer:
            percent = self.viewer.reset_zoom()
        else:
            self.text_font_size = 13
            self.text_font.configure(size=self.text_font_size)
            percent = 100
        self.zoom_var.set(f"{percent}%")
        return "break"

    def _set_playback_state(self, state):
        if self._shutdown_event.is_set():
            return
        try:
            if threading.current_thread() is threading.main_thread():
                self._apply_playback_state(state)
            else:
                self.root.after(0, self._apply_playback_state, state)
        except Exception:
            pass

    def _apply_playback_state(self, state):
        self.playback_state = state
        self._update_tray_title()
        self._sync_speed_panel()
        bridge = self._browser_bridge
        if bridge is not None:
            with self._playback_lock:
                browser_owns_playback = (
                    self._browser_request_cancel is not None and
                    self._active_cancel is self._browser_request_cancel and
                    self._browser_request_cancel in self._speech_requests
                )
            browser_session = (self._browser_active_session
                               if browser_owns_playback else None)
            bridge.update_state(
                playback_state=state,
                active_session=browser_session,
                voice=self.voice_var.get(),
                speed=round(float(self.speed_var.get()), 2),
                volume=round(float(self.vol_var.get()), 2),
            )
            if browser_session:
                bridge.publish(
                    browser_session, "state", state=state)
        if not hasattr(self, "play_pause_tile"):
            return
        if state == "paused":
            self.play_pause_tile.set_content(
                "Resume", "Continue reading", "play")
        elif state == "playing":
            subtitle = ("Waiting for voice" if getattr(
                self, "_active_request_phase", None) == "preparing"
                else "Pause reading")
            self.play_pause_tile.set_content(
                "Pause", subtitle, "pause")
        else:
            self.play_pause_tile.set_content(
                "Play", "Start reading", "play")

    def _complete_playback_request(self, cancel_event, completed=False):
        """Retire one request without idling a newer FIFO request."""
        with self._playback_lock:
            self._speech_requests.discard(cancel_event)
            getattr(self, "_clipboard_enqueued_at", {}).pop(cancel_event, None)
            if self._active_cancel is cancel_event:
                self._active_cancel = None
                self._active_pause = None
                self._active_request_phase = "preparing"
                self._active_request_message = ""
            has_more = bool(self._speech_requests)
            if not has_more:
                self._pause_requested.clear()
        self._apply_playback_state(
            ("paused" if self._pause_requested.is_set() else "playing")
            if has_more else "idle")
        if not has_more and not self._shutdown_event.is_set():
            self._try_runtime_reprime()
            # Startup speech can overlap the first warm attempt. Retry only
            # after the speech queue becomes idle so clipboard playback wins.
            self._cancel_after("_clipboard_warm_after_id")
            self._clipboard_warm_after_id = self.root.after(
                150, self._warm_clipboard_voice_async)
        if completed and not cancel_event.is_set() and not has_more:
            self.set_status("Ready.")

    def toggle_play_pause(self):
        with self._playback_lock:
            has_requests = bool(self._speech_requests)
        if not has_requests:
            self.read_box()
            return
        if self._pause_requested.is_set():
            self._pause_requested.clear()
            self._set_playback_state("playing")
            self.set_status(self._last_speaking_status)
        else:
            self._pause_requested.set()
            self._set_playback_state("paused")
            self.set_status("Paused.")

    # ---------------- settings
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            self.settings = loaded if isinstance(loaded, dict) else {}
        except Exception:
            self.settings = {}

    def _save_settings(self):
        try:
            self._clipboard_offline_backup_enabled = self.clipboard_backup_var.get()
            payload = {
                "voice": self.voice_var.get(),
                "clipboard_voice": self.clipboard_voice_var.get(),
                "clipboard_offline_backup": self._clipboard_offline_backup_enabled,
                "speed": normalize_speed(self.speed_var.get()),
                "volume": round(self.vol_var.get(), 2),
                "autoread": self.auto_var.get(),
                "ocr_language": normalize_ocr_mode(
                    self.ocr_language_var.get()),
                "repeat_mode": normalize_repeat_mode(
                    self.repeat_mode_var.get()),
                "repeat_count": normalize_repeat_count(
                    self.repeat_count_var.get()),
            }
            _replace_text_file(
                SETTINGS_FILE, json.dumps(payload, indent=2) + "\n")
        except Exception:
            pass

    def _poll_instance_commands(self):
        """Handle show/exit requests from a duplicate or installer launch."""
        self._instance_after_id = None
        payload = None
        try:
            with open(INSTANCE_COMMAND_FILE, encoding="utf-8") as stream:
                payload = json.load(stream)
            os.remove(INSTANCE_COMMAND_FILE)
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            try:
                os.remove(INSTANCE_COMMAND_FILE)
            except OSError:
                pass
        if isinstance(payload, dict):
            created = payload.get("created")
            fresh = (isinstance(created, (int, float)) and
                     0 <= time.time() - created <= 30)
            if fresh and payload.get("command") == "show":
                self._show_window()
            elif fresh and payload.get("command") == "exit":
                self._exit_app()
                return
        if not self._shutdown_event.is_set():
            try:
                self._instance_after_id = self.root.after(
                    250, self._poll_instance_commands)
            except Exception:
                self._instance_after_id = None

    # ---------------- browser extension bridge
    def _start_browser_bridge(self):
        """Accept authenticated commands from the local browser extension."""
        try:
            ensure_browser_bridge_config()
            bridge = BrowserBridge(
                self._browser_commands, BROWSER_BRIDGE_CONFIG)
            bridge.update_state(
                voice=self.voice_var.get(),
                speed=round(float(self.speed_var.get()), 2),
                volume=round(float(self.vol_var.get()), 2),
            )
            bridge.start()
            self._browser_bridge = bridge
        except Exception as exc:
            self._browser_bridge = None
            self.set_status(
                f"Browser extension unavailable: {exc}")

    def _poll_browser_commands(self):
        """Move bridge commands onto Tk's UI thread before touching the app."""
        self._browser_after_id = None
        if self._shutdown_event.is_set():
            return
        for _ in range(20):
            try:
                command = self._browser_commands.get_nowait()
            except queue.Empty:
                break
            self._handle_browser_command(command)
        try:
            self._browser_after_id = self.root.after(
                50, self._poll_browser_commands)
        except Exception:
            self._browser_after_id = None

    def _handle_browser_command(self, command):
        bridge = self._browser_bridge
        if bridge is None or self._shutdown_event.is_set():
            return
        action = command.get("action")
        session_id = command.get("session_id")
        if action == "read":
            chunks = command["chunks"]
            start_index = command["start_index"]
            self.stop(silent=True, browser_reason="superseded", cancel_exports=False)
            self._browser_active_session = session_id
            voice = self.voice_var.get()
            bridge.update_state(
                active_session=session_id,
                voice=voice,
                speed=round(float(self.speed_var.get()), 2),
                volume=round(float(self.vol_var.get()), 2),
            )
            bridge.publish(
                session_id,
                "session_started",
                title=command.get("title", "Webpage"),
                url=command.get("url", ""),
                chunk_count=len(chunks),
                start_index=start_index,
                voice=voice,
            )
            payload = [
                (item["text"], {
                    "browser_session": session_id,
                    "chunk_id": item["id"],
                    "index": item["index"],
                    "total": len(chunks),
                    "sentence_id": item.get("sentence_id", item["index"]),
                    "paragraph_id": item.get("paragraph_id", item["index"]),
                })
                for item in chunks[start_index:]
            ]
            self._last_speaking_status = (
                f"Reading webpage: {command.get('title', 'Webpage')}")
            self._browser_request_cancel = self._queue_speech(
                "browser", payload, replace=False)
            self._set_playback_state("playing")
            self.set_status(self._last_speaking_status)
            return

        if session_id != self._browser_active_session:
            bridge.publish(session_id, "stopped", reason="inactive")
            return
        with self._playback_lock:
            owns_playback = (
                self._browser_request_cancel is not None and
                self._active_cancel is self._browser_request_cancel and
                self._browser_request_cancel in self._speech_requests
            )
        if not owns_playback:
            bridge.publish(session_id, "stopped", reason="inactive")
            return
        if action == "stop":
            self.stop()
            return
        if action == "toggle":
            action = ("resume" if self._pause_requested.is_set()
                      else "pause")
        if action == "pause":
            self._pause_requested.set()
            self._set_playback_state("paused")
            self.set_status("Paused.")
        elif action == "resume":
            self._pause_requested.clear()
            self._set_playback_state("playing")
            self.set_status(self._last_speaking_status)

    def _finish_browser_session(self, session_id, cancel_event):
        """Clear bridge state without disturbing a newer browser request."""
        if (session_id != self._browser_active_session or
                cancel_event is not self._browser_request_cancel):
            return
        self._browser_active_session = None
        self._browser_request_cancel = None
        bridge = self._browser_bridge
        if bridge is not None:
            bridge.update_state(active_session=None)

    # ---------------- notification-area tray
    @staticmethod
    def _make_tray_image():
        """Create a bold icon that remains legible at Windows tray sizes."""
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 3, 61, 61), radius=16, fill=ACCENT)
        # A simple white audio waveform survives Windows' 16-pixel rendering.
        heights = (16, 28, 40, 52, 40, 28, 16)
        for x, height in zip((17, 22, 27, 32, 37, 42, 47), heights):
            draw.rounded_rectangle(
                (x - 2, 32 - height // 2, x + 2, 32 + height // 2),
                radius=2, fill="white")
        return image

    def _start_tray_support(self):
        """Start the tray icon now, installing its small helper if required."""
        if self._tray_starting or self._tray_icon or self._shutdown_event.is_set():
            return
        self._tray_starting = True
        try:
            import pystray
            from PIL import Image  # noqa: F401 - verifies Pillow is available
            self._create_tray_icon(pystray)
        except ImportError:
            threading.Thread(target=self._install_tray_support,
                             daemon=True).start()
        except Exception as exc:
            self._set_tray_failure(exc)

    def _install_tray_support(self):
        """One-time self-healing setup for copies run with a fresh Python."""
        try:
            if not module_ok("pystray"):
                if not pip_install("pystray", self.set_status):
                    raise RuntimeError("pystray could not be installed")
            if not module_ok("PIL"):
                if not pip_install("pillow", self.set_status):
                    raise RuntimeError("Pillow could not be installed")
            import importlib
            importlib.invalidate_caches()
            import pystray
            if self._shutdown_event.is_set():
                self._tray_starting = False
                return
            self._create_tray_icon(pystray)
        except Exception as exc:
            self._set_tray_failure(exc)

    def _create_tray_icon(self, pystray):
        if self._shutdown_event.is_set():
            self._tray_starting = False
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open Piper Reader", self._tray_request_open,
                             default=True),
            pystray.MenuItem("Play / Pause", self._tray_request_play_pause),
            pystray.MenuItem("Stop reading", self._tray_request_stop),
            pystray.MenuItem("Read clipboard now",
                             self._tray_request_clipboard),
            pystray.MenuItem("Auto-read clipboard",
                             self._tray_request_toggle_autoread,
                             checked=self._tray_autoread_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Browser extension folder",
                             self._tray_request_extension_folder),
            pystray.MenuItem("Exit Piper Reader", self._tray_request_exit),
        )
        icon = pystray.Icon("piper_reader", self._make_tray_image(),
                            "Piper Reader - Ready", menu)
        self._tray_icon = icon
        self._tray_starting = False

        def mark_ready(ready_icon):
            ready_icon.visible = True
            self._tray_ready.set()

        # This is pystray's supported integration path for another UI loop.
        icon.run_detached(mark_ready)

    def _set_tray_failure(self, exc):
        self._tray_failed = str(exc) or exc.__class__.__name__
        self._tray_starting = False
        self.set_status("Tray icon unavailable; Piper Reader will stay open.")

    def _tray_autoread_checked(self, _item):
        # pystray calls this outside Tk; read only the mirrored Python boolean.
        return self._tray_autoread

    def _sync_tray_autoread(self, *_):
        self._tray_autoread = bool(self.auto_var.get())
        self._refresh_tray_menu()

    def _refresh_tray_menu(self):
        icon = self._tray_icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def _update_tray_title(self):
        icon = self._tray_icon
        if icon is None:
            return
        label = {"playing": "Speaking", "paused": "Paused"}.get(
            self.playback_state, "Ready")
        try:
            icon.title = f"Piper Reader - {label}"
        except Exception:
            pass

    def _queue_tray_command(self, command):
        if not self._shutdown_event.is_set():
            self._tray_commands.put(command)

    def _tray_request_open(self, *_):
        self._queue_tray_command("open")

    def _tray_request_play_pause(self, *_):
        self._queue_tray_command("play_pause")

    def _tray_request_stop(self, *_):
        self._queue_tray_command("stop")

    def _tray_request_clipboard(self, *_):
        self._queue_tray_command("clipboard")

    def _tray_request_toggle_autoread(self, *_):
        self._queue_tray_command("toggle_autoread")

    def _tray_request_extension_folder(self, *_):
        self._queue_tray_command("extension_folder")

    def _tray_request_exit(self, *_):
        self._queue_tray_command("exit")

    def _poll_tray_commands(self):
        self._tray_after_id = None
        if self._shutdown_event.is_set():
            return
        while True:
            try:
                command = self._tray_commands.get_nowait()
            except queue.Empty:
                break
            if command == "open":
                self._show_window()
            elif command == "play_pause":
                self.toggle_play_pause()
            elif command == "stop":
                self.stop()
            elif command == "clipboard":
                self.read_clipboard()
            elif command == "toggle_autoread":
                self.auto_var.set(not self._tray_autoread)
                self._save_settings()
            elif command == "extension_folder":
                try:
                    ensure_browser_bridge_config()
                    os.startfile(BROWSER_EXTENSION_DIR)
                except Exception as exc:
                    self.set_status(
                        f"Could not open browser extension folder: {exc}")
            elif command == "exit":
                self._exit_app()
                return
        try:
            self._tray_after_id = self.root.after(100,
                                                   self._poll_tray_commands)
        except Exception:
            self._tray_after_id = None

    def _hide_to_tray(self, *_):
        """Hide the window without interrupting speech or clipboard polling."""
        if self._shutdown_event.is_set():
            return "break"
        self._hide_speed_panel(save=False)
        self._save_settings()
        if self._tray_ready.is_set():
            self._finish_hide_to_tray()
            return "break"
        if self._tray_failed:
            if self.root.state() == "withdrawn":
                self._show_window()
            messagebox.showwarning(
                "Tray icon unavailable",
                "Piper Reader could not start its notification-area icon, so "
                "the window will remain open.\n\n" + self._tray_failed)
            return "break"
        self._pending_tray_hide = True
        self.set_status("Preparing the notification-area icon...")
        self._start_tray_support()
        if self._tray_hide_after_id is None:
            self._tray_hide_after_id = self.root.after(
                100, self._finish_pending_tray_hide)
        return "break"

    def _finish_pending_tray_hide(self):
        self._tray_hide_after_id = None
        if self._shutdown_event.is_set() or not self._pending_tray_hide:
            return
        if self._tray_ready.is_set():
            self._finish_hide_to_tray()
        elif self._tray_failed:
            self._pending_tray_hide = False
            if self.root.state() == "withdrawn":
                self._show_window()
            messagebox.showwarning(
                "Tray icon unavailable",
                "Piper Reader could not start its notification-area icon, so "
                "the window will remain open.\n\n" + self._tray_failed)
        else:
            self._tray_hide_after_id = self.root.after(
                100, self._finish_pending_tray_hide)

    def _finish_hide_to_tray(self):
        self._pending_tray_hide = False
        state = self.root.state()
        if state in ("normal", "zoomed"):
            self._window_state_before_hide = state
        self._fullscreen_before_hide = bool(
            self.root.attributes("-fullscreen"))
        self.root.withdraw()
        if not self._tray_notice_shown:
            detail = ("Clipboard auto-read remains active."
                      if self._tray_autoread
                      else "Piper Reader is still running.")
            try:
                self._tray_icon.notify(
                    detail + " Open or exit it from the notification-area icon.",
                    "Piper Reader is running in the background")
            except Exception:
                pass
            self._tray_notice_shown = True

    def _show_window(self):
        if self._shutdown_event.is_set():
            return
        self.root.deiconify()
        if self._fullscreen_before_hide:
            self.root.attributes("-fullscreen", True)
        elif self._window_state_before_hide == "zoomed":
            self.root.state("zoomed")
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(150, self._release_topmost)
            self.root.focus_force()
        except Exception:
            pass

    def _release_topmost(self):
        if not self._shutdown_event.is_set():
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass

    def _cancel_after(self, attribute):
        after_id = getattr(self, attribute, None)
        setattr(self, attribute, None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass

    # ---------------- clipboard
    def _sensitive_clipboard_input_active(self):
        guard = getattr(self, "_clipboard_sensitive_input", None)
        return guard is not None and guard.is_set()

    def _discard_clipboard_events(self):
        """Discard credential-dialog copies without inspecting their text."""
        self._clipboard_pending_updates.clear()
        self._studio_clipboard_queue.clear()
        while True:
            try:
                self._clipboard_events.get_nowait()
            except queue.Empty:
                break

    def _begin_sensitive_clipboard_input(self):
        """Pause clipboard intake without changing the user's Auto-read setting."""
        self._clipboard_sensitive_depth += 1
        self._clipboard_sensitive_input.set()
        self._discard_clipboard_events()
        self._last_clip = None

    def _end_sensitive_clipboard_input(self):
        if self._clipboard_sensitive_depth <= 0:
            return
        self._clipboard_sensitive_depth -= 1
        if self._clipboard_sensitive_depth:
            return
        # Keep the closing sequence as a fence. Listener callbacks can arrive
        # after this dialog closes; the key remaining on the clipboard is also
        # excluded from the manual Read clipboard action until a new copy.
        sequence = self._clipboard_sequence_number()
        self._clipboard_sensitive_sequence = sequence
        self._clipboard_sensitive_baseline = True
        self._last_clip_sequence = sequence
        self._last_clip = None
        self._discard_clipboard_events()
        self._clipboard_sensitive_input.clear()

    def _clipboard_sequence_is_sensitive(self, sequence):
        if self._sensitive_clipboard_input_active():
            return True
        if not getattr(self, "_clipboard_sensitive_baseline", False):
            return False
        baseline = self._clipboard_sensitive_sequence
        # Fail closed if Windows cannot establish a clipboard boundary.
        return (sequence is None or baseline is None or
                not self._clipboard_sequence_is_newer(sequence, baseline))

    def _capture_clipboard_update(self, hwnd):
        sequence = self._clipboard_sequence_number()
        if self._clipboard_sequence_is_sensitive(sequence):
            return
        opened, text = self._read_windows_clipboard_text(hwnd)
        # The dialog may have opened while the native read was in progress.
        if not self._clipboard_sequence_is_sensitive(sequence):
            self._clipboard_events.put((sequence, text, opened))

    @staticmethod
    def _clipboard_sequence_number():
        if os.name != "nt":
            return None
        try:
            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception:
            return None

    def _prime_clipboard(self):
        self._last_clip_sequence = self._clipboard_sequence_number()
        try:
            self._last_clip = self.root.clipboard_get()
        except Exception:
            self._last_clip = None

    @staticmethod
    def _read_windows_clipboard_text(owner_hwnd):
        """Read Unicode clipboard text without touching Tk from the listener."""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        if not user32.OpenClipboard(owner_hwnd):
            return False, None
        try:
            cf_unicode_text = 13
            if not user32.IsClipboardFormatAvailable(cf_unicode_text):
                return True, None
            handle = user32.GetClipboardData(cf_unicode_text)
            if not handle:
                return False, None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return False, None
            try:
                char_count = kernel32.GlobalSize(handle) // ctypes.sizeof(
                    ctypes.c_wchar)
                text = ctypes.wstring_at(pointer, char_count)
                return True, text.split("\0", 1)[0]
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def _start_clipboard_listener(self):
        """Capture every Windows clipboard update in its native arrival order."""
        if os.name != "nt":
            self._clipboard_listener_failed = "Windows listener unavailable"
            return
        threading.Thread(target=self._clipboard_listener_loop,
                         daemon=True).start()

    def _clipboard_listener_loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        wm_clipboardupdate = 0x031D
        wm_close = 0x0010
        wm_destroy = 0x0002
        hwnd_message = wintypes.HWND(-3)
        lresult = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(
            lresult, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = ctypes.c_ushort
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, ctypes.c_void_p]
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = lresult
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.AddClipboardFormatListener.restype = wintypes.BOOL
        user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = lresult
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        class_name = f"PiperReaderClipboard_{os.getpid()}_{id(self)}"
        instance = kernel32.GetModuleHandleW(None)

        @wndproc_type
        def window_proc(hwnd, message, wparam, lparam):
            try:
                if message == wm_clipboardupdate:
                    self._capture_clipboard_update(hwnd)
                    return 0
                if message == wm_close:
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == wm_destroy:
                    user32.RemoveClipboardFormatListener(hwnd)
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                # The sequence-number fallback in _poll_clipboard retries reads.
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._clipboard_wndproc = window_proc
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        hwnd = None
        try:
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            hwnd = user32.CreateWindowExW(
                0, class_name, "Piper Reader clipboard listener", 0,
                0, 0, 0, 0, hwnd_message, None, instance, None)
            if not hwnd:
                raise ctypes.WinError()
            self._clipboard_listener_hwnd = hwnd
            self._clipboard_listener_thread_id = kernel32.GetCurrentThreadId()
            if not user32.AddClipboardFormatListener(hwnd):
                raise ctypes.WinError()
            # Capture any change made in the tiny gap between priming and
            # registering the listener. A later WM update with the same
            # sequence is discarded on the Tk thread.
            sequence = self._clipboard_sequence_number()
            if sequence != self._last_clip_sequence:
                self._capture_clipboard_update(hwnd)
            self._clipboard_listener_ready.set()

            message = wintypes.MSG()
            while not self._clipboard_listener_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._clipboard_listener_failed = str(exc) or exc.__class__.__name__
        finally:
            self._clipboard_listener_ready.clear()
            self._clipboard_listener_hwnd = None
            self._clipboard_listener_thread_id = None
            if hwnd:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            try:
                user32.UnregisterClassW(class_name, instance)
            except Exception:
                pass

    def _stop_clipboard_listener(self):
        self._clipboard_listener_stop.set()
        if os.name != "nt":
            return
        try:
            hwnd = self._clipboard_listener_hwnd
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            elif self._clipboard_listener_thread_id:
                ctypes.windll.user32.PostThreadMessageW(
                    self._clipboard_listener_thread_id, 0x0012, 0, 0)
        except Exception:
            pass

    @staticmethod
    def _clipboard_sequence_is_newer(sequence, previous):
        """Compare wrapping Windows DWORD clipboard sequence numbers."""
        if sequence is None or previous is None:
            return sequence != previous
        delta = (int(sequence) - int(previous)) & 0xFFFFFFFF
        return 0 < delta < 0x80000000

    def _accept_clipboard_update(self, sequence, clip):
        if self._clipboard_sequence_is_sensitive(sequence):
            return
        if sequence is not None:
            if not self._clipboard_sequence_is_newer(
                    sequence, self._last_clip_sequence):
                return
            self._last_clip_sequence = sequence
        elif clip == self._last_clip:
            return
        self._last_clip = clip
        if self.auto_var.get() and clip and clip.strip():
            self._queue_clipboard_text(clip)

    def _poll_clipboard(self):
        self._clipboard_after_id = None
        if self._shutdown_event.is_set():
            return
        if self._sensitive_clipboard_input_active():
            self._discard_clipboard_events()
            self._clipboard_after_id = self.root.after(75, self._poll_clipboard)
            return
        while True:
            try:
                update = self._clipboard_events.get_nowait()
            except queue.Empty:
                break
            self._clipboard_pending_updates.append(update)

        # Preserve the listener's exact FIFO. If another program briefly
        # locks the clipboard, keep that update at the head and retry it before
        # later updates instead of allowing them to overtake it.
        while self._clipboard_pending_updates:
            sequence, clip, opened = self._clipboard_pending_updates[0]
            if self._clipboard_sequence_is_sensitive(sequence):
                self._clipboard_pending_updates.popleft()
                continue
            if not opened:
                current_sequence = self._clipboard_sequence_number()
                if (sequence is not None and current_sequence is not None and
                        self._clipboard_sequence_is_newer(
                            current_sequence, sequence)):
                    # Windows no longer retains this older clipboard value.
                    self._clipboard_pending_updates.popleft()
                    continue
                try:
                    clip = self.root.clipboard_get()
                    opened = True
                except Exception:
                    break
            self._clipboard_pending_updates.popleft()
            self._accept_clipboard_update(sequence, clip)

        # Fall back to sequence polling only if native notifications are not
        # available. Mixing the two active paths could reorder updates.
        if not self._clipboard_listener_ready.is_set():
            sequence = self._clipboard_sequence_number()
            try:
                clip = (None if self._clipboard_sequence_is_sensitive(sequence)
                        else self.root.clipboard_get())
            except Exception:
                clip = None
            if clip is not None:
                self._accept_clipboard_update(sequence, clip)
        self._flush_studio_clipboard_queue()
        self._probe_runtime_reprime()
        try:
            self._clipboard_after_id = self.root.after(
                75, self._poll_clipboard)
        except Exception:
            self._clipboard_after_id = None

    # ---------------- actions
    def _queue_speech(self, kind, payload, replace=True, voice_label=None,
                      use_repeat=True):
        if self._shutdown_event.is_set():
            return
        if replace:
            self.stop(silent=True, cancel_exports=False)
        if voice_label is None:
            voice_label = self.voice_var.get()
        engine_key, voice_id, lang = self._voice_snapshot(voice_label)
        speed = normalize_speed(self.speed_var.get())
        volume = float(self.vol_var.get())
        repeat_mode, repeat_count = (
            self._repeat_snapshot() if use_repeat else ("off", 1))
        cancel_event = threading.Event()
        pause_event = self._pause_requested
        with self._playback_lock:
            self._speech_requests.add(cancel_event)
            if kind == "clipboard":
                if not hasattr(self, "_clipboard_enqueued_at"):
                    self._clipboard_enqueued_at = {}
                self._clipboard_enqueued_at[cancel_event] = time.monotonic()
            if self._active_cancel is None:
                # Covers the short enqueue-to-worker gap for Pause and Stop.
                self._active_cancel = cancel_event
                self._active_pause = pause_event
                self._active_request_phase = "preparing"
                self._active_request_message = "Queued for reading..."
        self.speak_q.put((kind, payload, cancel_event, pause_event,
                          engine_key, voice_id, lang, speed, volume,
                          repeat_mode, repeat_count))
        self._set_playback_state("paused" if pause_event.is_set() else "playing")
        return cancel_event

    def _queue_clipboard_text(self, text):
        """Keep copies in order while a Studio lesson owns the speakers."""
        if self._sensitive_clipboard_input_active():
            return
        text = text.strip()[:100000]
        if not text or self._shutdown_event.is_set():
            return
        self._studio_clipboard_queue.append(text)
        self._flush_studio_clipboard_queue()
        if self._studio_clipboard_queue:
            count = len(self._studio_clipboard_queue)
            noun = "item" if count == 1 else "items"
            self.set_status(
                f"{count} clipboard {noun} waiting for AI Studio playback to finish.")

    def _flush_studio_clipboard_queue(self):
        """Resume queued copies after playback finishes, stops or fails."""
        if (self._shutdown_event.is_set() or not self.auto_var.get()
                or self._sensitive_clipboard_input_active()):
            self._studio_clipboard_queue.clear()
            return
        if self._ai_studio_owns_audio():
            return
        while self._studio_clipboard_queue:
            self._speak_clipboard_text(self._studio_clipboard_queue.popleft())

    def _speak_clipboard_text(self, text):
        """Append to reader speech without stopping independent Studio jobs."""
        if self._sensitive_clipboard_input_active():
            return
        self._queue_speech(
            "clipboard", text, replace=False,
            voice_label=self.clipboard_voice_var.get())
        with self._playback_lock:
            active = self._active_cancel
            message = getattr(self, "_active_request_message", "")
        if active is not None:
            self._set_request_status(active, message or "Queued for reading...")

    def speak(self, text, use_repeat=True):
        if self._shutdown_event.is_set():
            return
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.speak, text, use_repeat)
            return
        text = text.strip()[:100000]
        if not text:
            return
        self._queue_speech(
            "text", text, replace=True, use_repeat=use_repeat)

    def speak_doc(self, start=0):
        if not (self.viewer and self.viewer.chunks):
            return
        viewer = self.viewer
        repeat_groups = getattr(viewer, "repeat_groups", [])
        payload = []
        for i, chunk in enumerate(viewer.chunks):
            groups = (repeat_groups[i] if i < len(repeat_groups) else {})
            payload.append((chunk[0], {
                "viewer": viewer,
                "chunk_index": i,
                "sentence_id": groups.get("sentence_id", i),
                "paragraph_id": groups.get("paragraph_id", i),
            }))
        self._contents_browsing_viewer = None
        self._document_read_viewer = viewer
        self._document_read_cancel = self._queue_speech("doc", payload[start:], replace=True)

    def _doc_click(self, idx):
        self.speak_doc(start=idx)

    def _safe_focus(self, target):
        try:
            if isinstance(target, dict) and "viewer" in target:
                expected_viewer = target["viewer"]
                idx = target["chunk_index"]
            elif isinstance(target, tuple) and len(target) == 2:
                expected_viewer, idx = target
            else:  # compatibility with speech requests queued by older patches
                expected_viewer, idx = self.viewer, target
            if self.viewer is expected_viewer and expected_viewer:
                if (getattr(self, "_contents_browsing_viewer", None) is expected_viewer
                        and self._pause_requested.is_set()):
                    return
                expected_viewer.focus_chunk(idx)
        except Exception:
            pass

    def _begin_content_load(self):
        self._content_load_serial += 1
        return self._content_load_serial

    def _content_load_is_current(self, serial):
        return (serial == self._content_load_serial and
                not self._shutdown_event.is_set())

    def _load_status(self, serial, message):
        if self._shutdown_event.is_set():
            return
        try:
            self.root.after(0, self._apply_load_status, serial, message)
        except Exception:
            pass

    def _apply_load_status(self, serial, message):
        if self._content_load_is_current(serial):
            self._apply_status(message)

    def _finish_load_error(self, serial, message):
        if self._content_load_is_current(serial):
            self._apply_status(f"Error: {message}")

    def _on_ocr_language_changed(self, *_):
        self._save_settings()

    def _ocr_profile_snapshot(self):
        _, _, voice_language = self._voice_snapshot(self.voice_var.get())
        return resolve_ocr_profile(
            self.ocr_language_var.get(), voice_language)

    def _wait_for_ocr_slot(self, serial, _page_number, _ordinal, _total):
        """Keep OCR from competing with Heart or queued clipboard speech."""
        waiting_status_sent = False
        while self._content_load_is_current(serial):
            with self._playback_lock:
                busy = self._synthesis_busy_locked()
            if not busy:
                return True
            if not waiting_status_sent:
                self._load_status(
                    serial, "OCR paused while speech is active; it will resume "
                    "automatically...")
                waiting_status_sent = True
            if self._shutdown_event.wait(0.1):
                return False
        return False

    def _confirm_large_auto_ocr(self, serial, scanned_pages, total_pages):
        if scanned_pages <= OCR_AUTO_CONFIRM_PAGES:
            return True
        completed = threading.Event()
        answer = []

        def ask_on_ui_thread():
            try:
                if self._content_load_is_current(serial):
                    answer.append(messagebox.askyesno(
                        "Recognize scanned document?",
                        f"Piper Reader found {scanned_pages} scanned pages out "
                        f"of {total_pages}. Local OCR may take several "
                        "minutes. You can stop between pages.\n\nContinue?",
                        parent=self.root))
            finally:
                completed.set()

        try:
            self.root.after(0, ask_on_ui_thread)
        except Exception:
            return False
        while not completed.wait(0.1):
            if not self._content_load_is_current(serial):
                return False
        accepted = bool(answer and answer[0])
        if not accepted and self._content_load_is_current(serial):
            self._load_status(serial, "OCR canceled.")
        return accepted

    def _recognize_pdf_for_load(self, pdf_path, serial, profile, force=False):
        result = recognize_pdf_pages(
            pdf_path, profile["language"], profile["label"],
            lambda message: self._load_status(serial, message),
            is_current=lambda: self._content_load_is_current(serial),
            before_page=lambda page, ordinal, total: self._wait_for_ocr_slot(
                serial, page, ordinal, total),
            confirm_pages=(None if force else
                           lambda scanned, total: self._confirm_large_auto_ocr(
                               serial, scanned, total)),
            force=force)
        result["warning"] = profile.get("warning", "")
        return result

    def _start_viewer_ocr(self):
        viewer = self.viewer
        if not isinstance(viewer, PdfViewer):
            return
        page_count = len(viewer.doc)
        if page_count > 20 and not messagebox.askyesno(
                "Recognize the whole document?",
                f"This will run local OCR on all {page_count} pages and may "
                "take several minutes. You can stop between pages.\n\n"
                "Continue?", parent=self.root):
            return
        pdf_path, name = viewer.path, viewer.title
        spoken_text = getattr(viewer, "spoken_text", None)
        review_summary = getattr(viewer, "review_summary", None)
        library_item_id = getattr(viewer, "library_item_id", None)
        profile = self._ocr_profile_snapshot()
        # PyMuPDF explicitly does not support rendering one document while
        # another thread performs OCR, so remove the viewer before the worker.
        self._close_viewer(cancel_load=False)
        serial = self._begin_content_load()
        self._load_status(
            serial, f"Preparing {profile['label']} OCR for {name}...")

        def job():
            try:
                with self._ocr_load_lock:
                    if not self._content_load_is_current(serial):
                        return
                    result = self._recognize_pdf_for_load(
                        pdf_path, serial, profile, force=True)
                if self._content_load_is_current(serial):
                    self.root.after(
                        0, self._open_viewer, pdf_path, name, serial, result,
                        spoken_text, review_summary, library_item_id)
            except OcrCancelled:
                return
            except Exception as exc:
                try:
                    self.root.after(
                        0, self._finish_load_error, serial, str(exc))
                except Exception:
                    pass

        threading.Thread(target=job, daemon=True).start()

    def _open_viewer(self, pdf_path, name, serial=None, ocr_result=None,
                     spoken_text=None, review_summary=None,
                     library_item_id=None, library_warning=None):
        if serial is not None and not self._content_load_is_current(serial):
            return
        try:
            self._close_library()
            self._close_viewer(cancel_load=False)
            engine, _, _ = self.current_engine()
            result = ocr_result or {}
            v = PdfViewer(
                self.editor_host, pdf_path, name, self._close_viewer,
                click_cb=self._doc_click, ocr_cb=self._start_viewer_ocr,
                ocr_language_var=self.ocr_language_var,
                ocr_language_changed_cb=self._on_ocr_language_changed,
                ocr_result=result, spoken_text=spoken_text,
                review_summary=review_summary)
            v.library_item_id = library_item_id
            self.viewer = v
            self.current_library_item_id = library_item_id
            self.current_text_name = name
            self.editor_view.grid_remove()
            v.grid(row=0, column=0, sticky="nsew")
            self.zoom_var.set("100%")
            chunks = v.build_chunks(
                engine.chunk_len, result.get("words_by_page"))
            if not chunks:
                if result.get("attempted_pages"):
                    self.set_status(
                        "Error: OCR found no readable text. Try a clearer or "
                        "higher-contrast scan, or choose another OCR language.")
                else:
                    self.set_status(
                        f"{name}: no readable text. Choose Recognize text to "
                        "run OCR.")
                return
            attempted = int(result.get("attempted_pages") or 0)
            recognized = int(result.get("recognized_pages") or 0)
            displayed_words = sum(
                len(chunk[0].split()) for chunk in chunks)
            if attempted:
                base_status = (
                    f"OCR complete - {displayed_words:,} words; text found on "
                    f"{recognized}/{attempted} scanned pages.")
                warning = str(result.get("warning") or "").strip()
                if warning:
                    base_status += f" {warning}"
            else:
                base_status = f"Loaded {name} ({len(v.doc)} pages)."
            if v.review_summary.get("has_markup"):
                base_status += (
                    " Tracked changes are shown; reading current text only.")
            if library_warning:
                base_status += f" Library copy was not saved: {library_warning}."
            elif library_item_id:
                base_status += " Available in Library."
            with self._playback_lock:
                speech_busy = self._synthesis_busy_locked()
            if speech_busy:
                self.set_status(
                    base_status + " Current speech continues; press Play when "
                    "you are ready.")
            else:
                self.set_status(base_status + " Reading...")
                self.speak_doc()
        except Exception as e:
            self.set_status(f"Error: {e}")

    def _open_web_viewer(self, article, serial):
        if not self._content_load_is_current(serial):
            return
        try:
            self._close_library()
            self._close_viewer(cancel_load=False)
            engine, _, _ = self.current_engine()
            viewer = WebArticleViewer(
                self.editor_host, article, self._close_viewer,
                click_cb=self._doc_click)
            self.viewer = viewer
            self.current_library_item_id = None
            self.current_text_name = article.get("title")
            self.editor_view.grid_remove()
            viewer.grid(row=0, column=0, sticky="nsew")
            self.zoom_var.set("100%")
            chunks = viewer.build_chunks(engine.chunk_len)
            if not chunks:
                raise RuntimeError("no readable text was found on that page")
            suffix = " (Reader View shortened)" if article.get("truncated") else ""
            self.set_status(
                f"Loaded {article['title']} ({article['word_count']:,} words)"
                f"{suffix}. Reading...")
            self.speak_doc()
        except Exception as exc:
            self.set_status(f"Error: {exc}")

    def _show_text_document(self, text, name, serial, library_item_id=None,
                            library_warning=None):
        if not self._content_load_is_current(serial):
            return
        self._close_library()
        self._close_viewer(cancel_load=False)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.current_library_item_id = library_item_id
        self.current_text_name = name
        status = f"Loaded {name} ({len(text.split()):,} words)."
        if library_warning:
            status += f" Library copy was not saved: {library_warning}."
        elif library_item_id:
            status += " Available in Library."
        self.set_status(status + " Reading...")
        self.speak(text)

    def _close_viewer(self, cancel_load=True):
        self._hide_contents()
        if cancel_load:
            self._content_load_serial += 1
        if self.viewer:
            self.stop(silent=True, cancel_exports=False)
            try:
                self.viewer.close()
            except Exception:
                pass
            self.viewer = None
            self._document_read_viewer = None
            self._document_read_cancel = None
            self._contents_browsing_viewer = None
            self.current_library_item_id = None
            self.current_text_name = None
            self.editor_view.grid()
            self.zoom_var.set(f"{self._text_zoom_percent()}%")

    def preview_voice(self):
        label = self.voice_var.get()
        eng, _, lang = VOICES.get(label, VOICES[DEFAULT_VOICE])
        localized_preview = VOICE_PREVIEW_TEXT.get(lang)
        if localized_preview:
            self.speak(localized_preview, use_repeat=False)
            return
        name = label.split(" (")[0]
        kind = {"kokoro": "Kokoro", "piper": "Piper",
                "edge": "Microsoft online"}.get(eng, "built-in Windows")
        self.speak(f"Hi! This is {name}, a free {kind} voice. "
                   "I sound like this when I read your text out loud.",
                   use_repeat=False)

    def open_webpage(self):
        initial = ""
        try:
            clip = self.root.clipboard_get().strip()
            if re.match(r"^(?:https?://|www\.)\S+$", clip, re.I):
                initial = clip[:2000]
        except Exception:
            pass
        value = simpledialog.askstring(
            "Open webpage",
            "Paste the address of a public webpage to open in Reader View:",
            parent=self.root, initialvalue=initial)
        if value is None:
            return
        try:
            url = normalize_web_url(value)
        except ValueError as exc:
            messagebox.showerror("Cannot open webpage", str(exc),
                                 parent=self.root)
            return
        self._close_library()
        serial = self._begin_content_load()
        self._load_status(serial, "Loading webpage...")

        def job():
            try:
                with self._web_load_lock:
                    if not self._content_load_is_current(serial):
                        return
                    article = load_webpage(
                        url, lambda message: self._load_status(
                            serial, message))
                if self._content_load_is_current(serial):
                    self.root.after(
                        0, self._open_web_viewer, article, serial)
            except Exception as exc:
                try:
                    self.root.after(
                        0, self._finish_load_error, serial, str(exc))
                except Exception:
                    pass

        threading.Thread(target=job, daemon=True).start()

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open a document or image to read aloud",
            filetypes=[("Documents and images",
                        "*.pdf *.docx *.txt *.md *.png *.jpg *.jpeg *.bmp "
                        "*.tif *.tiff *.webp"),
                       ("PDF files", "*.pdf"),
                       ("Word documents", "*.docx *.doc"),
                       ("Images for OCR",
                        "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("Text files", "*.txt *.md"),
                       ("All files", "*.*")])
        if not path:
            return
        self._open_file_path(path)

    def _remember_imported_file(self, path, name, word_count=0):
        if self.library_store is None:
            return None, (self.library_error or "Library is unavailable")
        try:
            item = self.library_store.add_file(
                path, title=name, word_count=word_count)
            return item["id"], None
        except LibraryError as exc:
            return None, str(exc)

    @staticmethod
    def _ocr_result_word_count(result):
        pages = (result or {}).get("words_by_page") or {}
        if isinstance(pages, dict):
            return sum(len(words or ()) for words in pages.values())
        if isinstance(pages, (list, tuple)):
            return sum(len(words or ()) for words in pages)
        return 0

    def _open_file_path(self, path, remember=True, library_item_id=None,
                        display_name=None):
        path = os.path.abspath(path)
        name = display_name or os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        self._close_library()
        if self.viewer:
            # Prevent unsupported concurrent PyMuPDF rendering and OCR.
            self._close_viewer(cancel_load=False)
        serial = self._begin_content_load()
        profile = self._ocr_profile_snapshot()

        def job():
            word_text = None
            review_summary = None
            try:
                self._load_status(serial, f"Loading {name}...")
                if ext in ({".pdf", ".docx"} | OCR_IMAGE_EXTENSIONS):
                    if not (module_ok("fitz") or pip_install(
                            "pymupdf", lambda message: self._load_status(
                                serial, message))):
                        raise RuntimeError(
                            "the PDF/OCR reader is not installed")
                    p = path
                    conversion_failed = False
                    with self._ocr_load_lock:
                        if not self._content_load_is_current(serial):
                            return
                        if ext == ".docx":
                            try:
                                self._load_status(
                                    serial,
                                    "Preparing Word view with tracked changes...")
                                word_text, review_summary = (
                                    _docx_review_content(path))
                                p = docx_to_pdf(path, self.tmpdir)
                            except Exception:
                                conversion_failed = True
                                p = None
                        elif ext in OCR_IMAGE_EXTENSIONS:
                            self._load_status(
                                serial, "Preparing image for local OCR...")
                            p = image_to_pdf(
                                path, os.path.join(
                                    self.tmpdir, f"image-{serial}.pdf"))
                        if p:
                            result = self._recognize_pdf_for_load(
                                p, serial, profile, force=False)
                    if p:
                        stored_id = library_item_id
                        library_warning = None
                        if remember:
                            word_count = (len(word_text.split()) if word_text
                                          else self._ocr_result_word_count(
                                              result))
                            stored_id, library_warning = (
                                self._remember_imported_file(
                                    path, name, word_count))
                        if self._content_load_is_current(serial):
                            self.root.after(
                                0, self._open_viewer, p, name, serial, result,
                                (word_text if review_summary and
                                 review_summary.get("has_markup") else None),
                                review_summary, stored_id, library_warning)
                        return
                    if not conversion_failed:
                        raise RuntimeError("could not prepare that document")
                # fallback: plain text mode
                text = (word_text if ext == ".docx" and word_text else
                        extract_document(
                            path, lambda message: self._load_status(
                                serial, message)))
                stored_id = library_item_id
                library_warning = None
                if remember:
                    stored_id, library_warning = self._remember_imported_file(
                        path, name, len(text.split()))
                self.root.after(
                    0, self._show_text_document, text, name, serial,
                    stored_id, library_warning)
            except OcrCancelled:
                return
            except Exception as e:
                try:
                    self.root.after(
                        0, self._finish_load_error, serial, str(e))
                except Exception:
                    pass
        threading.Thread(target=job, daemon=True).start()

    def _text_click_read(self, event):
        """Double-click in the text box: read from that sentence."""
        try:
            idx = self.text.index(f"@{event.x},{event.y}")
            offset = len(self.text.get("1.0", idx))
        except Exception:
            return None
        full = self.text.get("1.0", "end")
        best = 0
        for pat in (
                ". ", "! ", "? ", "؟ ", "؛ ", "۔ ",
                ".\n", "!\n", "?\n", "؟\n", "؛\n", "۔\n", "\n"):
            p = full.rfind(pat, 0, offset)
            if p >= 0:
                best = max(best, p + len(pat))
        self.speak(full[best:])
        return "break"

    def read_box(self):
        if self.viewer:
            self.speak_doc()
        else:
            self.speak(self.text.get("1.0", "end"))

    def read_clipboard(self):
        if self._clipboard_sequence_is_sensitive(self._clipboard_sequence_number()):
            self.set_status("Clipboard reading is protected while entering a key. Copy new text after closing the key dialog.")
            return
        try:
            text = self.root.clipboard_get().strip()[:100000]
            if not text:
                raise ValueError("Clipboard has no text")
            # This is a manual reading action, so it follows Reading voice.
            # Only automatic clipboard monitoring uses Clipboard voice.
            self.speak(text)
        except Exception:
            self.set_status("Clipboard has no text.")

    def stop(self, silent=False, browser_reason="requested", cancel_exports=True):
        # Explicit Stop and shutdown cancel online exports. Replacing playback
        # (for example Preview voice) leaves independent WAV exports running.
        # Also invalidate a webpage/document fetch that has not reached the UI
        # yet, so Stop cannot be followed by a late auto-starting page.
        for name in ("ai_studio", "textbook_studio"):
            studio = getattr(self, name, None)
            if studio is not None:
                studio.stop()
        self._studio_clipboard_queue.clear()
        self._content_load_serial += 1
        browser_session = self._browser_active_session
        self._browser_active_session = None
        self._browser_request_cancel = None
        bridge = self._browser_bridge
        if bridge is not None and browser_session:
            bridge.publish(
                browser_session, "stopped", reason=browser_reason)
            bridge.update_state(active_session=None, playback_state="idle")
        with self._playback_lock:
            requests = list(self._speech_requests)
            exports = (list(getattr(self, "_online_export_cancels", ()))
                       if cancel_exports else [])
            queued_count = max(0, len(requests) -
                               (1 if self._active_cancel is not None else 0))
            self._speech_requests.clear()
            getattr(self, "_clipboard_enqueued_at", {}).clear()
            self._active_cancel = None
            self._active_pause = None
            self._active_request_phase = "preparing"
            self._active_request_message = ""
        for cancel_event in requests + exports:
            cancel_event.set()
        try:
            # Close MCI before clearing a shared Pause request so Stop cannot
            # briefly resume a paused clip on its way out.
            self.player.stop()
        except Exception:
            pass
        self._pause_requested.clear()
        with self.speak_q.mutex:
            for request in list(self.speak_q.queue):
                if request is not None and len(request) >= 4:
                    request[2].set()
            self.speak_q.queue.clear()
        self._set_playback_state("idle")
        if not silent:
            if queued_count:
                noun = "item" if queued_count == 1 else "items"
                self.set_status(
                    f"Stopped. Cleared {queued_count} queued {noun}.")
            else:
                self.set_status("Stopped.")

    def _synthesize_text_to_wav(self, engine, vid, lang, speed, vol,
                                text, path, cancel_event=None):
        """Synthesize bounded chunks and join them into one valid WAV."""
        chunks = split_chunks(text, engine.chunk_len)
        if not chunks:
            raise RuntimeError("there is no text to export")
        target_dir = os.path.dirname(os.path.abspath(path))
        output_fd, temp_output = tempfile.mkstemp(
            prefix=".piper_reader_", suffix=".wav", dir=target_dir)
        os.close(output_fd)
        writer = None
        part_path = None
        expected = None

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise SpeechCancelled("Export stopped")

        try:
            for number, chunk in enumerate(chunks, 1):
                check_cancelled()
                online = getattr(engine, "supports_cancellation", False)
                notice = " Microsoft online - text sent to Microsoft." if online else ""
                self.set_status(
                    f"Generating WAV file... ({number}/{len(chunks)}){notice}")
                part_fd, part_path = tempfile.mkstemp(
                    prefix="web_export_", suffix=".wav", dir=self.tmpdir)
                os.close(part_fd)
                options = {"cancel_event": cancel_event} if online else {}
                engine.synth_to_wav(
                    chunk, vid, lang, speed, vol, part_path, **options)
                check_cancelled()
                with wave.open(part_path, "rb") as source:
                    signature = (
                        source.getnchannels(), source.getsampwidth(),
                        source.getframerate(), source.getcomptype())
                    if expected is None:
                        expected = signature
                        writer = wave.open(temp_output, "wb")
                        writer.setnchannels(signature[0])
                        writer.setsampwidth(signature[1])
                        writer.setframerate(signature[2])
                        writer.setcomptype(
                            source.getcomptype(), source.getcompname())
                    elif signature != expected:
                        raise RuntimeError(
                            "voice engine returned incompatible audio chunks")
                    writer.writeframes(source.readframes(source.getnframes()))
                os.remove(part_path)
                part_path = None
            if writer is None:
                raise RuntimeError("voice engine did not create audio")
            writer.close()
            writer = None
            check_cancelled()
            os.replace(temp_output, path)
            temp_output = None
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            for candidate in (part_path, temp_output):
                if candidate:
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass

    def save_wav(self):
        text = (self.viewer.text() if self.viewer
                else self.text.get("1.0", "end")).strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(defaultextension=".wav",
                                            filetypes=[("WAV audio", "*.wav")])
        if not path:
            return
        engine, vid, lang = self.current_engine()
        speed, vol = self.speed_var.get(), self.vol_var.get()
        cancel_event = (threading.Event()
                        if getattr(engine, "supports_cancellation", False) else None)
        with self._playback_lock:
            self._save_wav_jobs += 1
            if cancel_event is not None:
                self._online_export_cancels.add(cancel_event)

        def job():
            try:
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not engine.ready() and not engine.install():
                    return
                self._synthesize_text_to_wav(
                    engine, vid, lang, speed, vol, text, path, cancel_event)
                if cancel_event is None or not cancel_event.is_set():
                    self.set_status(f"Saved: {path}")
            except SpeechCancelled:
                pass
            except Exception as e:
                if cancel_event is None or not cancel_event.is_set():
                    self.set_status(f"Error: {e}")
            finally:
                with self._playback_lock:
                    self._save_wav_jobs = max(0, self._save_wav_jobs - 1)
                    self._online_export_cancels.discard(cancel_event)
                try:
                    self.root.after(0, self._try_runtime_reprime)
                except Exception:
                    pass
        try:
            threading.Thread(target=job, daemon=True).start()
        except Exception:
            with self._playback_lock:
                self._save_wav_jobs = max(0, self._save_wav_jobs - 1)
                self._online_export_cancels.discard(cancel_event)
            raise

    # ---------------- worker
    @staticmethod
    def _wait_for_resume(cancel_event, pause_event):
        while pause_event.is_set():
            if cancel_event.wait(0.05):
                return False
        return not cancel_event.is_set()

    @staticmethod
    def _iter_synthesized_chunks(engine, prepared, voice_id, lang,
                                 speed, volume, cancel_event):
        """Yield WAVs in order, preparing one natural-voice block ahead."""
        def render(item):
            chunk, wav_path = item[0], item[-1]
            options = ({"cancel_event": cancel_event}
                       if getattr(engine, "supports_cancellation", False) else {})
            engine.synth_to_wav(
                chunk, voice_id, lang, speed, volume, wav_path, **options)
            return item

        lookahead = (engine.name == "Kokoro" or
                     getattr(engine, "lookahead", False))
        if not lookahead or len(prepared) < 2:
            for item in prepared:
                if cancel_event.is_set():
                    break
                yield render(item)
            return

        ready = queue.Queue(maxsize=1)
        slot = threading.Semaphore(1)
        halt = threading.Event()
        done = threading.Event()

        def produce():
            try:
                for item in prepared:
                    while not (halt.is_set() or cancel_event.is_set()):
                        if slot.acquire(timeout=0.05):
                            break
                    else:
                        return
                    if halt.is_set() or cancel_event.is_set():
                        slot.release()
                        return
                    try:
                        rendered = render(item)
                        result = (True, rendered)
                    except Exception as exc:
                        result = (False, exc)
                    if halt.is_set() or cancel_event.is_set():
                        slot.release()
                        return
                    ready.put_nowait(result)
                    if not result[0]:
                        return
            finally:
                done.set()

        producer = threading.Thread(
            target=produce, name="PiperReader-SpeechLookahead", daemon=True)
        producer.start()
        try:
            for _index in range(len(prepared)):
                while not cancel_event.is_set():
                    try:
                        ok, value = ready.get(timeout=0.05)
                        slot.release()
                        break
                    except queue.Empty:
                        if done.is_set():
                            raise RuntimeError(
                                "Voice lookahead stopped unexpectedly")
                else:
                    break
                if not ok:
                    raise value
                yield value
        finally:
            halt.set()
            producer.join()

    def _worker(self):
        while True:
            request = self.speak_q.get()
            if request is None or self._shutdown_event.is_set():
                return
            (kind, payload, cancel_event, pause_event,
             engine_key, vid, lang, speed, volume,
             repeat_mode, repeat_count) = request
            repeat_mode = normalize_repeat_mode(repeat_mode)
            repeat_count = (1 if repeat_mode == "off" else
                            normalize_repeat_count(repeat_count))
            with self._playback_lock:
                tracked = cancel_event in self._speech_requests
                if tracked and not cancel_event.is_set():
                    self._active_cancel = cancel_event
                    self._active_pause = pause_event
                    self._active_request_phase = "preparing"
                    self._active_request_message = "Preparing voice..."
            if not tracked or cancel_event.is_set():
                continue
            self._set_request_status(
                cancel_event, "Preparing voice...", phase="preparing")
            completed = False
            browser_session = None
            terminal_error = None
            request_dir = None
            request_started = time.monotonic()
            queue_wait = max(0.0, request_started - getattr(
                self, "_clipboard_enqueued_at", {}).get(cancel_event, request_started))
            chunks_played = 0
            failure_reason = None
            failure_http_status = None
            engine = None
            if kind == "browser" and payload:
                first_meta = payload[0][1]
                if isinstance(first_meta, dict):
                    browser_session = first_meta.get("browser_session")
            try:
                engine = self._engine_for_speech_request(kind, engine_key, cancel_event)
                if not engine.ready():
                    self.set_status(f"{engine.name} engine not installed yet.")
                    if not engine.install():
                        raise RuntimeError(
                            f"{engine.name} voice engine is unavailable")
                units = build_repeat_units(
                    kind, payload, repeat_mode, engine.chunk_len,
                    first_max_len=getattr(
                        engine, "initial_chunk_len", None))
                total_units = len(units)
                total_atoms = sum(len(unit) for unit in units)
                request_dir = tempfile.mkdtemp(
                    prefix="speech_", dir=self.tmpdir)
                prepared = []
                for unit_index, unit in enumerate(units):
                    for part_index, (chunk, meta) in enumerate(unit):
                        wav = os.path.join(
                            request_dir, f"chunk-{len(prepared):05d}.wav")
                        prepared.append((
                            chunk, meta, unit_index, total_units,
                            part_index == len(unit) - 1, wav))
                if engine_key == "kokoro" and prepared:
                    self._set_request_status(cancel_event, "Preparing natural voice...", phase="preparing")
                elif engine_key == "edge" and prepared:
                    message = (
                        "Preparing Lessac offline while Microsoft recovers..."
                        if getattr(engine, "active_engine_key", None) == "piper"
                        else "Preparing Microsoft online voice: text is sent to Microsoft...")
                    self._set_request_status(cancel_event, message, phase="preparing")
                if not self._wait_for_resume(cancel_event, pause_event):
                    prepared = []
                synthesized = self._iter_synthesized_chunks(
                    engine, prepared, vid, lang, speed, volume,
                    cancel_event)

                def play_item(item, repetition, atom_number):
                    nonlocal chunks_played
                    (chunk, meta, unit_index, unit_total,
                     _unit_end, wav) = item
                    if not self._wait_for_resume(cancel_event, pause_event):
                        return False
                    if repeat_count > 1:
                        if repeat_mode == "passage":
                            status = (f"Passage repeat {repetition}/"
                                      f"{repeat_count}")
                        else:
                            unit_name = REPEAT_MODE_LABELS[repeat_mode]
                            status = (
                                f"{unit_name} {unit_index + 1}/{unit_total} "
                                f"- repeat {repetition}/{repeat_count}")
                    else:
                        status = (
                            f"Speaking... ({atom_number}/{total_atoms})"
                            if total_atoms > 1 else "Speaking...")
                    if engine_key == "edge":
                        status += (" Lessac offline backup active."
                                   if getattr(engine, "active_engine_key", None) == "piper"
                                   else " Microsoft online - text sent to Microsoft.")
                    with self._playback_lock:
                        self._audio_playing_cancel = cancel_event
                        self._active_audio_message = status
                    self._set_request_status(cancel_event, status, phase="playing")
                    # Highlight immediately before each original or repeated
                    # WAV begins. Passage repeats therefore visibly return to
                    # the first sentence instead of leaving the last one lit.
                    if (meta is not None and
                            not self._shutdown_event.is_set()):
                        if kind == "browser" and isinstance(meta, dict):
                            bridge = self._browser_bridge
                            if bridge is not None:
                                bridge.publish(
                                    meta["browser_session"], "highlight",
                                    chunk_id=meta["chunk_id"],
                                    index=meta["index"],
                                    total=meta["total"],
                                    repeat_mode=repeat_mode,
                                    repeat_index=repetition,
                                    repeat_count=repeat_count,
                                    repeat_unit=unit_index + 1,
                                    repeat_units=unit_total,
                                )
                        else:
                            self.root.after(0, self._safe_focus, meta)
                    try:
                        self.player.play_blocking(wav, cancel_event, pause_event)
                    finally:
                        with self._playback_lock:
                            if getattr(self, "_audio_playing_cancel", None) is cancel_event:
                                self._audio_playing_cancel = None
                    if not cancel_event.is_set():
                        chunks_played += 1
                    return not cancel_event.is_set()

                try:
                    unit_cache = []
                    unit_cache_bytes = 0
                    unit_uncached = False
                    atom_number = 0
                    keep_playing = True
                    for rendered in synthesized:
                        atom_number += 1
                        unit_cache.append(rendered)
                        if not play_item(rendered, 1, atom_number):
                            keep_playing = False
                            break
                        if not unit_uncached:
                            try:
                                unit_cache_bytes += os.path.getsize(
                                    rendered[-1])
                            except OSError:
                                pass
                            if (unit_cache_bytes >
                                    REPEAT_WAV_CACHE_MAX_BYTES):
                                unit_uncached = True
                                for cached in unit_cache:
                                    try:
                                        os.remove(cached[-1])
                                    except OSError:
                                        pass
                        else:
                            try:
                                os.remove(rendered[-1])
                            except OSError:
                                pass
                        if rendered[4]:
                            if unit_uncached:
                                # Re-render very large units one repetition at
                                # a time. This keeps disk bounded while the
                                # standard Kokoro lookahead still hides most
                                # synthesis inside the preceding audio.
                                for repetition in range(2, repeat_count + 1):
                                    replayed = self._iter_synthesized_chunks(
                                        engine, unit_cache, vid, lang, speed,
                                        volume, cancel_event)
                                    try:
                                        for cached in replayed:
                                            if not play_item(
                                                    cached, repetition,
                                                    atom_number):
                                                keep_playing = False
                                                break
                                            try:
                                                os.remove(cached[-1])
                                            except OSError:
                                                pass
                                    finally:
                                        replayed.close()
                                    if not keep_playing:
                                        break
                            else:
                                for repetition in range(2, repeat_count + 1):
                                    for cached in unit_cache:
                                        if not play_item(
                                                cached, repetition,
                                                atom_number):
                                            keep_playing = False
                                            break
                                    if not keep_playing:
                                        break
                            for cached in unit_cache:
                                try:
                                    os.remove(cached[-1])
                                except OSError:
                                    pass
                            unit_cache = []
                            unit_cache_bytes = 0
                            unit_uncached = False
                            if not keep_playing:
                                break
                finally:
                    synthesized.close()
                completed = not cancel_event.is_set()
            except Exception as e:
                terminal_error = str(e)
                failure_reason = getattr(e, "reason", "unknown")
                failure_http_status = getattr(e, "http_status", None)
                if not cancel_event.is_set():
                    self._set_request_status(cancel_event, f"Error: {e}")
            finally:
                if request_dir:
                    shutil.rmtree(request_dir, ignore_errors=True)
                diagnostics = getattr(self, "_speech_diagnostics", None)
                if kind == "clipboard" and diagnostics is not None:
                    try:
                        diagnostics.record(
                            outcome=("cancelled" if cancel_event.is_set() else
                                     "completed" if completed else "failed"),
                            reason=failure_reason or getattr(engine, "fallback_reason", None),
                            http_status=(failure_http_status if failure_http_status is not None
                                         else getattr(engine, "fallback_http_status", None)),
                            elapsed_seconds=time.monotonic() - request_started,
                            queue_wait_seconds=queue_wait,
                            voice_engine=getattr(engine, "active_engine_key", engine_key),
                            chunks_played=chunks_played)
                    except Exception:
                        # Optional diagnostics must never stop the speech worker.
                        pass
                try:
                    if not self._shutdown_event.is_set():
                        bridge = self._browser_bridge
                        if browser_session and bridge is not None:
                            if completed and not cancel_event.is_set():
                                bridge.publish(browser_session, "complete")
                            elif (terminal_error and
                                  not cancel_event.is_set()):
                                bridge.publish(
                                    browser_session, "error",
                                    message=terminal_error[:500])
                            self.root.after(
                                0, self._finish_browser_session,
                                browser_session, cancel_event)
                        self.root.after(0, self._complete_playback_request,
                                        cancel_event, completed)
                except Exception:
                    pass

    def _exit_app(self, *_):
        """Fully stop Piper Reader; closing the window only hides it."""
        if self._exiting:
            return "break"
        if getattr(self, "_exit_waiting_for_studio", False):
            return "break"
        for studio in self._open_studios():
            studio.close()
            if studio in self._open_studios():
                if getattr(studio, "_close_when_idle", False):
                    self._exit_closing_studio = studio
                    self._exit_waiting_for_studio = True
                    self.root.after(80, self._continue_exit_after_studio)
                # An unsaved-change Cancel keeps the entire reader open.
                return "break"
        self._exiting = True
        self._hide_speed_panel(save=False)
        self._save_settings()
        self._shutdown_event.set()
        self._stop_clipboard_listener()
        self._pending_tray_hide = False
        self._cancel_after("_clipboard_after_id")
        self._cancel_after("_tray_after_id")
        self._cancel_after("_tray_hide_after_id")
        self._cancel_after("_clipboard_warm_after_id")
        self._cancel_after("_browser_after_id")
        self._cancel_after("_instance_after_id")
        self.stop(silent=True)
        self.speak_q.put(None)
        diagnostics = getattr(self, "_speech_diagnostics", None)
        if diagnostics is not None:
            diagnostics.close()
        bridge, self._browser_bridge = self._browser_bridge, None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        icon, self._tray_icon = self._tray_icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass
        return "break"

    def _continue_exit_after_studio(self):
        """Let the studio reap its child process before destroying Tk."""
        studios = self._open_studios()
        studio = getattr(self, "_exit_closing_studio", None)
        if studio is None and studios:
            studio = studios[0]
        if studio in studios:
            if (getattr(studio, "_close_when_idle", False)
                    or getattr(studio, "_close_prompt_active", False)):
                self.root.after(80, self._continue_exit_after_studio)
            else:
                # Its close prompt was cancelled after the worker stopped.
                self._exit_waiting_for_studio = False
            return
        self._exit_waiting_for_studio = False
        self._exit_closing_studio = None
        self._exit_app()

    def _on_close(self):
        """Compatibility alias for older shortcuts: X now hides to tray."""
        return self._hide_to_tray()


def run_ocr_runtime_self_test():
    """Perform a tiny real OCR pass against the packaged English model."""
    import fitz
    from PIL import Image, ImageDraw, ImageFont

    tessdata = ensure_ocr_tessdata("eng")
    image = Image.new("RGB", (900, 180), "white")
    drawing = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 58)
    except OSError:
        font = ImageFont.load_default(size=48)
    drawing.text((28, 50), "PIPER OCR TEST 42", fill="black", font=font)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    with fitz.open() as document:
        page = document.new_page(width=900, height=180)
        page.insert_image(page.rect, stream=encoded.getvalue())
        textpage = page.get_textpage_ocr(
            language="eng", dpi=OCR_DPI, full=True, tessdata=tessdata)
        recognized = page.get_text("text", textpage=textpage).upper()
    if "PIPER" not in recognized or "42" not in recognized:
        raise RuntimeError(
            f"OCR smoke text was not recognized: {recognized.strip()!r}")


def run_runtime_self_test():
    """Check packaged runtime imports and writable first-run resources."""
    checks = {}
    required_modules = (
        "piper", "kokoro_onnx", "onnxruntime", "numpy", "pythoncom",
        "win32com.client", "PIL", "pystray", "lxml.html", "pypdf",
        "fitz", "pymupdf", "library_store",
        "edge_engine", "edge_tts", "soundfile", "aiohttp", "certifi",
        "clipboard_speech", "speech_diagnostics", "document_outline",
        "ai_studio", "ai_studio_ui", "lesson_audio",
        "gemini_engine", "textbook_core", "textbook_audio", "textbook_studio",
    )
    for module_name in required_modules:
        try:
            __import__(module_name)
            checks[module_name] = "ok"
        except Exception as exc:
            checks[module_name] = f"error: {exc}"
    if IS_FROZEN:
        try:
            for relative in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
                             "SOURCE_CODE.md", "licenses/INDEX.txt", "licenses/SOURCES.json"):
                if not os.path.isfile(os.path.join(APP_DIR, relative)):
                    raise RuntimeError(f"Missing distribution material: {relative}")
            checks["distribution_materials"] = "ok"
        except Exception as exc:
            checks["distribution_materials"] = f"error: {exc}"
    try:
        import soundfile as sf
        if "MP3" not in sf.available_formats():
            raise RuntimeError("The bundled audio decoder cannot read MP3")
        checks["andrew_mp3_decoder"] = "ok"
    except Exception as exc:
        checks["andrew_mp3_decoder"] = f"error: {exc}"
    try:
        ensure_browser_bridge_config()
        with open(BROWSER_BRIDGE_CONFIG, encoding="utf-8") as stream:
            config = json.load(stream)
        extension_manifest = os.path.join(
            BROWSER_EXTENSION_DIR, "manifest.json")
        extension_config = os.path.join(
            BROWSER_EXTENSION_DIR, "bridge-config.js")
        if (len(str(config.get("token", ""))) < 32 or
                not os.path.isfile(extension_manifest) or
                not os.path.isfile(extension_config)):
            raise RuntimeError("browser extension first-run staging failed")
        checks["first_run_data"] = "ok"
    except Exception as exc:
        checks["first_run_data"] = f"error: {exc}"
    try:
        run_ocr_runtime_self_test()
        checks["ocr"] = "ok"
    except Exception as exc:
        checks["ocr"] = f"error: {exc}"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory(
                prefix="library-self-test-", dir=DATA_DIR) as library_root:
            library = LibraryStore(library_root)
            sample = "Private Library test - Hausa karatu - Arabic \u0642\u0631\u0627\u0621\u0629"
            item = library.add_text(sample, "Library self-test")
            if library.read_text(item) != sample or library.count() != 1:
                raise RuntimeError("Library text round-trip failed")
            library.remove(item["id"])
            if library.count() != 0:
                raise RuntimeError("Library item removal failed")
        checks["library"] = "ok"
    except Exception as exc:
        checks["library"] = f"error: {exc}"
    try:
        from ai_studio import parse_editable_script
        from ai_studio_ui import audio_cache_key
        from lesson_audio import split_speech_text
        lesson = parse_editable_script(
            "[Host]\nWhat is an opportunity cost?\n\n[Guest]\n"
            "It is the next best alternative given up.\n[Pause 5s]",
            style="podcast")
        if (len(lesson.segments) != 2 or lesson.segments[1].pause_after != 5
                or parse_editable_script(lesson.to_editable_text(), "podcast") != lesson
                or not split_speech_text(lesson.segments[1].text)
                or len(audio_cache_key([part.to_dict() for part in lesson.segments], {}, 1, 1)) != 64):
            raise RuntimeError("AI lesson round-trip failed")
        checks["ai_studio_offline"] = "ok"
    except Exception as exc:
        checks["ai_studio_offline"] = f"error: {exc}"
    checks["frozen"] = IS_FROZEN
    checks["version"] = APP_VERSION
    checks["app_dir"] = APP_DIR
    checks["data_dir"] = DATA_DIR
    passed = all(value == "ok" for key, value in checks.items()
                 if key not in ("frozen", "version", "app_dir", "data_dir"))
    report_path = os.environ.get("PIPER_READER_SELF_TEST_REPORT")
    if report_path:
        try:
            _replace_text_file(
                report_path,
                json.dumps({"passed": passed, "checks": checks},
                           indent=2) + "\n")
        except OSError:
            passed = False
    return 0 if passed else 1


if __name__ == "__main__":
    if os.name != "nt":
        print("This app is designed for Windows.")
    if any(arg.casefold() == "--self-test" for arg in sys.argv[1:]):
        raise SystemExit(run_runtime_self_test())
    shutdown_existing = any(
        arg.casefold() == "--shutdown-for-update" for arg in sys.argv[1:]
    )
    start_hidden = any(
        arg.casefold() in ("--tray", "--hidden", "--start-hidden", "--startup")
        for arg in sys.argv[1:]
    )
    try:
        if not acquire_single_instance(start_hidden, shutdown_existing):
            raise SystemExit(0)
        if shutdown_existing:
            raise SystemExit(0)
        WINDOWS_DPI_MODE = _enable_windows_high_dpi()
        root = tk.Tk()
        if start_hidden:
            # Keep sign-in launches invisible while the notification-area icon
            # is prepared. If tray startup fails, the window is restored.
            root.withdraw()
        app = ReaderApp(root)
        if start_hidden:
            root.after(0, app._hide_to_tray)
        root.mainloop()
    finally:
        release_single_instance()
