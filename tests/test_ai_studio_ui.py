"""Offline checks for lesson persistence, audio cache and worker isolation."""

import copy
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import ai_studio_ui as UI
from ai_studio import AIStudioCancelled
from edge_engine import AVA_LABEL, SONIA_LABEL


def lesson_document():
    return {
        "format": UI.LESSON_FORMAT, "version": 1,
        "source": {"title": "A lesson — résumé", "text": "Source text: a < b.\n第二段"},
        "options": {"style": "podcast", "duration_minutes": 10,
                    "audience": "General audience"},
        "script": "[Host]\nWelcome.\n\n[Guest]\nLet’s explain it.\n[Pause 3s]",
        "voices": dict(UI.DEFAULT_VOICES), "notes": ["One example is illustrative."],
    }


class LessonPersistenceTests(unittest.TestCase):
    def test_unicode_project_round_trip_preserves_source_and_editable_script(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lesson.json"
            document = lesson_document()
            UI.atomic_write_text(path, json.dumps(document, ensure_ascii=False))
            self.assertEqual(UI.read_lesson_file(path), document)

    def test_interrupted_atomic_save_preserves_previous_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lesson.json"
            path.write_text("previous lesson", encoding="utf-8")
            with mock.patch.object(UI.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    UI.atomic_write_text(path, "new lesson")
            self.assertEqual(path.read_text(encoding="utf-8"), "previous lesson")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_draft_with_unfinished_speaker_label_can_be_saved(self):
        document = lesson_document()
        document["script"] = "[Ho"
        self.assertEqual(UI.validate_lesson_document(document)["script"], "[Ho")

    def test_invalid_project_options_are_rejected_before_editor_update(self):
        mutations = (
            lambda d: d.update(version=True),
            lambda d: d.update(format="Other app"),
            lambda d: d["options"].update(style="unknown"),
            lambda d: d["options"].update(duration_minutes=True),
            lambda d: d["options"].update(audience=["Advanced"]),
            lambda d: d["source"].update(text={"not": "text"}),
            lambda d: d["voices"].update(Host="unavailable voice"),
            lambda d: d["voices"].update(Host=["invalid"]),
            lambda d: d.update(notes="wrong type"),
        )
        for mutate in mutations:
            document = lesson_document()
            mutate(document)
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    UI.validate_lesson_document(document)

    def test_unknown_fields_do_not_become_auth_or_execution_settings(self):
        document = lesson_document()
        document.update(api_key="not-a-real-key", executable="untrusted.exe")
        result = UI.validate_lesson_document(document)
        self.assertNotIn("api_key", result)
        self.assertNotIn("executable", result)

    def test_malformed_and_oversized_local_files_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lesson.json"
            path.write_bytes(b"\xffinvalid")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                UI.read_lesson_file(path)
            path.write_bytes(b" " * 101)
            with mock.patch.object(UI, "MAX_LESSON_BYTES", 100):
                with self.assertRaisesRegex(ValueError, "too large"):
                    UI.read_lesson_file(path)


class AudioCacheTests(unittest.TestCase):
    def test_short_audio_names_preserve_full_cache_identity_and_reuse(self):
        with tempfile.TemporaryDirectory(prefix="piperreader_ai_preview_") as parent:
            directory = Path(parent) / "ai_studio_k31fdeug"
            directory.mkdir()
            studio = object.__new__(UI.AIStudioWindow)
            studio._temp_dir = str(directory)
            studio._cache_metadata = {}
            studio._cache_paths = {}
            studio.app = mock.Mock(engines={"edge": object()})
            studio._progress = mock.Mock()
            snapshot = ([{"speaker": "Host", "text": "Hello.", "pause_after": 0}],
                        dict(UI.DEFAULT_VOICES), 1.0, 1.0)
            # Full identities remain distinct even when their prefixes match.
            first_key, second_key = "a" * 63 + "1", "a" * 63 + "2"
            def render(_segments, path, _voices, **kwargs):
                Path(path).write_bytes(b"complete mocked audio")
                return {"duration_seconds": 1}
            with mock.patch.object(UI, "audio_cache_key", side_effect=[
                    first_key, second_key, first_key]), mock.patch.object(
                        UI, "render_lesson_audio", side_effect=render) as renderer:
                first, _ = studio._render(snapshot, threading.Event(), 1)
                second, _ = studio._render(snapshot, threading.Event(), 2)
                cached, _ = studio._render(snapshot, threading.Event(), 3)
            self.assertNotEqual(first, second)
            self.assertEqual(cached, first)
            self.assertEqual(renderer.call_count, 2)
            self.assertRegex(Path(first).name, r"^a[0-9a-f]{7}\.wav$")
            self.assertEqual(set(studio._cache_metadata), {first_key, second_key})
            self.assertLess(len(first), len(str(directory / (first_key + ".wav"))))

    def test_every_audible_setting_changes_cache_identity(self):
        segments = [{"speaker": "Host", "text": "Hello.", "pause_after": 0.4}]
        voices = dict(UI.DEFAULT_VOICES)
        original = UI.audio_cache_key(segments, voices, 1.0, 1.0)
        variants = []
        changed = copy.deepcopy(segments)
        changed[0]["text"] = "Different words."
        variants.append((changed, voices, 1.0, 1.0))
        changed = copy.deepcopy(segments)
        changed[0]["pause_after"] = 3
        variants.append((changed, voices, 1.0, 1.0))
        changed = {**voices, "Host": AVA_LABEL}
        variants.append((segments, changed, 1.0, 1.0))
        variants.append((segments, voices, 1.2, 1.0))
        variants.append((segments, voices, 1.0, 0.5))
        for snapshot in variants:
            with self.subTest(snapshot=snapshot):
                self.assertNotEqual(UI.audio_cache_key(*snapshot), original)
        self.assertEqual(UI.audio_cache_key(segments, dict(reversed(list(voices.items()))), 1, 1), original)

    def test_windows_reserved_and_path_like_titles_produce_safe_names(self):
        self.assertEqual(UI._safe_filename("CON"), "AI lesson")
        self.assertEqual(UI._safe_filename("COM2.txt"), "AI lesson")
        self.assertNotIn("/", UI._safe_filename("a/b\\c: lesson?"))
        self.assertEqual(UI._safe_filename("normal title"), "normal title")


class WorkerIsolationTests(unittest.TestCase):
    def make_studio(self):
        studio = object.__new__(UI.AIStudioWindow)
        studio._closed = False
        studio._close_prompt_active = False
        studio._busy = None
        studio._playing = False
        studio._audio_reserved = False
        studio._playback_gate = None
        studio._close_when_idle = False
        studio._job_id = 0
        studio._events = queue.Queue()
        studio._cancel = threading.Event()
        studio._pause = threading.Event()
        studio.status_var = mock.Mock()
        studio._update_controls = mock.Mock()
        studio._player = mock.Mock()
        studio.pause_button = mock.Mock()
        studio.app = mock.Mock()
        studio.app._reader_audio_is_busy.return_value = False
        studio.after = mock.Mock()
        studio._completed = mock.Mock()
        return studio

    def test_preparation_allows_reader_then_ready_audio_waits_for_existing_speech(self):
        studio = self.make_studio()
        studio._audio_snapshot = mock.Mock(return_value=object())
        render_started = threading.Event()
        render_release = threading.Event()
        playback_started = threading.Event()
        playback_release = threading.Event()
        def render(*_args):
            render_started.set()
            if not render_release.wait(5):
                raise AssertionError("Test did not finish rendering.")
            return "prepared.wav", {"duration_seconds": 1}
        def play(*_args):
            playback_started.set()
            if not playback_release.wait(5):
                raise AssertionError("Test did not release playback.")
        studio._render = mock.Mock(side_effect=render)
        studio._player.play_blocking.side_effect = play
        studio.app._reader_audio_is_busy.return_value = True
        try:
            studio._play()
            self.assertTrue(render_started.wait(2))
            self.assertFalse(studio.owns_audio)
            studio.app.stop.assert_not_called()
            render_release.set()
            ready_event = studio._events.get(timeout=2)
            self.assertEqual(ready_event[1], "ready_to_play")
            self.assertFalse(ready_event[3].is_set())
            studio._events.put(ready_event)
            studio._drain()
            self.assertTrue(studio.owns_audio)
            self.assertFalse(ready_event[3].is_set())
            studio._player.play_blocking.assert_not_called()

            studio.app._reader_audio_is_busy.return_value = False
            studio._drain()
            self.assertTrue(playback_started.wait(2))
            self.assertTrue(studio.owns_audio)
            playback_release.set()
            studio._worker.join(2)
            self.assertFalse(studio._worker.is_alive())
            studio._drain()
            self.assertFalse(studio.owns_audio)
            self.assertIsNone(studio._busy)
            studio._player.play_blocking.assert_called_once()
        finally:
            render_release.set()
            playback_release.set()
            studio.stop()
            studio._worker.join(2)

    def test_cancelling_ready_audio_does_not_start_it_or_stop_reader(self):
        studio = self.make_studio()
        studio._audio_snapshot = mock.Mock(return_value=object())
        studio._render = mock.Mock(return_value=("prepared.wav", {}))
        studio.app._reader_audio_is_busy.return_value = True
        studio._play()
        try:
            ready_event = studio._events.get(timeout=2)
            self.assertEqual(ready_event[1], "ready_to_play")
            studio._events.put(ready_event)
            studio._drain()
            self.assertTrue(studio.owns_audio)
            studio.stop()
            studio._worker.join(2)
            self.assertFalse(studio._worker.is_alive())
            studio.app._reader_audio_is_busy.return_value = False
            studio._drain()
            self.assertFalse(studio.owns_audio)
            self.assertFalse(ready_event[3].is_set())
            studio._player.play_blocking.assert_not_called()
            studio.app.stop.assert_not_called()
        finally:
            studio.stop()
            studio._worker.join(2)

    def test_worker_reports_result_through_queue_without_tk_updates(self):
        studio = self.make_studio()
        main_thread = threading.get_ident()
        worker_threads = []
        studio.status_var.set.side_effect = lambda _: self.assertEqual(
            threading.get_ident(), main_thread)
        def worker(cancel, job):
            worker_threads.append(threading.get_ident())
            studio._progress(job, "working")
            return "lesson"
        self.assertTrue(studio._start_job("generate", worker, "starting"))
        studio._worker.join(5)
        self.assertFalse(studio._worker.is_alive())
        self.assertNotEqual(worker_threads, [main_thread])
        self.assertEqual(studio._events.get_nowait()[1:], ("progress", None, "working"))
        self.assertEqual(studio._events.get_nowait()[1:], ("done", "generate", "lesson"))

    def test_stop_cancels_worker_and_only_its_own_player(self):
        studio = self.make_studio()
        studio.app = mock.Mock()
        started = threading.Event()
        def worker(cancel, job):
            started.set()
            if cancel.wait(5):
                raise AIStudioCancelled("stopped")
            raise AssertionError("Cancellation did not arrive.")
        studio._start_job("play", worker, "starting")
        self.assertTrue(started.wait(2))
        studio.stop()
        studio._worker.join(5)
        self.assertFalse(studio._worker.is_alive())
        studio._player.stop.assert_called_once()
        studio.app.stop.assert_not_called()
        self.assertEqual(studio._events.get_nowait()[1:], ("cancelled", "play", None))

    def test_active_job_blocks_second_worker(self):
        studio = self.make_studio()
        studio._busy = "generate"
        other = mock.Mock()
        self.assertFalse(studio._start_job("export", other, "starting"))
        other.assert_not_called()

    def test_close_marks_nested_save_dialog_active_until_cancel_returns(self):
        studio = self.make_studio()
        seen = []
        def decline():
            seen.append(studio._close_prompt_active)
            return False
        studio._confirm_discard = decline
        studio.close()
        self.assertEqual(seen, [True])
        self.assertFalse(studio._close_prompt_active)
        self.assertFalse(studio._closed)

    def test_close_resets_nested_dialog_flag_even_on_dialog_error(self):
        studio = self.make_studio()
        studio._confirm_discard = mock.Mock(side_effect=RuntimeError("dialog failed"))
        with self.assertRaises(RuntimeError):
            studio.close()
        self.assertFalse(studio._close_prompt_active)


if __name__ == "__main__":
    unittest.main()
