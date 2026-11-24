import importlib.machinery
from collections import deque
import queue
import threading
import unittest
from pathlib import Path
from unittest import mock


APP = importlib.machinery.SourceFileLoader(
    "piper_reader_ai_integration",
    str(Path(__file__).resolve().parents[1] / "piper_reader.pyw"),
).load_module()


class AIReaderIntegrationTests(unittest.TestCase):
    def bare(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.viewer = None
        app.text = mock.Mock()
        app.current_text_name = "Original chapter"
        app.current_library_item_id = "keep-this-item"
        app.ai_studio = None
        return app

    def test_snapshot_copies_visible_document_without_writing_reader(self):
        app = self.bare()
        app.viewer = mock.Mock(title="Chapter 3")
        app.viewer.text.return_value = "  Original document text.  "
        self.assertEqual(app._ai_source_snapshot(),
                         ("Original document text.", "Chapter 3"))
        self.assertEqual(app.current_library_item_id, "keep-this-item")
        app.text.delete.assert_not_called()
        app.text.insert.assert_not_called()

    def test_welcome_instructions_are_not_used_as_lesson_source(self):
        app = self.bare()
        app._welcome_text = "Reader instructions."
        app.text.get.return_value = "Reader instructions.\n"
        self.assertEqual(app._ai_source_snapshot()[0], "")
        app.text.get.return_value = "A pasted lesson.\n"
        self.assertEqual(app._ai_source_snapshot()[0], "A pasted lesson.")

    def test_open_existing_studio_preserves_its_current_script(self):
        app = self.bare()
        app.ai_studio = mock.Mock()
        app.ai_studio.winfo_exists.return_value = True
        app._ai_source_snapshot = mock.Mock()
        app.open_ai_studio()
        app.ai_studio.deiconify.assert_called_once()
        app.ai_studio.lift.assert_called_once()
        app._ai_source_snapshot.assert_not_called()

    def clipboard_app(self, studio_job=None):
        app = self.bare()
        app._last_clip = None
        app._last_clip_sequence = None
        app._shutdown_event = threading.Event()
        app._studio_clipboard_queue = deque()
        app.auto_var = mock.Mock()
        app.auto_var.get.return_value = True
        app._speak_clipboard_text = mock.Mock()
        app.ai_studio = mock.Mock()
        app.ai_studio.winfo_exists.return_value = True
        app.ai_studio._busy = studio_job
        app.ai_studio.owns_audio = studio_job == "play"
        return app

    def test_clipboard_continues_with_idle_or_nonplayback_studio_jobs(self):
        for job in (None, "connection", "login", "generate", "export", "play"):
            with self.subTest(studio_job=job):
                app = self.clipboard_app(job)
                # Play's audio preparation has no reservation until ready.
                app.ai_studio.owns_audio = False
                app._accept_clipboard_update(None, "Copied while studio is open")
                app._speak_clipboard_text.assert_called_once_with(
                    "Copied while studio is open")
                app.ai_studio.stop.assert_not_called()
                app.auto_var.set.assert_not_called()
                self.assertFalse(app._studio_clipboard_queue)

    def test_playback_ownership_tracks_reservation_and_not_rendering(self):
        app = self.clipboard_app("play")
        app.ai_studio.owns_audio = False
        self.assertFalse(app._ai_studio_owns_audio())
        app.ai_studio.owns_audio = True
        for playing, paused in ((False, False), (True, False), (True, True)):
            with self.subTest(playing=playing, paused=paused):
                app.ai_studio._playing = playing
                app.ai_studio._pause = threading.Event()
                if paused:
                    app.ai_studio._pause.set()
                self.assertTrue(app._ai_studio_owns_audio())
        app.ai_studio.winfo_exists.return_value = False
        self.assertFalse(app._ai_studio_owns_audio())

    def test_copies_wait_for_studio_playback_and_resume_once_in_fifo_order(self):
        app = self.clipboard_app("play")
        app._last_clip_sequence = 10
        app._accept_clipboard_update(11, "  First copied passage.  ")
        app._accept_clipboard_update(12, "Second copied passage.")
        app._accept_clipboard_update(12, "Second copied passage.")
        app._flush_studio_clipboard_queue()
        app._speak_clipboard_text.assert_not_called()
        self.assertEqual(list(app._studio_clipboard_queue), [
            "First copied passage.", "Second copied passage."])
        self.assertEqual(app._last_clip_sequence, 12)

        app.ai_studio._busy = None
        app.ai_studio.owns_audio = False
        app._flush_studio_clipboard_queue()
        app._flush_studio_clipboard_queue()
        self.assertEqual(app._speak_clipboard_text.call_args_list, [
            mock.call("First copied passage."),
            mock.call("Second copied passage."),
        ])
        self.assertFalse(app._studio_clipboard_queue)
        app.ai_studio.stop.assert_not_called()
        app.auto_var.set.assert_not_called()

    def test_failed_or_cancelled_playback_releases_waiting_clipboard(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Waiting for audio to finish.")
        # The Studio's terminal error/cancel event clears _busy while its
        # window and the user's source/script remain open.
        app.ai_studio._busy = None
        app.ai_studio.owns_audio = False
        app._flush_studio_clipboard_queue()
        app._accept_clipboard_update(None, "Copied after the audio failed.")
        self.assertEqual(app._speak_clipboard_text.call_args_list, [
            mock.call("Waiting for audio to finish."),
            mock.call("Copied after the audio failed."),
        ])

    def test_clipboard_poll_resumes_waiting_text_without_another_copy(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Resume this after playback.")
        app._clipboard_events = queue.Queue()
        app._clipboard_pending_updates = deque()
        app._clipboard_listener_ready = threading.Event()
        app._clipboard_listener_ready.set()
        app._probe_runtime_reprime = mock.Mock()
        app.root = mock.Mock()
        app.ai_studio._busy = None
        app.ai_studio.owns_audio = False
        app._poll_clipboard()
        app._speak_clipboard_text.assert_called_once_with(
            "Resume this after playback.")
        app.root.after.assert_called_once_with(75, app._poll_clipboard)

    def test_closing_studio_releases_waiting_clipboard(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Waiting for the studio.")
        app.ai_studio.winfo_exists.return_value = False
        app._flush_studio_clipboard_queue()
        app._speak_clipboard_text.assert_called_once_with(
            "Waiting for the studio.")

    def test_disabling_auto_read_clears_waiting_copies_without_replaying(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Do not replay this later.")
        app.auto_var.get.return_value = False
        app._flush_studio_clipboard_queue()
        self.assertFalse(app._studio_clipboard_queue)
        app._accept_clipboard_update(None, "Copied with auto-read off.")
        app.auto_var.get.return_value = True
        app.ai_studio._busy = None
        app.ai_studio.owns_audio = False
        app._flush_studio_clipboard_queue()
        app._speak_clipboard_text.assert_not_called()

    def test_shutdown_clears_waiting_clipboard_without_starting_speech(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Waiting at shutdown.")
        app._shutdown_event.set()
        app._flush_studio_clipboard_queue()
        self.assertFalse(app._studio_clipboard_queue)
        app._speak_clipboard_text.assert_not_called()

    def test_explicit_stop_clears_waiting_clipboard(self):
        app = self.clipboard_app("play")
        app._accept_clipboard_update(None, "Waiting at Stop.")
        app._content_load_serial = 0
        app._browser_active_session = None
        app._browser_request_cancel = None
        app._browser_bridge = None
        app._playback_lock = threading.RLock()
        app._speech_requests = set()
        app._active_cancel = None
        app._active_pause = None
        app._pause_requested = threading.Event()
        app.player = mock.Mock()
        app.speak_q = queue.Queue()
        app._set_playback_state = mock.Mock()
        app.stop(silent=True)
        self.assertFalse(app._studio_clipboard_queue)
        app.ai_studio._busy = None
        app.ai_studio.owns_audio = False
        app._flush_studio_clipboard_queue()
        app._speak_clipboard_text.assert_not_called()

    def test_opening_studio_preserves_ongoing_reader_playback(self):
        app = self.bare()
        app._ai_source_snapshot = mock.Mock(return_value=("Chapter text.", "Chapter"))
        app.stop = mock.Mock()
        app.set_status = mock.Mock()
        with mock.patch("ai_studio_ui.AIStudioWindow") as window:
            self.assertEqual(app.open_ai_studio(), "break")
        window.assert_called_once_with(
            app, "Chapter text.", "Chapter", data_dir=APP.DATA_DIR)
        app.stop.assert_not_called()

    def test_quit_waits_for_studio_cancellation_before_destroying_reader(self):
        app = self.bare()
        app._exiting = False
        app.root = mock.Mock()
        app.ai_studio = mock.Mock(_close_when_idle=True)
        app.ai_studio.winfo_exists.return_value = True
        app._exit_app()
        self.assertFalse(app._exiting)
        self.assertTrue(app._exit_waiting_for_studio)
        app.ai_studio.close.assert_called_once_with()
        app.root.destroy.assert_not_called()
        app.root.after.assert_called_once()

    def test_quit_continues_waiting_while_unsaved_prompt_is_open(self):
        app = self.bare()
        app.root = mock.Mock()
        app.ai_studio = mock.Mock(_close_when_idle=False, _close_prompt_active=True)
        app.ai_studio.winfo_exists.return_value = True
        app._exit_waiting_for_studio = True
        app._continue_exit_after_studio()
        self.assertTrue(app._exit_waiting_for_studio)
        app.root.after.assert_called_once()

    def test_cancel_unsaved_prompt_cancels_reader_quit(self):
        app = self.bare()
        app.root = mock.Mock()
        app.ai_studio = mock.Mock(_close_when_idle=False, _close_prompt_active=False)
        app.ai_studio.winfo_exists.return_value = True
        app._exit_waiting_for_studio = True
        app._continue_exit_after_studio()
        self.assertFalse(app._exit_waiting_for_studio)
        app.root.after.assert_not_called()

    def test_closed_studio_resumes_reader_quit(self):
        app = self.bare()
        app._exit_app = mock.Mock()
        app._exit_waiting_for_studio = True
        app._continue_exit_after_studio()
        app._exit_app.assert_called_once()
        self.assertFalse(app._exit_waiting_for_studio)


if __name__ == "__main__":
    unittest.main()
