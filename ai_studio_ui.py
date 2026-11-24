"""Reviewable AI lessons with subscription-backed Codex and online narration.

The studio owns its source snapshot, worker, cancellation events and audio
player. It never changes the reader's document, library or selected voices.
"""

from dataclasses import asdict
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from ai_studio import (
    AIStudioCancelled, AIStudioError, CodexStudio, GenerationOptions,
    parse_editable_script,
)
from edge_engine import ANDREW_LABEL, AVA_LABEL, ONLINE_VOICES, SpeechCancelled
from lesson_audio import render_lesson_audio


LESSON_FORMAT = "PiperReader AI lesson"
LESSON_VERSION = 1
MAX_LESSON_BYTES = 10 * 1024 * 1024
STYLES = {"Lecture": "lecture", "Podcast": "podcast", "Study": "study"}
AUDIENCES = ("Beginner", "General audience", "Advanced")
DURATIONS = (5, 10, 15, 20)
DEFAULT_VOICES = {
    "Narrator": ANDREW_LABEL, "Host": ANDREW_LABEL, "Guest": AVA_LABEL,
}
DEFAULT_PALETTE = {
    "background": "#ffffff", "soft": "#f8fafe", "line": "#d8e2f1",
    "text": "#14213d", "secondary": "#43536f", "muted": "#7888a3",
    "accent": "#3c63f3", "accent_dark": "#3156dd",
}


def validate_lesson_document(document):
    """Validate a local lesson before any editor state is changed."""
    if not isinstance(document, dict):
        raise ValueError("This file does not contain a Piper Reader AI lesson.")
    if (document.get("format") != LESSON_FORMAT
            or type(document.get("version")) is not int
            or document.get("version") != LESSON_VERSION):
        raise ValueError("This is not a supported Piper Reader AI lesson file.")
    source = document.get("source")
    options = document.get("options")
    script = document.get("script")
    if not isinstance(source, dict) or not isinstance(options, dict):
        raise ValueError("The lesson's source or options are missing.")
    title, text = source.get("title"), source.get("text")
    if not isinstance(title, str) or len(title) > 1000:
        raise ValueError("The lesson title is invalid.")
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ValueError("The lesson source is missing or too large.")
    if not isinstance(script, str) or len(script) > 1_000_000:
        raise ValueError("The lesson script is missing or too large.")
    style = options.get("style")
    duration = options.get("duration_minutes")
    audience = options.get("audience")
    if style not in STYLES.values():
        raise ValueError("The lesson has an unsupported format.")
    if isinstance(duration, bool) or duration not in DURATIONS:
        raise ValueError("The lesson has an unsupported target length.")
    if audience not in AUDIENCES:
        raise ValueError("The lesson has an unsupported audience.")
    voices = document.get("voices", DEFAULT_VOICES)
    if not isinstance(voices, dict):
        raise ValueError("The lesson's voice choices are invalid.")
    voices = {speaker: voices.get(speaker, default)
              for speaker, default in DEFAULT_VOICES.items()}
    if any(not isinstance(value, str) or value not in ONLINE_VOICES
           for value in voices.values()):
        raise ValueError("The lesson uses a voice that is not available here.")
    notes = document.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        raise ValueError("The lesson's notes are invalid.")
    # An unfinished, editable script can be saved, including incomplete labels.
    # Parsing is required when it is played or exported as audio.
    return {
        "format": LESSON_FORMAT,
        "version": LESSON_VERSION,
        "source": {"title": title, "text": text},
        "options": {"style": style, "duration_minutes": duration,
                    "audience": audience},
        "script": script, "voices": voices, "notes": notes,
    }


def read_lesson_file(path):
    with open(path, "rb") as handle:
        contents = handle.read(MAX_LESSON_BYTES + 1)
    if len(contents) > MAX_LESSON_BYTES:
        raise ValueError("This lesson file is too large (maximum 10 MB).")
    try:
        document = json.loads(contents.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("This file is not valid UTF-8 lesson JSON.") from exc
    return validate_lesson_document(document)


def atomic_write_text(path, contents):
    """Preserve an existing export if a new write fails."""
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=str(destination.parent), prefix=".piperreader_",
                suffix=".tmp") as handle:
            temporary = handle.name
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def audio_cache_key(segments, voices, speed, volume):
    payload = {"segments": segments, "voices": voices,
               "speed": float(speed), "volume": float(volume)}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100]
    if not name or name.split(".", 1)[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}:
        return "AI lesson"
    return name


class AIStudioWindow(tk.Toplevel):
    """Independent Source/Script editor. Public lifecycle: stop(), close()."""

    def __init__(self, app, source_text="", source_title="Current chapter",
                 data_dir=None, palette=None):
        super().__init__(app.root)
        self.app = app
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.configure(bg=self.palette["background"])
        self.title("AI Studio — Piper Reader")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._closed = False
        self._close_when_idle = False
        self._close_prompt_active = False
        self._busy = None
        self._playing = False
        self._audio_reserved = False
        self._playback_gate = None
        self._worker = None
        self._job_id = 0
        self._events = queue.Queue()
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._poll_after = None
        self._metrics_after = None
        self._lesson_path = None
        self._notes = []
        self._cache_metadata = {}
        self._cache_paths = {}
        self._studio = CodexStudio()
        self._player = type(app.player)()
        self._player._alias = "piperreader_studio_" + uuid.uuid4().hex[:10]
        self._temp_dir = tempfile.mkdtemp(prefix="ai_studio_", dir=app.tmpdir)
        data_dir = data_dir or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.dirname(__file__)),
            "PiperReader")
        self.lesson_dir = os.path.join(data_dir, "ai_lessons")
        self._job_controls = []
        self._combos = []
        self.title_var = tk.StringVar(self, value=source_title or "AI lesson")
        self.style_var = tk.StringVar(self, value="Lecture")
        self.duration_var = tk.StringVar(self, value="10")
        self.audience_var = tk.StringVar(self, value="General audience")
        self.connection_var = tk.StringVar(self, value="Codex connection not checked")
        self.status_var = tk.StringVar(self, value="Review the source, then generate a script.")
        self.source_info_var = tk.StringVar(self)
        self.script_info_var = tk.StringVar(self, value="No script yet")
        self.voice_vars = {speaker: tk.StringVar(self, value=label)
                           for speaker, label in DEFAULT_VOICES.items()}
        self._build()
        self.source_text.insert("1.0", source_text or "")
        self.source_text.edit_reset()
        self.source_text.edit_modified(False)
        self.script_text.edit_modified(False)
        self._update_metrics()
        self._last_saved = self._document_fingerprint()
        self._update_controls()
        self._place_window()
        self.bind("<Control-s>", self._save_shortcut)
        self.bind("<Escape>", self._escape)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._poll_after = self.after(80, self._drain)

    def _place_window(self):
        """Keep point-sized controls and the editor usable on high-DPI laptops."""
        try:
            scale = max(0.75, min(4.0, float(self.winfo_fpixels("1i")) / 96.0))
        except (tk.TclError, TypeError, ValueError):
            scale = 1.0
        left = top = 0
        work_width, work_height = self.winfo_screenwidth(), self.winfo_screenheight()
        if os.name == "nt":
            try:
                class WorkRect(ctypes.Structure):
                    _fields_ = [(name, ctypes.c_long)
                                for name in ("left", "top", "right", "bottom")]
                rect = WorkRect()
                if ctypes.windll.user32.SystemParametersInfoW(
                        0x0030, 0, ctypes.byref(rect), 0):
                    left, top = rect.left, rect.top
                    work_width = rect.right - rect.left
                    work_height = rect.bottom - rect.top
            except (AttributeError, OSError):
                pass
        available_width = max(1, work_width - 80)
        available_height = max(1, work_height - 100)
        width = min(round(1120 * scale), available_width)
        height = min(round(900 * scale), available_height)
        x = left + max(0, (work_width - width) // 2)
        y = top + min(20, max(0, (work_height - height) // 2))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(round(900 * scale), width),
                     min(round(720 * scale), height))

    def _frame(self, parent):
        return tk.Frame(parent, bg=self.palette["background"])

    def _label(self, parent, text=None, textvariable=None, **kwargs):
        return tk.Label(parent, text=text, textvariable=textvariable,
                        bg=self.palette["background"],
                        fg=kwargs.pop("fg", self.palette["secondary"]),
                        font=kwargs.pop("font", ("Segoe UI", 10)), **kwargs)

    def _button(self, parent, text, command, primary=False, job_control=True):
        button = tk.Button(
            parent, text=text, command=command, relief="flat", bd=0,
            bg=self.palette["accent"] if primary else self.palette["soft"],
            fg="#ffffff" if primary else self.palette["text"],
            activebackground=self.palette["accent_dark"] if primary else "#eaf0ff",
            activeforeground="#ffffff" if primary else self.palette["text"],
            disabledforeground="#8992a3", padx=11, pady=6,
            font=("Segoe UI", 10, "bold" if primary else "normal"),
            highlightthickness=1, highlightbackground=self.palette["line"],
            highlightcolor=self.palette["accent"], takefocus=True, cursor="hand2")
        if job_control:
            self._job_controls.append(button)
        return button

    def _combo(self, parent, variable, values, width):
        widget = ttk.Combobox(parent, textvariable=variable, values=values,
                              state="readonly", width=width,
                              font=("Segoe UI", 10))
        self._combos.append(widget)
        return widget

    def _editor(self, parent):
        host = self._frame(parent)
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        editor = tk.Text(
            host, wrap="word", undo=True, autoseparators=True,
            maxundo=100, padx=14, pady=12, relief="flat", bd=0,
            bg=self.palette["background"], fg=self.palette["text"],
            insertbackground=self.palette["accent"],
            selectbackground="#cfe0fb", selectforeground=self.palette["text"],
            font=("Segoe UI", 11), spacing1=3, spacing3=5,
            highlightthickness=1, highlightbackground=self.palette["line"],
            highlightcolor=self.palette["accent"])
        scrollbar = ttk.Scrollbar(host, command=editor.yview)
        editor.configure(yscrollcommand=scrollbar.set)
        editor.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        editor.bind("<<Modified>>", self._text_modified)
        return host, editor

    def _build(self):
        shell = self._frame(self)
        shell.pack(fill="both", expand=True, padx=18, pady=(10, 9))
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(5, weight=1)
        header = self._frame(shell)
        header.grid(row=0, column=0, sticky="ew")
        self._label(header, "AI Studio", fg=self.palette["text"],
                    font=("Segoe UI", 20, "bold")).pack(side="left")
        self._button(header, "Sign in to Codex", self._login).pack(side="right")
        self._button(header, "Check connection", self._check_connection).pack(
            side="right", padx=(0, 8))
        self._label(shell, textvariable=self.connection_var, anchor="w").grid(
            row=1, column=0, sticky="ew", pady=(3, 8))
        self.notice = self._label(
            shell,
            "Generate sends the source to OpenAI through your ChatGPT sign-in. "
            "Your Codex allowance and any existing credit settings apply; there is no API key fallback. "
            "Audio sends the script to Microsoft and requires internet.",
            anchor="w", justify="left", font=("Segoe UI", 9), wraplength=980)
        self.notice.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        shell.bind("<Configure>", lambda event: self.notice.configure(
            wraplength=max(300, event.width - 4)), add="+")

        title_row = self._frame(shell)
        title_row.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        title_row.grid_columnconfigure(1, weight=1)
        self._label(title_row, "Lesson title").grid(row=0, column=0, padx=(0, 10))
        self.title_entry = tk.Entry(
            title_row, textvariable=self.title_var, font=("Segoe UI", 11),
            relief="flat", bg=self.palette["soft"], fg=self.palette["text"],
            insertbackground=self.palette["accent"], highlightthickness=1,
            highlightbackground=self.palette["line"],
            highlightcolor=self.palette["accent"])
        self.title_entry.grid(row=0, column=1, sticky="ew", ipady=5)

        options = self._frame(shell)
        options.grid(row=4, column=0, sticky="ew", pady=(0, 9))
        options.grid_columnconfigure(6, weight=1)
        self._label(options, "Format").grid(row=0, column=0, sticky="w")
        self._combo(options, self.style_var, tuple(STYLES), 10).grid(
            row=1, column=0, padx=(0, 12), pady=(4, 0))
        self._label(options, "Length (approx.)").grid(row=0, column=1, sticky="w")
        self._combo(options, self.duration_var, tuple(map(str, DURATIONS)), 5).grid(
            row=1, column=1, sticky="w", padx=(0, 4), pady=(4, 0))
        self._label(options, "minutes").grid(row=1, column=2, padx=(0, 12))
        self._label(options, "Audience").grid(row=0, column=3, sticky="w")
        self._combo(options, self.audience_var, AUDIENCES, 17).grid(
            row=1, column=3, sticky="w", padx=(0, 12), pady=(4, 0))
        self.generate_button = self._button(
            options, "Generate script", self._generate, primary=True)
        self.generate_button.grid(row=1, column=4, padx=(0, 8))
        self.cancel_button = self._button(
            options, "Cancel", self.stop, job_control=False)
        self.cancel_button.grid(row=1, column=5)

        self.notebook = ttk.Notebook(shell)
        self.notebook.grid(row=5, column=0, sticky="nsew")
        self.source_page, self.script_page = self._frame(self.notebook), self._frame(self.notebook)
        self.notebook.add(self.source_page, text="  1. Source  ")
        self.notebook.add(self.script_page, text="  2. Script & audio  ")
        for page in (self.source_page, self.script_page):
            page.grid_columnconfigure(0, weight=1)
            page.grid_rowconfigure(1, weight=1)
        source_top = self._frame(self.source_page)
        source_top.grid(row=0, column=0, sticky="ew", pady=(10, 8))
        self._label(source_top, "Edit this copy of the source before generating.").pack(side="left")
        self._label(source_top, textvariable=self.source_info_var).pack(side="right")
        host, self.source_text = self._editor(self.source_page)
        host.grid(row=1, column=0, sticky="nsew")
        self._label(self.source_page,
                    "Lecture explains · Podcast uses two speakers · Study adds review questions and pauses",
                    anchor="w", font=("Segoe UI", 9)).grid(
                        row=2, column=0, sticky="ew", pady=(8, 6))

        script_top = self._frame(self.script_page)
        script_top.grid(row=0, column=0, sticky="ew", pady=(10, 8))
        self._label(script_top, "Review and edit the script before listening.").pack(side="left")
        self._label(script_top, textvariable=self.script_info_var).pack(side="right")
        host, self.script_text = self._editor(self.script_page)
        host.grid(row=1, column=0, sticky="nsew")
        self._label(self.script_page,
                    "Speaker labels go on their own line: [Narrator], or [Host] and [Guest]. "
                    "Use [Pause 3s] after a passage.",
                    anchor="w", font=("Segoe UI", 9)).grid(
                        row=2, column=0, sticky="ew", pady=(7, 9))
        voice_row = self._frame(self.script_page)
        voice_row.grid(row=3, column=0, sticky="ew")
        # Short menu labels keep all three choices visible on laptop screens.
        self._voice_short = {label.split(" - ", 1)[0]: label for label in ONLINE_VOICES}
        self._voice_display_vars = {}
        for column, speaker in enumerate(DEFAULT_VOICES):
            voice_row.grid_columnconfigure(column, weight=1)
            group = self._frame(voice_row)
            group.grid(row=0, column=column, sticky="ew", padx=(0, 10 if column < 2 else 0))
            group.grid_columnconfigure(0, weight=1)
            self._label(group, speaker + " voice").grid(row=0, column=0, sticky="w")
            display = tk.StringVar(self, value=self.voice_vars[speaker].get().split(" - ", 1)[0])
            self._voice_display_vars[speaker] = display
            combo = self._combo(group, display, tuple(self._voice_short), 22)
            combo.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            display.trace_add("write", lambda *_args, role=speaker: self._voice_changed(role))
        playback = self._frame(self.script_page)
        playback.grid(row=4, column=0, sticky="ew", pady=(10, 6))
        self.play_button = self._button(playback, "Play", self._play, primary=True)
        self.play_button.pack(side="left")
        self.pause_button = self._button(playback, "Pause", self._toggle_pause, job_control=False)
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = self._button(playback, "Stop", self.stop, job_control=False)
        self.stop_button.pack(side="left")
        self._label(playback, "Microsoft online voices · Reader speed and volume apply",
                    font=("Segoe UI", 9)).pack(side="left", padx=12)

        footer = self._frame(shell)
        footer.grid(row=6, column=0, sticky="ew", pady=(9, 0))
        self._button(footer, "Load lesson…", self._load).pack(side="left")
        self._button(footer, "Save lesson…", self._save).pack(side="left", padx=(8, 0))
        self.export_text_button = self._button(footer, "Export script…", self._export_text)
        self.export_text_button.pack(side="left", padx=(8, 0))
        self.export_wav_button = self._button(footer, "Export WAV…", self._export_wav)
        self.export_wav_button.pack(side="left", padx=(8, 0))
        self.notes_button = self._button(footer, "Notes…", self._show_notes)
        self.notes_button.pack(side="left", padx=(8, 0))
        self._button(footer, "Close", self.close, job_control=False).pack(side="right")
        status = self._frame(shell)
        status.grid(row=7, column=0, sticky="ew", pady=(7, 0))
        status.grid_columnconfigure(0, weight=1)
        self.status_label = self._label(status, textvariable=self.status_var,
                                        anchor="w", justify="left", wraplength=930)
        self.status_label.grid(row=0, column=0, sticky="ew")
        status.bind("<Configure>", lambda event: self.status_label.configure(
            wraplength=max(200, event.width)), add="+")
        self.style_var.trace_add("write", lambda *_: self._style_changed())

    def _voice_changed(self, speaker):
        selection = self._voice_display_vars[speaker].get()
        if selection in self._voice_short:
            self.voice_vars[speaker].set(self._voice_short[selection])

    def _style_changed(self):
        if hasattr(self, "script_text"):
            self._update_metrics()

    def _text_modified(self, event):
        if not event.widget.edit_modified():
            return
        event.widget.edit_modified(False)
        if self._metrics_after is None and not self._closed:
            self._metrics_after = self.after(120, self._update_metrics)

    def _update_metrics(self):
        self._metrics_after = None
        if self._closed:
            return
        source = self.source_text.get("1.0", "end-1c")
        script = self.script_text.get("1.0", "end-1c")
        self.source_info_var.set(f"{len(source.split()):,} source words")
        spoken = re.sub(r"(?m)^\s*\[[^\]]+\]\s*$", "", script)
        words = len(spoken.split())
        if words:
            self.script_info_var.set(f"{words:,} words · about {max(1, round(words / 150))} min at 1×")
        else:
            self.script_info_var.set("No script yet")
        self._update_controls()

    def _update_controls(self):
        if self._closed:
            return
        busy = self._busy is not None
        state = "disabled" if busy else "normal"
        for widget in self._job_controls:
            widget.configure(state=state)
        for widget in self._combos:
            widget.configure(state="disabled" if busy else "readonly")
        self.title_entry.configure(state=state)
        self.source_text.configure(state=state)
        self.script_text.configure(state=state)
        has_script = bool(self.script_text.get("1.0", "end-1c").strip())
        has_source = bool(self.source_text.get("1.0", "end-1c").strip())
        self.generate_button.configure(state="normal" if has_source and not busy else "disabled")
        for widget in (self.play_button, self.export_text_button, self.export_wav_button):
            widget.configure(state="normal" if has_script and not busy else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.pause_button.configure(state="normal" if self._playing else "disabled",
                                    text="Resume" if self._pause.is_set() else "Pause")
        self.notes_button.configure(state="normal" if self._notes and not busy else "disabled")

    def _document(self):
        return {
            "format": LESSON_FORMAT, "version": LESSON_VERSION,
            "source": {"title": self.title_var.get(),
                       "text": self.source_text.get("1.0", "end-1c")},
            "options": {"style": STYLES[self.style_var.get()],
                        "duration_minutes": int(self.duration_var.get()),
                        "audience": self.audience_var.get()},
            "script": self.script_text.get("1.0", "end-1c"),
            "voices": {key: value.get() for key, value in self.voice_vars.items()},
            "notes": self._notes,
        }

    def _document_fingerprint(self):
        return hashlib.sha256(json.dumps(self._document(), ensure_ascii=False,
                                        sort_keys=True).encode("utf-8")).hexdigest()

    def _start_job(self, kind, worker, message):
        if self._closed or self._busy:
            return False
        self._busy = kind
        self._playing = False
        self._audio_reserved = False
        self._playback_gate = None
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._job_id += 1
        job_id, cancel = self._job_id, self._cancel
        self.status_var.set(message)
        self._update_controls()

        def run():
            try:
                result = worker(cancel, job_id)
                # Export cancellation is checked before its atomic commit.
                # Once committed, report the saved file even if Stop arrives.
                if cancel.is_set() and kind != "export":
                    raise AIStudioCancelled("Cancelled.")
                self._events.put((job_id, "done", kind, result))
            except (AIStudioCancelled, SpeechCancelled):
                self._events.put((job_id, "cancelled", kind, None))
            except Exception as exc:
                self._events.put((job_id, "error", kind, str(exc)))
            finally:
                if self._closed:
                    shutil.rmtree(self._temp_dir, ignore_errors=True)

        self._worker = threading.Thread(target=run, daemon=True,
                                        name="PiperReader-AIStudio-" + kind)
        self._worker.start()
        return True

    def _progress(self, job_id, text):
        self._events.put((job_id, "progress", None, str(text)))

    def _drain(self):
        self._poll_after = None
        if self._closed:
            return
        try:
            while True:
                job_id, event, kind, result = self._events.get_nowait()
                if job_id != self._job_id:
                    continue
                if event == "progress":
                    self.status_var.set(result)
                elif event == "ready_to_play":
                    # Audio preparation can run alongside clipboard reading.
                    # Reserve the speakers only once the WAV is ready, then
                    # let already queued reader speech finish before playback.
                    self._playback_gate = result
                    reserve = getattr(type(self.app), "_try_reserve_studio_audio", None)
                    self._audio_reserved = (reserve(self.app, self) if reserve else True)
                    self.status_var.set("Audio ready. Waiting for reader speech to finish…")
                elif event == "playing":
                    self._playing = True
                    self.status_var.set("Playing. Pause or Stop at any time.")
                    self._update_controls()
                elif event in ("done", "error", "cancelled"):
                    self._busy = None
                    self._playing = False
                    self._audio_reserved = False
                    self._playback_gate = None
                    self._pause.clear()
                    self._update_controls()
                    if event == "error":
                        show_error = getattr(self, "_show_job_error", None)
                        if show_error is not None:
                            show_error(result)
                        else:
                            self.status_var.set("Could not complete this step. See the error details.")
                            messagebox.showerror("AI Studio", result, parent=self)
                    elif event == "cancelled":
                        self.status_var.set("Stopped. Your source and script are still here.")
                    else:
                        self._completed(kind, result)
                    if self._close_when_idle:
                        self._close_when_idle = False
                        self.close()
                        if self._closed:
                            return
                else:
                    handler = getattr(self, "_handle_studio_event", None)
                    if handler is not None:
                        handler(event, kind, result)
        except queue.Empty:
            pass
        self._grant_audio_when_reader_idle()
        if not self._closed:
            self._poll_after = self.after(80, self._drain)

    @property
    def owns_audio(self):
        """True while ready/playback reserves speakers, excluding rendering."""
        return self._audio_reserved

    def _grant_audio_when_reader_idle(self):
        gate = self._playback_gate
        if (self._closed or gate is None
                or gate.is_set() or self._cancel.is_set()):
            return
        if not self._audio_reserved:
            reserve = getattr(type(self.app), "_try_reserve_studio_audio", None)
            self._audio_reserved = (reserve(self.app, self) if reserve else True)
            if not self._audio_reserved:
                return
        if not self.app._reader_audio_is_busy():
            gate.set()

    def _completed(self, kind, result):
        if kind in ("connection", "login"):
            self.connection_var.set(result.message)
            self.status_var.set("Ready to generate with Codex." if result.connected else result.message)
        elif kind == "generate":
            self.connection_var.set("Connected through ChatGPT. Uses your Codex allowance.")
            self.script_text.edit_separator()
            self.script_text.delete("1.0", "end")
            self.script_text.insert("1.0", result.to_editable_text())
            self.script_text.edit_separator()
            self._notes = list(result.notes)
            self.notebook.select(self.script_page)
            self.status_var.set("Script ready. Review names, facts and examples before listening."
                                + (f" {len(self._notes)} generation notes are available."
                                   if self._notes else ""))
            self._update_metrics()
        elif kind == "play":
            self.status_var.set("Playback finished.")
        elif kind == "export":
            path, metadata = result
            seconds = float(metadata.get("duration_seconds", 0))
            self.status_var.set(f"Saved WAV ({int(seconds // 60)}:{int(seconds % 60):02d}): {path}")

    def _check_connection(self):
        self._start_job("connection", lambda cancel, _: self._studio.check_connection(cancel=cancel),
                        "Checking the local Codex connection…")

    def _login(self):
        self._start_job("login", lambda cancel, job: self._studio.login(
            cancel=cancel, progress=lambda text: self._progress(job, text)),
            "Starting Codex sign-in. Follow the browser instructions when they appear…")

    def _generate(self):
        source = self.source_text.get("1.0", "end-1c").strip()
        if not source:
            return
        options = GenerationOptions(
            style=STYLES[self.style_var.get()],
            duration_minutes=int(self.duration_var.get()),
            audience=self.audience_var.get(), source_title=self.title_var.get())
        self._start_job("generate", lambda cancel, job: self._studio.generate(
            source, options, cancel=cancel,
            progress=lambda text: self._progress(job, text)),
            "Preparing your script with Codex…")

    def _audio_snapshot(self):
        try:
            lesson = parse_editable_script(
                self.script_text.get("1.0", "end-1c"),
                style=STYLES[self.style_var.get()],
                title=self.title_var.get() or "AI lesson")
            segments = [asdict(segment) for segment in lesson.segments]
            voices = {role: value.get() for role, value in self.voice_vars.items()}
            return (segments, voices, float(self.app.speed_var.get()),
                    float(self.app.vol_var.get()))
        except (AIStudioError, ValueError, TypeError) as exc:
            messagebox.showerror("Check the script", str(exc), parent=self)
            return None

    def _render(self, snapshot, cancel, job_id):
        segments, voices, speed, volume = snapshot
        key = audio_cache_key(segments, voices, speed, volume)
        # MCI waveaudio rejects otherwise valid long paths (including a full
        # SHA256 filename beneath the app's temporary directories). Keep an
        # 8.3 filename while retaining the complete hash as the cache identity.
        # Sequential names avoid collisions between shortened hash prefixes.
        path = self._cache_paths.setdefault(
            key, os.path.join(self._temp_dir,
                              f"a{len(self._cache_paths):07x}.wav"))
        if key in self._cache_metadata and os.path.isfile(path):
            return path, self._cache_metadata[key]
        metadata = render_lesson_audio(
            segments, path, voices, speed=speed, volume=volume,
            cancel_event=cancel, engine=self.app.engines["edge"],
            progress=lambda done, total, speaker: self._progress(
                job_id, f"Preparing audio {done}/{total} · {speaker}…"))
        self._cache_metadata[key] = metadata
        return path, metadata

    def _play(self):
        if self._busy:
            return
        snapshot = self._audio_snapshot()
        if snapshot is None:
            return
        def worker(cancel, job_id):
            path, metadata = self._render(snapshot, cancel, job_id)
            if cancel.is_set():
                raise AIStudioCancelled("Cancelled.")
            ready = threading.Event()
            self._events.put((job_id, "ready_to_play", None, ready))
            while not ready.wait(0.05):
                if cancel.is_set():
                    raise AIStudioCancelled("Cancelled.")
            if cancel.is_set():
                raise AIStudioCancelled("Cancelled.")
            self._events.put((job_id, "playing", None, None))
            self._player.play_blocking(path, cancel, self._pause)
            return metadata
        self._start_job("play", worker, "Preparing audio before playback…")

    def _toggle_pause(self):
        if not self._playing:
            return
        if self._pause.is_set():
            self._pause.clear()
            self.status_var.set("Playing.")
        else:
            self._pause.set()
            self.status_var.set("Paused. Choose Resume to continue.")
        self._update_controls()

    def stop(self):
        """Cancel this studio's work without calling app.stop recursively."""
        self._cancel.set()
        self._pause.clear()
        self._player.stop()
        if not self._closed and self._busy:
            self.status_var.set("Stopping…")
            self.pause_button.configure(state="disabled", text="Pause")

    def _export_wav(self):
        snapshot = self._audio_snapshot()
        if snapshot is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export lesson audio", defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav")],
            initialfile=_safe_filename(self.title_var.get()) + ".wav")
        if not path:
            return
        def worker(cancel, job_id):
            cached, metadata = self._render(snapshot, cancel, job_id)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                        dir=os.path.dirname(os.path.abspath(path)),
                        prefix=".piperreader_", suffix=".wav", delete=False) as out:
                    temporary = out.name
                    with open(cached, "rb") as source:
                        while True:
                            if cancel.is_set():
                                raise AIStudioCancelled("Cancelled.")
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                if cancel.is_set():
                    raise AIStudioCancelled("Cancelled.")
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            return path, metadata
        self._start_job("export", worker, "Preparing your WAV export…")

    def _export_text(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="Export editable script", defaultextension=".txt",
            filetypes=[("Text files", "*.txt")],
            initialfile=_safe_filename(self.title_var.get()) + ".txt")
        if not path:
            return
        try:
            atomic_write_text(path, self.title_var.get() + "\n\n" +
                              self.script_text.get("1.0", "end-1c") + "\n")
            self.status_var.set("Saved script: " + path)
        except OSError as exc:
            messagebox.showerror("Could not export script", str(exc), parent=self)

    def _show_notes(self):
        if self._notes:
            messagebox.showinfo("Generation notes", "\n\n".join(self._notes), parent=self)

    def _save(self):
        if self._busy:
            return False
        try:
            os.makedirs(self.lesson_dir, exist_ok=True)
            path = filedialog.asksaveasfilename(
                parent=self, title="Save AI lesson", defaultextension=".json",
                filetypes=[("Piper Reader AI lessons", "*.json")],
                initialdir=os.path.dirname(self._lesson_path) if self._lesson_path else self.lesson_dir,
                initialfile=os.path.basename(self._lesson_path) if self._lesson_path else
                _safe_filename(self.title_var.get()) + ".json")
            if not path:
                return False
            document = validate_lesson_document(self._document())
            contents = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            if len(contents.encode("utf-8")) > MAX_LESSON_BYTES:
                raise ValueError("This lesson is too large to save (maximum 10 MB).")
            atomic_write_text(path, contents)
            self._lesson_path = path
            self._last_saved = self._document_fingerprint()
            self.status_var.set("Saved lesson locally: " + path)
            return True
        except (ValueError, OSError) as exc:
            messagebox.showerror("Could not save lesson", str(exc), parent=self)
            return False

    def _confirm_discard(self):
        if self._document_fingerprint() == self._last_saved:
            return True
        answer = messagebox.askyesnocancel(
            "Save this lesson?", "Save your source and script changes before continuing?",
            parent=self)
        if answer is None:
            return False
        return self._save() if answer else True

    def _load(self):
        if self._busy or not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            parent=self, title="Load AI lesson", initialdir=self.lesson_dir,
            filetypes=[("Piper Reader AI lessons", "*.json")])
        if not path:
            return
        try:
            document = read_lesson_file(path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Could not load lesson", str(exc), parent=self)
            return
        self.title_var.set(document["source"]["title"])
        self.style_var.set(next(label for label, key in STYLES.items()
                                if key == document["options"]["style"]))
        self.duration_var.set(str(document["options"]["duration_minutes"]))
        self.audience_var.set(document["options"]["audience"])
        for editor, text in ((self.source_text, document["source"]["text"]),
                             (self.script_text, document["script"])):
            editor.delete("1.0", "end")
            editor.insert("1.0", text)
            editor.edit_reset()
            editor.edit_modified(False)
        for role, voice in document["voices"].items():
            self._voice_display_vars[role].set(voice.split(" - ", 1)[0])
        self._notes = document["notes"]
        self._lesson_path = path
        self._last_saved = self._document_fingerprint()
        self._update_metrics()
        self.notebook.select(self.script_page if document["script"].strip() else self.source_page)
        self.status_var.set("Loaded lesson: " + path)

    def _save_shortcut(self, _event=None):
        self._save()
        return "break"

    def _escape(self, _event=None):
        if self._busy:
            self.stop()
        return "break"

    def close(self, force=False):
        if self._closed:
            return
        if not force and self._busy:
            self._close_when_idle = True
            self.stop()
            self.status_var.set("Stopping before closing…")
            return
        if not force:
            self._close_prompt_active = True
            try:
                if not self._confirm_discard():
                    return
            finally:
                self._close_prompt_active = False
        self._closed = True
        self.stop()
        for token in (self._poll_after, self._metrics_after):
            if token:
                try:
                    self.after_cancel(token)
                except tk.TclError:
                    pass
        if not self._worker or not self._worker.is_alive():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.destroy()

    def _destroyed(self, event):
        if event.widget is self and not self._closed:
            self._closed = True
            self._cancel.set()
            self._pause.clear()
            self._player.stop()
            if not self._worker or not self._worker.is_alive():
                shutil.rmtree(self._temp_dir, ignore_errors=True)
