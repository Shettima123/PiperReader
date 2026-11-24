"""Complete source reading and optional, separately stored teaching notes.

Network work starts only after an explicit Generate, Sample or Export action.
The reader's document, clipboard settings, voices and library are not changed.
"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from ai_studio import AIStudioCancelled
from ai_studio_ui import AIStudioWindow, DEFAULT_PALETTE, _safe_filename
from edge_engine import ANDREW_LABEL
from gemini_engine import (
    CredentialStore, DEFAULT_GEMINI_LABEL, GEMINI_VOICES, GeminiEngine, GeminiError,
    estimate_audio_cost,
)
from textbook_core import (
    COVERAGE_LIMITATION, MAX_SOURCE_CHARS, TextbookCancelled, TextbookError,
    TextbookProject, generate_explanations, load_project, save_project,
)


PROVIDERS = {"Gemini 3.8 Flash TTS": "gemini", "Andrew — free, online": "edge"}
MODES = {"Read the complete textbook": "read",
         "Read and explain the complete textbook": "explain"}


def project_for_editor(previous, source, title, mode, extraction_notes=()):
    """Editing a title/mode preserves notes; editing source makes a fresh plan."""
    if previous is not None and previous.source_text == source:
        return replace(previous, title=title, mode=mode).validate()
    return TextbookProject.create(source, title=title, mode=mode,
                                  extraction_notes=extraction_notes)


def estimate_project(project, speed=1.0, sample=False):
    """Planning estimate at 150 words/minute, never a metered charge or cap."""
    plan = project.plan_segments()
    if sample:
        selected = {block.source_id for block in project.blocks[:2]}
        plan = tuple(item for item in plan if item["source_id"] in selected)
    seconds = sum(len(item["text"].split()) for item in plan) * 60 / (150 * speed)
    seconds += sum(item["pause_after"] for item in plan)
    return seconds, estimate_audio_cost(seconds)


def import_source_file(path, cancel=None, pdf_lock=None):
    """Read bounded text/PDF content. PDF coverage is explicitly unverified."""
    path = Path(path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise TextbookError("Choose a source smaller than 100 MB.")
    if path.suffix.lower() != ".pdf":
        with path.open("r", encoding="utf-8-sig") as handle:
            text = handle.read(MAX_SOURCE_CHARS + 1)
        if len(text) > MAX_SOURCE_CHARS:
            raise TextbookError("This text is too large. Import one chapter at a time.")
        if not text.strip():
            raise TextbookError("The file contains no readable text.")
        return text, path.stem, ()
    import fitz
    lock = pdf_lock or threading.Lock()
    pages, size, empty = [], 0, []
    with lock, fitz.open(str(path)) as document:
        if document.needs_pass:
            raise TextbookError("This PDF is password protected. Open an unlocked copy.")
        for index, page in enumerate(document):
            if cancel is not None and cancel.is_set():
                raise AIStudioCancelled("Import stopped.")
            text = page.get_text("text", sort=True)
            if not text.strip():
                empty.append(index + 1)
            pages.append(text)
            size += len(text) + 2
            if size > MAX_SOURCE_CHARS:
                raise TextbookError("This PDF is too large. Import one chapter at a time.")
    text = "\n\n".join(pages)
    if not text.strip():
        raise TextbookError("This PDF has no extractable text. Use the reader's OCR first, then Current text.")
    notes = ["PDF text extraction: verify reading order, figures, tables, equations and footnotes against the original pages."]
    if empty:
        notes.append("Pages without extracted text: " + ", ".join(map(str, empty[:150])) +
                     ("…" if len(empty) > 150 else ""))
    return text, path.stem, tuple(notes)


class TextbookStudioWindow(AIStudioWindow):
    """Uses AI Studio's worker cancellation, audio player and close lifecycle."""

    def __init__(self, app, source_text="", source_title="Current chapter",
                 data_dir=None, palette=None):
        tk.Toplevel.__init__(self, app.root)
        self.app = app
        self.palette = {**DEFAULT_PALETTE, **(palette or {})}
        self.configure(bg=self.palette["background"])
        self.title("Textbook Studio — Piper Reader")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._closed = self._close_when_idle = self._close_prompt_active = False
        self._busy = self._worker = self._playback_gate = None
        self._playing = self._audio_reserved = False
        self._job_id = 0
        self._events = queue.Queue()
        self._cancel, self._pause = threading.Event(), threading.Event()
        self._poll_after = self._metrics_after = None
        self._project = self._project_path = None
        self._extraction_notes = ()
        self._api_key = None
        self._key_window = None
        self._last_error_detail = None
        self._note_source_id = None
        self._player = type(app.player)()
        self._player._alias = "piperreader_book_" + uuid.uuid4().hex[:10]
        self._temp_dir = tempfile.mkdtemp(prefix="book_", dir=app.tmpdir)
        self.data_dir = Path(data_dir or Path(os.environ.get("LOCALAPPDATA", ".")) / "PiperReader")
        self.project_dir = self.data_dir / "textbooks"
        self._credential_store = CredentialStore(self.data_dir)
        self._job_controls, self._combos, self._radios = [], [], []
        self.title_var = tk.StringVar(self, value=source_title or "Textbook chapter")
        self.mode_var = tk.StringVar(self, value="read")
        self.provider_var = tk.StringVar(self, value=next(iter(PROVIDERS)))
        self.voice_var = tk.StringVar(self, value=DEFAULT_GEMINI_LABEL.split(" - ")[0])
        self.status_var = tk.StringVar(self, value="Review your source, then listen to a short sample.")
        self.source_info_var = tk.StringVar(self)
        self.cost_var = tk.StringVar(self)
        self._build()
        self.source_text.insert("1.0", source_text)
        self.source_text.edit_modified(False)
        self._last_saved = self._document_fingerprint()
        self._update_metrics()
        self._update_controls()
        self._place_window()
        self.bind("<Control-s>", self._save_shortcut)
        self.bind("<Escape>", self._escape)
        self.bind("<Destroy>", self._destroyed, add="+")
        self.title_var.trace_add("write", lambda *_: self._schedule_metrics())
        self._poll_after = self.after(80, self._drain)

    def _build(self):
        shell = self._frame(self)
        shell.pack(fill="both", expand=True, padx=18, pady=12)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(5, weight=1)
        header = self._frame(shell)
        header.grid(row=0, column=0, sticky="ew")
        self._label(header, "Textbook Studio", font=("Segoe UI", 20, "bold"),
                    fg=self.palette["text"]).pack(side="left")
        for label, command in (("Gemini key", self._key_dialog),
                               ("Check access", self._check_access),
                               ("Save project", self._save), ("Open project", self._load)):
            self._button(header, label, command).pack(side="right", padx=(6, 0))
        self.notice = self._label(shell,
            "Gemini audio uses your Google API key and may incur separate charges. Free quotas depend on your account. "
            "Explanations use your ChatGPT Codex allowance. Nothing is generated automatically.",
            anchor="w", justify="left", font=("Segoe UI", 9), wraplength=1000)
        self.notice.grid(row=1, column=0, sticky="ew", pady=(7, 9))
        title = self._frame(shell)
        title.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._label(title, "Title").pack(side="left", padx=(0, 9))
        self.title_entry = tk.Entry(title, textvariable=self.title_var, font=("Segoe UI", 11),
                                    bg=self.palette["soft"], relief="flat")
        self.title_entry.pack(side="left", fill="x", expand=True, ipady=5)
        mode = self._frame(shell)
        mode.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        for label, value in MODES.items():
            button = tk.Radiobutton(mode, text=label, variable=self.mode_var, value=value,
                                    command=self._mode_changed, bg=self.palette["background"],
                                    fg=self.palette["text"], activebackground=self.palette["background"],
                                    font=("Segoe UI", 10), anchor="w")
            button.pack(side="left", padx=(0, 20))
            self._radios.append(button)
        options = self._frame(shell)
        options.grid(row=4, column=0, sticky="ew", pady=(0, 9))
        self._combo(options, self.provider_var, tuple(PROVIDERS), 25).pack(side="left")
        self._combos[-1].bind("<<ComboboxSelected>>", self._provider_changed)
        self.voice_combo = self._combo(options, self.voice_var,
            tuple(key.split(" - ")[0] for key in GEMINI_VOICES), 27)
        self.voice_combo.pack(side="left", padx=8)
        self._label(options, "Speed and volume follow the reader.", font=("Segoe UI", 9)).pack(side="left")
        self.notebook = ttk.Notebook(shell)
        self.notebook.grid(row=5, column=0, sticky="nsew")
        self.source_page, self.notes_page = self._frame(self.notebook), self._frame(self.notebook)
        self.notebook.add(self.source_page, text="Complete source")
        self.notebook.add(self.notes_page, text="Teaching notes")
        source_tools = self._frame(self.source_page)
        source_tools.pack(fill="x", pady=7)
        self._button(source_tools, "Import text / PDF", self._import).pack(side="left")
        self._button(source_tools, "Current reader text", self._current_source).pack(side="left", padx=8)
        self._label(source_tools, textvariable=self.source_info_var).pack(side="right", padx=8)
        host, self.source_text = self._editor(self.source_page)
        host.pack(fill="both", expand=True)
        notes_tools = self._frame(self.notes_page)
        notes_tools.pack(fill="x", pady=7)
        self._button(notes_tools, "Generate / resume explanations", self._generate,
                     primary=True).pack(side="left")
        self._button(notes_tools, "Apply note edit", self._apply_note).pack(side="left", padx=8)
        self._label(notes_tools, "AI additions are separate from the source.", font=("Segoe UI", 9)).pack(side="left")
        notes_body = self._frame(self.notes_page)
        notes_body.pack(fill="both", expand=True)
        self.note_list = tk.Listbox(notes_body, width=31, exportselection=False,
                                   bg=self.palette["soft"], fg=self.palette["text"],
                                   font=("Segoe UI", 10), relief="flat")
        self.note_list.pack(side="left", fill="y", padx=(0, 8))
        self.note_list.bind("<<ListboxSelect>>", self._select_note)
        host, self.note_text = self._editor(notes_body)
        host.pack(fill="both", expand=True)
        self.note_text.configure(state="disabled")
        self.coverage_notice = self._label(shell, COVERAGE_LIMITATION, anchor="w", justify="left",
                                           font=("Segoe UI", 9), wraplength=1000)
        self.coverage_notice.grid(row=6, column=0, sticky="ew", pady=(8, 3))
        self._label(shell, textvariable=self.cost_var, anchor="w", font=("Segoe UI", 9)).grid(
            row=7, column=0, sticky="ew", pady=(0, 5))
        actions = self._frame(shell)
        actions.grid(row=8, column=0, sticky="ew")
        self._button(actions, "Listen to sample", self._play, primary=True).pack(side="left")
        self._button(actions, "Listen complete", self._play_complete).pack(side="left", padx=8)
        self._button(actions, "Export WAV", self._export_wav).pack(side="left", padx=(0, 8))
        self.pause_button = self._button(actions, "Pause", self._toggle_pause, job_control=False)
        self.pause_button.pack(side="left")
        self.stop_button = self._button(actions, "Stop", self.stop, job_control=False)
        self.stop_button.pack(side="left", padx=8)
        self.error_button = self._button(actions, "Last error", self._show_last_error,
                                         job_control=False)
        self.error_button.pack(side="right")
        self._label(shell, textvariable=self.status_var, anchor="w", justify="left", wraplength=1000).grid(
            row=9, column=0, sticky="ew", pady=(7, 0))
        shell.bind("<Configure>", lambda event: [label.configure(wraplength=max(300, event.width - 4))
                   for label in (self.notice, self.coverage_notice)], add="+")

    def _document_fingerprint(self):
        payload = [self.title_var.get(), self.mode_var.get(),
                   self.source_text.get("1.0", "end-1c"), self._extraction_notes,
                   [(item.source_id, item.text) for item in self._project.explanations]
                   if self._project else []]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _snapshot_project(self):
        self._apply_note(quiet=True)
        self._project = project_for_editor(self._project,
            self.source_text.get("1.0", "end-1c"), self.title_var.get().strip() or "Textbook chapter",
            self.mode_var.get(), self._extraction_notes)
        self._refresh_notes()
        return self._project

    def _text_modified(self, event):
        widget = event.widget
        if widget.edit_modified():
            widget.edit_modified(False)
            self._schedule_metrics()

    def _schedule_metrics(self):
        if self._closed:
            return
        if self._metrics_after:
            self.after_cancel(self._metrics_after)
        self._metrics_after = self.after(220, self._update_metrics)

    def _update_metrics(self):
        self._metrics_after = None
        source = self.source_text.get("1.0", "end-1c")
        same = self._project is not None and self._project.source_text == source
        words = len(source.split())
        notes = f" · {len(self._project.completed_source_ids)}/{len(self._project.blocks)} notes prepared" if same else ""
        if self._project is not None and not same and self._project.explanations:
            notes = " · edited source: notes reset on the next step"
        self.source_info_var.set(f"{words:,} words{notes}")
        try:
            speed = max(0.25, float(self.app.speed_var.get()))
        except (TypeError, ValueError):
            speed = 1.0
        seconds = words * 60 / (150 * speed)
        sample_seconds = min(seconds, len(source[:3200].split()) * 60 / (150 * speed))
        if same:
            seconds, _ = estimate_project(self._project.with_mode(self.mode_var.get()), speed)
            sample_seconds, _ = estimate_project(self._project.with_mode(self.mode_var.get()), speed, sample=True)
        cost = estimate_audio_cost(seconds)
        if PROVIDERS[self.provider_var.get()] == "gemini":
            self.cost_var.set(f"Estimate: sample {sample_seconds / 60:.1f} min / ${estimate_audio_cost(sample_seconds):.2f}; "
                              f"complete {seconds / 60:.0f} min / ${cost:.2f} audio output. "
                              "Excludes text input and regenerated attempts; not a spending limit.")
        else:
            self.cost_var.set(f"Estimated full audio: {seconds / 60:.0f} min · Andrew sends text to Microsoft; internet required.")

    def _update_controls(self):
        busy = bool(self._busy)
        for widget in self._job_controls + self._radios:
            widget.configure(state="disabled" if busy else "normal")
        for widget in self._combos:
            widget.configure(state="disabled" if busy else "readonly")
        self.title_entry.configure(state="disabled" if busy else "normal")
        self.source_text.configure(state="disabled" if busy else "normal")
        self.note_list.configure(state="disabled" if busy else "normal")
        self.note_text.configure(state="normal" if not busy and self._note_source_id else "disabled")
        self.pause_button.configure(state="normal" if self._playing else "disabled",
                                    text="Resume" if self._pause.is_set() else "Pause")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.error_button.configure(state="normal" if self._last_error_detail else "disabled")

    def _mode_changed(self):
        if self._project:
            self._project = self._project.with_mode(self.mode_var.get())
        self._update_metrics()

    def _provider_changed(self, _event=None):
        values = tuple(key.split(" - ")[0] for key in GEMINI_VOICES) if PROVIDERS[self.provider_var.get()] == "gemini" else (ANDREW_LABEL.split(" - ")[0],)
        self.voice_combo.configure(values=values)
        self.voice_var.set(values[0])
        self._update_metrics()

    def _refresh_notes(self):
        self.note_list.delete(0, "end")
        self._note_source_id = None
        self.note_text.configure(state="normal")
        self.note_text.delete("1.0", "end")
        self.note_text.edit_modified(False)
        self.note_text.configure(state="disabled")
        if self._project:
            completed = self._project.completed_source_ids
            for index, block in enumerate(self._project.blocks):
                state = "ready" if block.source_id in completed else "pending"
                label = (" ".join(block.text.split())[:28]
                         if block.section.startswith("Part ") else block.section[:28])
                self.note_list.insert("end", f"{index + 1}. {label} — {state}")

    def _select_note(self, _event=None):
        if self._busy or not self._project:
            return
        selection = self.note_list.curselection()
        if not selection:
            return
        try:
            self._apply_note(quiet=True)
        except TextbookError as exc:
            messagebox.showerror("Teaching note", str(exc), parent=self)
            self.note_list.selection_clear(0, "end")
            if self._note_source_id:
                for index, block in enumerate(self._project.blocks):
                    if block.source_id == self._note_source_id:
                        self.note_list.selection_set(index)
                        break
            return
        block = self._project.blocks[selection[0]]
        self._note_source_id = block.source_id
        notes = {item.source_id: item.text for item in self._project.explanations}
        self.note_text.configure(state="normal")
        self.note_text.delete("1.0", "end")
        self.note_text.insert("1.0", notes.get(block.source_id, ""))
        self.note_text.edit_modified(False)
        self.status_var.set(f"Part {selection[0] + 1}: {block.text.strip()[:130]}…")

    def _apply_note(self, quiet=False):
        if not self._project or not self._note_source_id:
            return
        text = self.note_text.get("1.0", "end-1c")
        existing = {item.source_id: item.text for item in self._project.explanations}
        if text.strip() == existing.get(self._note_source_id, "") and quiet:
            return
        try:
            self._project = self._project.with_explanation(self._note_source_id, text)
        except TextbookError as exc:
            if quiet:
                raise
            messagebox.showerror("Teaching note", str(exc), parent=self)
            return
        if not quiet:
            self.status_var.set("Teaching note applied. Save the project to keep it.")
        self._update_metrics()

    def _set_source(self, text, title, notes=()):
        self._project = None
        self._project_path = None
        self._extraction_notes = notes
        self.title_var.set(title)
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", text)
        self.source_text.edit_reset()
        self.source_text.edit_modified(False)
        self._refresh_notes()
        self._update_metrics()
        self.notebook.select(self.source_page)

    def _current_source(self):
        if not self._confirm_discard():
            return
        text, title = self.app._ai_source_snapshot()
        self._set_source(text, title)
        self.status_var.set("Reader text copied. Review it before generating audio.")

    def _import(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(parent=self, title="Import textbook source",
                filetypes=[("Text and PDF", "*.txt *.md *.pdf"), ("All files", "*.*")])
        if path:
            lock = getattr(self.app, "_ocr_load_lock", None)
            self._start_job("import", lambda cancel, _: import_source_file(path, cancel, lock),
                            "Importing the source…")

    def _generate(self):
        try:
            project = self._snapshot_project()
            self.project_dir.mkdir(parents=True, exist_ok=True)
            if self._project_path is None:
                self._project_path = self.project_dir / f"{_safe_filename(project.title)}-{uuid.uuid4().hex[:8]}.json"
            save_project(project, self._project_path)
            checkpoint_path = self._project_path
        except (OSError, ValueError) as exc:
            messagebox.showerror("Prepare textbook", str(exc), parent=self)
            return
        def worker(cancel, job):
            def checkpoint(updated):
                save_project(updated, checkpoint_path)
                self._events.put((job, "checkpoint", None, updated))
            try:
                return generate_explanations(project, cancel=cancel,
                    progress=lambda text: self._progress(job, text), checkpoint=checkpoint)
            except TextbookCancelled as exc:
                checkpoint(exc.project)
                raise AIStudioCancelled("Stopped; completed notes saved.") from None
        self._start_job("generate", worker, "Generating explanations with your Codex subscription…")

    def _show_job_error(self, detail):
        # Backend diagnostics contain only fixed, allowlisted messages. Also
        # redact a session key defensively before retaining details in memory.
        detail = str(detail)
        if self._api_key:
            detail = detail.replace(self._api_key, "[redacted]")
        self._last_error_detail = detail[:4000]
        self.status_var.set("This step failed. Last error has the details; Check access tests key and model metadata without generating audio.")
        self.error_button.configure(state="normal")
        self._show_last_error()

    def _show_last_error(self):
        if self._last_error_detail:
            messagebox.showerror("Textbook Studio", self._last_error_detail, parent=self)

    def _check_access(self):
        if self._busy or self._closed:
            return
        try:
            key = self._api_key or self._credential_store.load()
            if not key and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                self.status_var.set("Enter a Gemini key to check access. No audio will be generated.")
                self._key_dialog(on_ready=self._check_access)
                return
        except GeminiError as exc:
            self._show_job_error(exc)
            return
        self._start_job("gemini_access", lambda cancel, _job:
            GeminiEngine(api_key=key).check_access(cancel_event=cancel),
            "Checking the Gemini key and model metadata without generating audio…")

    def _key_dialog(self, on_ready=None):
        if self._key_window is not None and self._key_window.winfo_exists():
            self._key_window.lift()
            return
        # Guard the native listener, queued events, fallback and manual read.
        # Do not toggle Auto-read: its persisted preference belongs to the user.
        self.app._begin_sensitive_clipboard_input()
        dialog = key_var = None
        cleaned = False

        def cleanup(event=None):
            nonlocal cleaned
            if cleaned or (event is not None and event.widget is not dialog):
                return
            cleaned = True
            try:
                if key_var is not None:
                    key_var.set("")
            finally:
                self._key_window = None
                self.app._end_sensitive_clipboard_input()

        def finish(accepted=False):
            try:
                if dialog is not None:
                    dialog.destroy()
            finally:
                cleanup()
            if accepted and on_ready is not None and not self._closed:
                self.after_idle(on_ready)

        try:
            dialog = tk.Toplevel(self)
            self._key_window = dialog
            dialog.bind("<Destroy>", cleanup, add="+")
            dialog.title("Gemini API key")
            dialog.configure(bg=self.palette["background"])
            dialog.transient(self)
            dialog.resizable(False, False)
            shell = self._frame(dialog)
            shell.pack(padx=18, pady=16)
            self._label(shell, "Enter your key here. It is never saved in a textbook project.",
                        wraplength=510, justify="left").pack(anchor="w")
            key_var = tk.StringVar(dialog)
            entry = tk.Entry(shell, textvariable=key_var, show="●", width=53, font=("Segoe UI", 11))
            entry.pack(fill="x", pady=12, ipady=5)
            remember = tk.BooleanVar(dialog, value=False)
            tk.Checkbutton(shell, text="Remember on this Windows account (encrypted)", variable=remember,
                           bg=self.palette["background"], font=("Segoe UI", 10)).pack(anchor="w")
            self._label(shell, "Clipboard reading is paused while this dialog is open. Copies made here will not be replayed.",
                        wraplength=510, justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 0))
            self._label(shell, "Gemini API billing is separate from ChatGPT. Free availability depends on your Google project.",
                        wraplength=510, justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=10)
            actions = self._frame(shell)
            actions.pack(fill="x")

            def use_key():
                key = key_var.get().strip()
                if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    messagebox.showerror("Gemini key", "Paste a valid API key.", parent=dialog)
                    return
                try:
                    if remember.get():
                        self._credential_store.save(key)
                except GeminiError as exc:
                    messagebox.showerror("Gemini key", str(exc), parent=dialog)
                    return
                self._api_key = key
                self.status_var.set("Gemini key ready. " + ("Saved with Windows encryption." if remember.get() else "Used for this window only."))
                finish(accepted=True)

            def remove():
                try:
                    self._credential_store.remove()
                    self._api_key = None
                    self.status_var.set("Saved Gemini key removed; session key cleared.")
                    finish()
                except GeminiError as exc:
                    messagebox.showerror("Gemini key", str(exc), parent=dialog)

            self._button(actions, "Use key", use_key, primary=True, job_control=False).pack(side="left")
            self._button(actions, "Remove saved key", remove, job_control=False).pack(side="left", padx=8)
            self._button(actions, "Cancel", finish, job_control=False).pack(side="right")
            dialog.protocol("WM_DELETE_WINDOW", finish)
            dialog.grab_set()
            entry.focus_set()
        except Exception:
            finish()
            raise

    def _audio_snapshot(self, sample=True):
        try:
            project = self._snapshot_project()
            if project.mode == "explain" and not sample and not project.explanations_complete:
                raise TextbookError("Generate / resume explanations in Teaching notes before exporting this mode. Your complete source is already preserved.")
            provider = PROVIDERS[self.provider_var.get()]
            voice = next(key for key in GEMINI_VOICES if key.split(" - ")[0] == self.voice_var.get()) if provider == "gemini" else ANDREW_LABEL
            key = None
            if provider == "gemini":
                key = self._api_key or self._credential_store.load()
                if not key and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                    self._key_dialog()
                    self.status_var.set("Enter a Gemini key, then choose Listen to sample again.")
                    return None
            speed, volume = float(self.app.speed_var.get()), float(self.app.vol_var.get())
            return project, provider, voice, key, speed, volume
        except (ValueError, GeminiError, StopIteration) as exc:
            messagebox.showerror("Prepare textbook audio", str(exc), parent=self)
            return None

    def _render(self, snapshot, cancel, job, sample=True, destination=None):
        from textbook_audio import render_textbook_audio
        project, provider, voice, key, speed, volume = snapshot
        path = str(destination or Path(self._temp_dir) / f"b{job:07x}.wav")
        metadata = render_textbook_audio(project, path, voice_label=voice, provider=provider,
            speed=speed, volume=volume, cancel_event=cancel,
            progress=lambda done, total, role: self._progress(job, f"Preparing audio {done}/{total} · {role}…"),
            cache_dir=self.data_dir / "textbook_audio_cache", api_key=key, sample=sample)
        return path, metadata

    def _confirm_complete_audio(self, snapshot):
        project, provider, _, _, speed, _ = snapshot
        if provider != "gemini":
            return True
        seconds, cost = estimate_project(project, speed)
        return messagebox.askokcancel("Complete textbook audio",
            f"About {seconds / 60:.0f} minutes, approximately ${cost:.2f} for audio output at current standard pricing.\n\n"
            "Free quotas depend on your Google project. Text input, retries and tax are additional; this estimate is not a spending limit. "
            "This sends the full spoken text to Google. Continue?", parent=self)

    def _play_complete(self):
        self._play(sample=False)

    def _play(self, sample=True):
        if self._busy:
            return
        snapshot = self._audio_snapshot(sample=sample)
        if snapshot is None:
            return
        if not sample and not self._confirm_complete_audio(snapshot):
            return
        def worker(cancel, job):
            path, metadata = self._render(snapshot, cancel, job, sample=sample)
            ready = threading.Event()
            self._events.put((job, "ready_to_play", None, ready))
            while not ready.wait(0.05):
                if cancel.is_set():
                    raise AIStudioCancelled("Stopped.")
            if cancel.is_set():
                raise AIStudioCancelled("Stopped.")
            self._events.put((job, "playing", None, None))
            self._player.play_blocking(path, cancel, self._pause)
            return metadata
        self._start_job("play" if sample else "play_complete", worker,
                        "Preparing a sample from the first two source parts…" if sample else "Preparing complete audio before playback…")

    def _export_wav(self):
        snapshot = self._audio_snapshot(sample=False)
        if snapshot is None:
            return
        project = snapshot[0]
        if not self._confirm_complete_audio(snapshot):
            return
        path = filedialog.asksaveasfilename(parent=self, title="Export complete textbook WAV",
            defaultextension=".wav", filetypes=[("WAV audio", "*.wav")],
            initialfile=_safe_filename(project.title) + (" — explained" if project.mode == "explain" else "") + ".wav")
        if path:
            self._start_job("export", lambda cancel, job: self._render(snapshot, cancel, job,
                            sample=False, destination=path), "Preparing the complete audio…")

    def _handle_studio_event(self, event, _kind, result):
        # Runs on Tk's thread in the shared worker event drain.
        if event == "checkpoint":
            self._project = result
            self._last_saved = self._document_fingerprint()
            self._refresh_notes()
            self._update_metrics()

    def _completed(self, kind, result):
        if kind == "gemini_access":
            self.status_var.set(result["message"])
        elif kind == "import":
            self._set_source(*result)
            self.status_var.set("Source imported. " + (" ".join(result[2]) if result[2] else "Review it before listening."))
        elif kind == "generate":
            self._project = result
            try:
                save_project(result, self._project_path)
            except OSError as exc:
                self._refresh_notes()
                self._update_metrics()
                self.status_var.set("Explanations are ready, but the final project save failed. Choose Save project.")
                messagebox.showerror("Save textbook project", str(exc), parent=self)
                return
            self._refresh_notes()
            self._last_saved = self._document_fingerprint()
            self._update_metrics()
            self.notebook.select(self.notes_page)
            self.status_var.set("Explanations saved. Review the teaching notes; the source is unchanged.")
        elif kind == "play":
            self.status_var.set("Sample finished. Check pronunciation and delivery before exporting the chapter.")
        elif kind == "play_complete":
            self.status_var.set("Complete playback finished.")
        elif kind == "export":
            path, metadata = result
            seconds = metadata.get("duration_seconds", 0)
            self.status_var.set(f"Saved complete WAV ({seconds / 60:.1f} minutes): {path}")

    def _save(self):
        if self._busy:
            return False
        try:
            project = self._snapshot_project()
            self.project_dir.mkdir(parents=True, exist_ok=True)
            path = self._project_path or filedialog.asksaveasfilename(parent=self,
                title="Save textbook project", initialdir=str(self.project_dir),
                initialfile=_safe_filename(project.title) + ".json", defaultextension=".json",
                filetypes=[("Piper Reader textbook project", "*.json")])
            if not path:
                return False
            save_project(project, path)
            self._project_path = Path(path)
            self._last_saved = self._document_fingerprint()
            self.status_var.set("Project saved locally: " + str(path))
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror("Save textbook project", str(exc), parent=self)
            return False

    def _confirm_discard(self):
        try:
            self._apply_note(quiet=True)
        except TextbookError as exc:
            messagebox.showerror("Teaching note", str(exc), parent=self)
            return False
        if self._document_fingerprint() == self._last_saved:
            return True
        answer = messagebox.askyesnocancel("Save textbook project?",
            "Save your source and teaching notes before continuing?", parent=self)
        return False if answer is None else self._save() if answer else True

    def _load(self):
        if self._busy or not self._confirm_discard():
            return
        path = filedialog.askopenfilename(parent=self, title="Open textbook project",
            initialdir=str(self.project_dir), filetypes=[("Piper Reader textbook projects", "*.json")])
        if not path:
            return
        try:
            project = load_project(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open textbook project", str(exc), parent=self)
            return
        self._set_source(project.source_text, project.title, project.extraction_notes)
        self._project, self._project_path = project, Path(path)
        self.mode_var.set(project.mode)
        self._refresh_notes()
        self._last_saved = self._document_fingerprint()
        self._update_metrics()
        self.status_var.set("Project loaded. " + " ".join(project.extraction_notes))

    def close(self, force=False):
        super().close(force=force)
        if self._closed:
            self._api_key = None
