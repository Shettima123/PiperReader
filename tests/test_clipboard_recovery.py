"""Clipboard policy, FIFO failure recovery, and stale UI update regressions."""
import importlib.machinery
from pathlib import Path
from collections import deque
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

APP = importlib.machinery.SourceFileLoader(
    'piper_clipboard_recovery_tests',
    str(Path(__file__).resolve().parents[1] / 'piper_reader.pyw')).load_module()

class Value:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value

class PendingRoot:
    def __init__(self): self.pending = []
    def after(self, delay, function, *args):
        self.pending.append((function, args))
        return 'pending'
    def flush(self):
        while self.pending:
            function, args = self.pending.pop(0)
            function(*args)

def bare():
    app = APP.ReaderApp.__new__(APP.ReaderApp)
    app.root = PendingRoot()
    app._shutdown_event = threading.Event()
    app._playback_lock = threading.RLock()
    app._speech_requests = set()
    app._clipboard_enqueued_at = {}
    app._studio_clipboard_queue = deque()
    app._active_cancel = None
    app._active_pause = None
    app._pause_requested = threading.Event()
    app._active_request_phase = 'preparing'
    app._active_request_message = ''
    app._clipboard_online_retry_after = 0
    app.clipboard_backup_var = Value(True)
    app.auto_var = Value(True)
    app.voice_var = Value('Andrew (US male) - Microsoft, online')
    app.clipboard_voice_var = Value(app.voice_var.get())
    app.speed_var = Value(1)
    app.vol_var = Value(1)
    app._repeat_snapshot = lambda: ('off', 1)
    app._set_playback_state = mock.Mock()
    app._apply_playback_state = mock.Mock()
    app._apply_status = mock.Mock()
    app.set_status = mock.Mock()
    app.speak_q = queue.Queue()
    app._browser_bridge = None
    app._speech_diagnostics = mock.Mock()
    app.engines = {'edge': APP.EdgeEngine(lambda _: None), 'piper': mock.Mock()}
    return app

class ClipboardRecoveryTests(unittest.TestCase):
    def test_clipboard_uses_private_bounded_engine_and_keeps_other_engines(self):
        app = bare()
        original = app.engines['edge']
        app.clipboard_backup_var.set(False)
        engine = app._engine_for_speech_request('clipboard', 'edge', threading.Event())
        self.assertIsNot(engine, original)
        self.assertEqual((engine.max_attempts, engine.request_timeout), (2, 6))
        self.assertIs(app._engine_for_speech_request('text', 'edge', threading.Event()), original)
        self.assertEqual(original.request_timeout, 30)
        self.assertIs(app._engine_for_speech_request('clipboard', 'piper', threading.Event()), app.engines['piper'])

    def test_clipboard_fallback_sets_cooldown_and_next_copy_uses_it(self):
        app = bare()
        cancel = threading.Event()
        with mock.patch.object(APP.PiperEngine, '_voice_files_ready', return_value=True):
            engine = app._engine_for_speech_request('clipboard', 'edge', cancel)
            error = APP.OnlineSpeechError('timeout')
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                    APP.EdgeEngine, 'synth_to_wav', side_effect=error):
                engine.synth_to_wav('Public test.', 'voice', 'en-us', 1, 1,
                                    str(Path(directory) / 'test.wav'), cancel_event=cancel)
            remaining = app._clipboard_online_retry_after - time.monotonic()
            self.assertGreater(remaining, 298)
            next_engine = app._engine_for_speech_request('clipboard', 'edge', threading.Event())
            self.assertEqual(next_engine.active_engine_key, 'piper')
            app._clipboard_online_retry_after = time.monotonic() - 1
            recovered = app._engine_for_speech_request('clipboard', 'edge', threading.Event())
            self.assertEqual(recovered.active_engine_key, 'edge')

    def test_status_queued_before_stop_cannot_overwrite_stopped_state(self):
        app = bare()
        cancel = threading.Event()
        app._active_cancel = cancel
        app._speech_requests.add(cancel)
        app._set_request_status(cancel, 'Retrying once...', phase='preparing')
        cancel.set()
        app._speech_requests.clear()
        app.root.flush()
        app._apply_status.assert_not_called()

    def test_preparing_status_shows_waiting_copies_without_claiming_playback(self):
        app = bare()
        first, second = threading.Event(), threading.Event()
        app._active_cancel = first
        app._speech_requests.update((first, second))
        app._clipboard_enqueued_at = {first: time.monotonic(), second: time.monotonic()}
        app._set_request_status(first, 'Preparing Andrew online...', phase='preparing')
        app.root.flush()
        message = app._apply_status.call_args.args[0]
        self.assertIn('Preparing', message)
        self.assertIn('1 copied passage waiting', message)
        self.assertNotIn('Speaking', message)
        self.assertEqual(app._active_request_phase, 'preparing')

    def test_status_received_while_paused_does_not_hide_pause(self):
        app = bare()
        cancel = threading.Event()
        app._active_cancel = cancel
        app._speech_requests.add(cancel)
        app._pause_requested.set()
        app._set_request_status(cancel, 'Preparing Andrew online...', phase='preparing')
        app.root.flush()
        app._apply_status.assert_not_called()
        self.assertIn('Preparing', app._last_speaking_status)

    def test_lookahead_retry_keeps_playing_phase_while_audio_is_audible(self):
        app = bare()
        cancel = threading.Event()
        app._active_cancel = cancel
        app._speech_requests.add(cancel)
        app._audio_playing_cancel = cancel
        app._active_audio_message = 'Speaking... (1/2)'
        app._active_request_phase = 'playing'
        app._set_request_status(cancel, 'Retrying once...', phase='preparing')
        app.root.flush()
        self.assertEqual(app._active_request_phase, 'playing')
        self.assertIn('Speaking', app._apply_status.call_args.args[0])
        self.assertIn('Retrying', app._apply_status.call_args.args[0])

    def test_copy_queue_remains_fifo_and_keeps_paused_state(self):
        app = bare()
        app._pause_requested.set()
        app._queue_clipboard_text('First passage.')
        app._queue_clipboard_text('Second passage.')
        self.assertEqual([app.speak_q.get_nowait()[1] for _ in range(2)], ['First passage.', 'Second passage.'])
        self.assertEqual(len(app._clipboard_enqueued_at), 2)
        self.assertEqual(app._set_playback_state.call_args.args[0], 'paused')

    def test_failed_copy_does_not_strand_next_copy(self):
        self._check_failed_copy_continuation()

    def test_diagnostic_failure_does_not_strand_next_copy(self):
        self._check_failed_copy_continuation(diagnostics_fail=True)

    def _check_failed_copy_continuation(self, diagnostics_fail=False):
        app = bare()
        if diagnostics_fail:
            app._speech_diagnostics.record.side_effect = OSError('logging unavailable')
        events = [threading.Event(), threading.Event()]
        app._speech_requests.update(events)
        app._clipboard_enqueued_at.update({event: time.monotonic() for event in events})
        app.player = mock.Mock()
        played = []
        app.player.play_blocking.side_effect = lambda path, *_: played.append(Path(path).read_text())
        app._complete_playback_request = mock.Mock()
        engine = mock.Mock(name='engine', chunk_len=700, initial_chunk_len=150, lookahead=False, supports_cancellation=True)
        engine.name = 'Microsoft online'
        engine.ready.return_value = True
        calls = []
        def synth(text, _voice, _lang, _speed, _volume, path, cancel_event=None):
            calls.append(text)
            if len(calls) == 1:
                raise APP.OnlineSpeechError('empty_response')
            Path(path).write_text(text)
        engine.synth_to_wav.side_effect = synth
        app._engine_for_speech_request = lambda *_: engine
        for event, text in zip(events, ['First copy.', 'Second copy.']):
            app.speak_q.put(('clipboard', text, event, threading.Event(), 'edge', 'voice', 'en-us', 1, 1, 'off', 1))
        app.speak_q.put(None)
        with tempfile.TemporaryDirectory() as directory:
            app.tmpdir = directory
            app._worker()
            self.assertEqual(list(Path(directory).iterdir()), [])
        app.root.flush()
        self.assertEqual(played, ['Second copy.'])
        self.assertEqual(app._complete_playback_request.call_args_list,
                         [mock.call(events[0], False), mock.call(events[1], True)])
        self.assertEqual([call.kwargs['outcome'] for call in app._speech_diagnostics.record.call_args_list], ['failed', 'completed'])

if __name__ == '__main__': unittest.main()
