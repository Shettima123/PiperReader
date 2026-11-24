"""Offline regressions for source preservation, UI jobs and shared speakers."""

import importlib.machinery
from pathlib import Path
import queue
import tempfile
import threading
from collections import deque
import unittest
from unittest import mock

from ai_studio_ui import AIStudioWindow
from textbook_core import TextbookProject, load_project, save_project
import textbook_studio as UI


APP = importlib.machinery.SourceFileLoader(
    "piper_reader_textbook_tests", str(Path(__file__).resolve().parents[1] / "piper_reader.pyw")
).load_module()


class SourcePreservationTests(unittest.TestCase):
    def test_mode_and_title_changes_preserve_exact_source_and_explanations(self):
        source = "  Exact source.\n\nAn equation: ΔQ / ΔP.\n"
        project = TextbookProject.create(source)
        project = project.with_explanation(project.blocks[0].source_id, "A separate explanation.")
        updated = UI.project_for_editor(project, source, "New title", "explain")
        self.assertEqual(updated.source_text, source)
        self.assertEqual(updated.blocks, project.blocks)
        self.assertEqual(updated.explanations, project.explanations)
        self.assertEqual(updated.with_mode("read").explanations, project.explanations)
        self.assertTrue(updated.coverage_report().complete)

    def test_edited_source_invalidates_old_notes_and_identity(self):
        original = TextbookProject.create("Before.")
        original = original.with_explanation(original.blocks[0].source_id, "Old teaching note.")
        changed = UI.project_for_editor(original, "After.", "Chapter", "explain")
        self.assertEqual(changed.source_text, "After.")
        self.assertNotEqual(changed.source_sha256, original.source_sha256)
        self.assertFalse(changed.explanations)

    def test_import_and_project_round_trip_do_not_trim_or_rewrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            text = "  Exact text with accents: café.\n\n最後\n"
            path.write_text(text, encoding="utf-8")
            imported, title, notes = UI.import_source_file(path)
            self.assertEqual(imported, text)
            project = TextbookProject.create(imported, title, extraction_notes=notes)
            output = Path(directory) / "project.json"
            save_project(project, output)
            self.assertEqual(load_project(output).source_text, text)
            self.assertNotIn("api_key", output.read_text(encoding="utf-8"))

    def test_sample_estimate_uses_only_first_two_source_parts_and_their_notes(self):
        project = TextbookProject.create("Some source words. " * 300, max_block_chars=200, mode="explain")
        for block in project.blocks:
            project = project.with_explanation(block.source_id, "Explain this carefully.")
        sample, cost = UI.estimate_project(project, sample=True)
        complete, full_cost = UI.estimate_project(project)
        self.assertGreater(sample, 0)
        self.assertLess(sample, complete)
        self.assertLess(cost, full_cost)


class StudioStateTests(unittest.TestCase):
    def bare(self):
        studio = object.__new__(UI.TextbookStudioWindow)
        studio._project = TextbookProject.create("Unchanged source.")
        studio._note_source_id = studio._project.blocks[0].source_id
        studio._busy = None
        studio._closed = False
        studio._job_id = 3
        studio._events = queue.Queue()
        studio._cancel = threading.Event()
        studio._pause = threading.Event()
        studio._player = mock.Mock()
        studio.pause_button = mock.Mock()
        studio.note_text = mock.Mock()
        studio.note_list = mock.Mock()
        studio._update_metrics = mock.Mock()
        studio._refresh_notes = mock.Mock()
        studio._document_fingerprint = mock.Mock(return_value="current")
        studio.status_var = mock.Mock()
        studio._api_key = None
        studio._key_window = None
        studio._last_error_detail = None
        studio._credential_store = mock.Mock()
        studio._credential_store.load.return_value = None
        studio.error_button = mock.Mock()
        return studio

    def test_invalid_note_does_not_escape_close_prompt_or_lose_text(self):
        studio = self.bare()
        studio.note_text.get.return_value = "X" * 1601
        with mock.patch.object(UI.messagebox, "showerror") as error:
            self.assertFalse(studio._confirm_discard())
        error.assert_called_once()
        studio.note_text.delete.assert_not_called()
        self.assertFalse(studio._project.explanations)

    def test_invalid_note_keeps_selection_and_edit_instead_of_loading_other_note(self):
        studio = self.bare()
        studio.note_text.get.return_value = "X" * 1601
        studio.note_list.curselection.return_value = (0,)
        with mock.patch.object(UI.messagebox, "showerror"):
            studio._select_note()
        studio.note_text.delete.assert_not_called()
        studio.note_list.selection_set.assert_called_once_with(0)

    def test_checkpoint_updates_project_without_replacing_source_editor(self):
        studio = self.bare()
        studio.source_text = mock.Mock()
        updated = studio._project.with_explanation(studio._note_source_id, "Additional teaching.")
        studio._handle_studio_event("checkpoint", None, updated)
        self.assertIs(studio._project, updated)
        self.assertEqual(studio._last_saved, "current")
        studio.source_text.delete.assert_not_called()
        studio.source_text.insert.assert_not_called()

    def test_stop_cancels_only_textbook_work_and_player(self):
        studio = self.bare()
        studio.app = mock.Mock()
        studio._busy = "generate"
        studio.stop()
        self.assertTrue(studio._cancel.is_set())
        studio._player.stop.assert_called_once()
        studio.app.stop.assert_not_called()

    def test_complete_explain_audio_requires_complete_notes(self):
        studio = self.bare()
        studio._snapshot_project = mock.Mock(return_value=studio._project.with_mode("explain"))
        with mock.patch.object(UI.messagebox, "showerror") as error:
            self.assertIsNone(studio._audio_snapshot(sample=False))
        self.assertIn("Generate / resume", error.call_args.args[1])

    def test_access_check_missing_key_opens_dialog_then_rechecks_after_accept(self):
        studio = self.bare()
        studio._start_job = mock.Mock()
        studio._key_dialog = mock.Mock()
        with mock.patch.dict(UI.os.environ, {}, clear=True):
            studio._check_access()
        studio._start_job.assert_not_called()
        studio._key_dialog.assert_called_once_with(on_ready=studio._check_access)

    def test_access_check_uses_async_cancellable_job_without_synthesis(self):
        studio = self.bare()
        studio._api_key = "dummy-key"
        studio._update_controls = mock.Mock()
        started = threading.Event()

        def check(cancel_event):
            started.set()
            self.assertTrue(cancel_event.wait(2))
            raise UI.AIStudioCancelled("Stopped.")

        with mock.patch.object(UI, "GeminiEngine") as factory:
            factory.return_value.check_access.side_effect = check
            studio._check_access()
            self.assertTrue(started.wait(2))
            self.assertEqual(studio._busy, "gemini_access")
            studio.stop()
            studio._worker.join(2)
            self.assertFalse(studio._worker.is_alive())
            factory.assert_called_once_with(api_key="dummy-key")
            factory.return_value.synth_to_wav.assert_not_called()
        self.assertEqual(studio._events.get_nowait()[1:3], ("cancelled", "gemini_access"))

    def test_access_result_preserves_its_limited_claims(self):
        studio = self.bare()
        message = "Key accepted. Model metadata available. Quota, billing and speech generation remain unverified."
        studio._completed("gemini_access", {"message": message})
        studio.status_var.set.assert_called_once_with(message)

    def test_worker_error_hook_retains_safe_details_and_correct_window_title(self):
        studio = self.bare()
        studio._api_key = "dummy-private-key"
        studio._playing = studio._audio_reserved = False
        studio._close_when_idle = False
        studio._update_controls = mock.Mock()
        studio._grant_audio_when_reader_idle = mock.Mock()
        studio.after = mock.Mock()
        studio._events.put((3, "error", "gemini_access", "HTTP 403: dummy-private-key"))
        with mock.patch.object(UI.messagebox, "showerror") as error:
            studio._drain()
        error.assert_called_once_with("Textbook Studio", "HTTP 403: [redacted]", parent=studio)
        self.assertEqual(studio._last_error_detail, "HTTP 403: [redacted]")
        self.assertIsNone(studio._busy)
        self.assertFalse(studio._audio_reserved)

    def key_dialog_mocks(self, studio):
        studio.app = mock.Mock()
        studio.palette = UI.DEFAULT_PALETTE
        studio._frame = mock.Mock()
        studio._label = mock.Mock()
        studio.after_idle = mock.Mock()
        callbacks, bindings, protocols = {}, {}, {}
        dialog = mock.Mock()
        dialog.bind.side_effect = lambda name, fn, **kwargs: bindings.update({name: fn})
        dialog.protocol.side_effect = lambda name, fn: protocols.update({name: fn})
        dialog.destroy.side_effect = lambda: bindings["<Destroy>"](mock.Mock(widget=dialog))
        studio._button = lambda _, name, fn, **kwargs: (callbacks.update({name: fn}) or mock.Mock())
        return dialog, callbacks, bindings, protocols

    def test_key_dialog_releases_guard_on_cancel_window_close_destroy_and_use(self):
        for action in ("Cancel", "WM_DELETE_WINDOW", "destroy", "Use key", "Remove saved key"):
            with self.subTest(action=action):
                studio = self.bare()
                dialog, callbacks, bindings, protocols = self.key_dialog_mocks(studio)
                value = mock.Mock()
                value.get.return_value = "dummy-key"
                remember = mock.Mock()
                remember.get.return_value = False
                ready = mock.Mock()
                with mock.patch.object(UI.tk, "Toplevel", return_value=dialog), \
                     mock.patch.object(UI.tk, "StringVar", return_value=value), \
                     mock.patch.object(UI.tk, "BooleanVar", return_value=remember), \
                     mock.patch.object(UI.tk, "Entry"), mock.patch.object(UI.tk, "Checkbutton"):
                    studio._key_dialog(on_ready=ready)
                    studio.app._begin_sensitive_clipboard_input.assert_called_once()
                    studio.app._end_sensitive_clipboard_input.assert_not_called()
                    if action == "WM_DELETE_WINDOW":
                        protocols[action]()
                    elif action == "destroy":
                        bindings["<Destroy>"](mock.Mock(widget=dialog))
                    else:
                        callbacks[action]()
                    studio.app._end_sensitive_clipboard_input.assert_called_once()
                    value.set.assert_called_once_with("")
                    self.assertIsNone(studio._key_window)
                    if action == "Use key":
                        self.assertEqual(studio._api_key, "dummy-key")
                        studio.after_idle.assert_called_once_with(ready)
                    else:
                        studio.after_idle.assert_not_called()

    def test_key_dialog_creation_failure_releases_guard(self):
        studio = self.bare()
        studio.app = mock.Mock()
        with mock.patch.object(UI.tk, "Toplevel", side_effect=RuntimeError("Cannot create window")):
            with self.assertRaises(RuntimeError):
                studio._key_dialog()
        studio.app._end_sensitive_clipboard_input.assert_called_once()

    def test_key_save_error_keeps_guard_until_dialog_is_closed(self):
        studio = self.bare()
        dialog, callbacks, bindings, protocols = self.key_dialog_mocks(studio)
        value = mock.Mock()
        value.get.return_value = "dummy-key"
        remember = mock.Mock()
        remember.get.return_value = True
        studio._credential_store.save.side_effect = UI.GeminiError("Encrypted save failed.")
        with mock.patch.object(UI.tk, "Toplevel", return_value=dialog), \
             mock.patch.object(UI.tk, "StringVar", return_value=value), \
             mock.patch.object(UI.tk, "BooleanVar", return_value=remember), \
             mock.patch.object(UI.tk, "Entry"), mock.patch.object(UI.tk, "Checkbutton"), \
             mock.patch.object(UI.messagebox, "showerror"):
            studio._key_dialog()
            callbacks["Use key"]()
            self.assertIsNone(studio._api_key)
            studio.app._end_sensitive_clipboard_input.assert_not_called()
            callbacks["Cancel"]()
        studio.app._end_sensitive_clipboard_input.assert_called_once()


class SensitiveClipboardTests(unittest.TestCase):
    def bare(self):
        app = object.__new__(APP.ReaderApp)
        app._clipboard_sensitive_input = threading.Event()
        app._clipboard_sensitive_depth = 0
        app._clipboard_sensitive_sequence = None
        app._clipboard_sensitive_baseline = False
        app._clipboard_events = queue.Queue()
        app._clipboard_pending_updates = deque()
        app._studio_clipboard_queue = deque()
        app._last_clip = "Earlier text"
        app._last_clip_sequence = 10
        app._clipboard_sequence_number = mock.Mock(return_value=11)
        app._clipboard_listener_ready = threading.Event()
        app._shutdown_event = threading.Event()
        app._ai_studio_owns_audio = mock.Mock(return_value=False)
        app._probe_runtime_reprime = mock.Mock()
        app._read_windows_clipboard_text = mock.Mock(return_value=(True, "dummy-key"))
        app._speak_clipboard_text = mock.Mock()
        app.auto_var = mock.Mock()
        app.auto_var.get.return_value = True
        app.root = mock.Mock()
        app.root.clipboard_get.return_value = "dummy-key"
        app.speak = mock.Mock()
        app.set_status = mock.Mock()
        return app

    def test_guard_blocks_native_fallback_queue_and_manual_reads_without_changing_preference(self):
        app = self.bare()
        app._begin_sensitive_clipboard_input()
        app._clipboard_events.put((11, "dummy-key", True))
        app._capture_clipboard_update(1)
        app._poll_clipboard()
        app._accept_clipboard_update(11, "dummy-key")
        app._queue_clipboard_text("dummy-key")
        app.read_clipboard()
        app._read_windows_clipboard_text.assert_not_called()
        app.root.clipboard_get.assert_not_called()
        app.speak.assert_not_called()
        app._speak_clipboard_text.assert_not_called()
        app.auto_var.set.assert_not_called()
        self.assertTrue(app._clipboard_events.empty())
        self.assertFalse(app._studio_clipboard_queue)
        self.assertIsNone(app._last_clip)

    def test_late_notifications_and_manual_read_cannot_replay_closing_clipboard(self):
        app = self.bare()
        app._begin_sensitive_clipboard_input()
        app._end_sensitive_clipboard_input()
        app._clipboard_events.put((11, "dummy-key", True))
        app._clipboard_events.put((10, "older copy", True))
        app._poll_clipboard()
        app.read_clipboard()
        app._capture_clipboard_update(1)
        app.root.clipboard_get.assert_not_called()
        app._read_windows_clipboard_text.assert_not_called()
        app.speak.assert_not_called()
        app._speak_clipboard_text.assert_not_called()
        self.assertIsNone(app._last_clip)
        app._clipboard_sequence_number.return_value = 12
        app.root.clipboard_get.return_value = "New chapter text"
        app._poll_clipboard()
        app._speak_clipboard_text.assert_called_once_with("New chapter text")
        app.read_clipboard()
        app.speak.assert_called_once_with("New chapter text")
        app.auto_var.set.assert_not_called()

    def test_guard_handles_nested_dialogs_and_wrapping_windows_sequences(self):
        app = self.bare()
        app._clipboard_sequence_number.return_value = 0xFFFFFFFF
        app._begin_sensitive_clipboard_input()
        app._begin_sensitive_clipboard_input()
        app._end_sensitive_clipboard_input()
        self.assertTrue(app._sensitive_clipboard_input_active())
        app._end_sensitive_clipboard_input()
        self.assertFalse(app._sensitive_clipboard_input_active())
        self.assertTrue(app._clipboard_sequence_is_sensitive(0xFFFFFFFF))
        self.assertFalse(app._clipboard_sequence_is_sensitive(0))

    def test_read_in_flight_is_discarded_if_dialog_opens_before_it_finishes(self):
        app = self.bare()
        def capture(_hwnd):
            app._begin_sensitive_clipboard_input()
            return True, "dummy-key"
        app._read_windows_clipboard_text.side_effect = capture
        app._capture_clipboard_update(1)
        self.assertTrue(app._clipboard_events.empty())


class MultipleStudioIntegrationTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(APP.ReaderApp)
        app.ai_studio = mock.Mock(owns_audio=False)
        app.textbook_studio = mock.Mock(owns_audio=False)
        app.ai_studio.winfo_exists.return_value = True
        app.textbook_studio.winfo_exists.return_value = True
        app._studio_audio_owner = None
        return app

    def test_ready_studios_take_turns_and_preparing_studio_never_owns_audio(self):
        app = self.make_app()
        self.assertFalse(app._ai_studio_owns_audio())
        self.assertTrue(app._try_reserve_studio_audio(app.ai_studio))
        app.ai_studio.owns_audio = True
        self.assertFalse(app._try_reserve_studio_audio(app.textbook_studio))
        self.assertTrue(app._ai_studio_owns_audio())
        app.ai_studio.owns_audio = False
        self.assertTrue(app._try_reserve_studio_audio(app.textbook_studio))
        app.textbook_studio.owns_audio = True
        self.assertTrue(app._ai_studio_owns_audio())
        self.assertFalse(app._try_reserve_studio_audio(app.ai_studio))

    def test_closed_owner_does_not_leave_speakers_reserved(self):
        app = self.make_app()
        app._studio_audio_owner = app.ai_studio
        app.ai_studio.owns_audio = True
        app.ai_studio.winfo_exists.return_value = False
        self.assertTrue(app._try_reserve_studio_audio(app.textbook_studio))

    def test_waiting_studio_gate_never_starts_over_another_studio(self):
        app = self.make_app()
        app._reader_audio_is_busy = mock.Mock(return_value=False)
        app._studio_audio_owner = app.ai_studio
        app.ai_studio.owns_audio = True
        studio = object.__new__(AIStudioWindow)
        app.textbook_studio = studio
        studio.winfo_exists = mock.Mock(return_value=True)
        studio.app = app
        studio._closed = False
        studio._audio_reserved = False
        studio._cancel = threading.Event()
        studio._playback_gate = threading.Event()
        studio._grant_audio_when_reader_idle()
        self.assertFalse(studio._playback_gate.is_set())
        app.ai_studio.owns_audio = False
        app._reader_audio_is_busy.return_value = True
        studio._grant_audio_when_reader_idle()
        self.assertTrue(studio.owns_audio)
        self.assertFalse(studio._playback_gate.is_set())
        app._reader_audio_is_busy.return_value = False
        studio._grant_audio_when_reader_idle()
        self.assertTrue(studio._playback_gate.is_set())

    def test_reader_exit_cancellation_checks_textbook_after_ai_window_closed(self):
        app = self.make_app()
        app._exiting = False
        app.root = mock.Mock()
        app.ai_studio.close.side_effect = lambda: setattr(app.ai_studio.winfo_exists, "return_value", False)
        app.textbook_studio._close_when_idle = True
        app._exit_app()
        app.ai_studio.close.assert_called_once()
        app.textbook_studio.close.assert_called_once()
        self.assertIs(app._exit_closing_studio, app.textbook_studio)
        self.assertTrue(app._exit_waiting_for_studio)
        app.root.destroy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
