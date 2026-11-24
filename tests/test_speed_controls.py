import importlib.machinery
import importlib.util
import math
import os
import queue
import tempfile
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader(
    "piper_reader_speed_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Bridge:
    def __init__(self):
        self.updates = []

    def update_state(self, **changes):
        self.updates.append(changes)


class _ImmediateRoot:
    @staticmethod
    def after(_delay, callback, *args):
        callback(*args)
        return "after-id"


class _RecordingEngine:
    name = "Fake"
    chunk_len = 500
    initial_chunk_len = None

    def __init__(self):
        self.speeds = []

    @staticmethod
    def ready():
        return True

    def synth_to_wav(self, _text, _voice, _lang, speed, _volume, path):
        self.speeds.append(speed)
        with open(path, "wb") as stream:
            stream.write(b"fake-wave")


class _NoOpPlayer:
    @staticmethod
    def play_blocking(_path, _cancel_event, _pause_event):
        return None


class SpeedHelperTests(unittest.TestCase):
    def test_normalize_preserves_legacy_precision_and_clamps(self):
        cases = (
            (1.234, 1.23),
            (0.1, APP.SPEED_MIN),
            (99, APP.SPEED_MAX),
            (None, 1.0),
            ("not-a-number", 1.0),
            (float("nan"), 1.0),
            (float("inf"), 1.0),
            (False, 1.0),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(APP.normalize_speed(value), expected)

    def test_quantize_uses_exact_five_hundredths_and_half_up_ties(self):
        cases = (
            (1.23, 1.25),
            (0.575, 0.60),
            (1.025, 1.05),
            (1.275, 1.30),
            (0.49, APP.SPEED_MIN),
            (2.51, APP.SPEED_MAX),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertTrue(math.isclose(
                    APP.quantize_speed(value), expected, abs_tol=1e-9))

    def test_step_buttons_move_to_the_next_clean_tenth(self):
        cases = (
            (1.38, 1, 1.4),
            (1.38, -1, 1.3),
            (1.05, 1, 1.1),
            (1.05, -1, 1.0),
            (APP.SPEED_MAX, 1, APP.SPEED_MAX),
            (APP.SPEED_MIN, -1, APP.SPEED_MIN),
        )
        for value, direction, expected in cases:
            with self.subTest(value=value, direction=direction):
                self.assertEqual(
                    APP.stepped_speed(value, direction), expected)

    def test_reference_presets_and_readable_labels(self):
        self.assertEqual(
            APP.SPEED_PRESETS, (0.8, 1.0, 1.2, 1.5, 2.0, 2.5))
        self.assertEqual(APP.format_speed(0.8), "0.8\u00d7")
        self.assertEqual(APP.format_speed(1.0), "1.0\u00d7")
        self.assertEqual(APP.format_speed(1.38), "1.38\u00d7")
        self.assertEqual(APP.speed_description(1.0), "Normal")
        self.assertEqual(APP.speed_description(2.5), "Very fast")

    def test_description_thresholds(self):
        cases = (
            (0.79, "Very slow"), (0.80, "Slow"),
            (0.94, "Slow"), (0.95, "Normal"),
            (1.10, "Normal"), (1.11, "Faster"),
            (1.50, "Faster"), (1.51, "Fast"),
            (2.00, "Fast"), (2.01, "Very fast"),
        )
        for speed, expected in cases:
            with self.subTest(speed=speed):
                self.assertEqual(APP.speed_description(speed), expected)


class SpeedReaderAppTests(unittest.TestCase):
    @staticmethod
    def _minimal_app(speed=1.0):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.speed_var = _Value(speed)
        app.vol_var = _Value(1.0)
        app.auto_var = _Value(True)
        app._speed_panel = None
        app._browser_bridge = _Bridge()
        app.saved = []
        app._save_settings = lambda: app.saved.append(app.speed_var.get())
        app.statuses = []
        app.set_status = app.statuses.append
        return app

    def test_set_speed_updates_bridge_and_persists_without_engine_reload(self):
        app = self._minimal_app(1.0)
        engines = object()
        app.engines = engines

        result = app._set_speed(1.23, save=True, snap=True)

        self.assertEqual(result, 1.25)
        self.assertEqual(app.speed_var.get(), 1.25)
        self.assertEqual(app.saved, [1.25])
        self.assertEqual(app._browser_bridge.updates[-1], {"speed": 1.25})
        self.assertIs(app.engines, engines)

    def test_adjust_speed_uses_clean_tenths(self):
        app = self._minimal_app(1.38)
        self.assertEqual(app._adjust_speed(1), "break")
        self.assertEqual(app.speed_var.get(), 1.4)
        self.assertIn("1.4\u00d7", app.statuses[-1])

    def test_each_queued_request_snapshots_the_current_speed(self):
        app = self._minimal_app(1.0)
        app._shutdown_event = threading.Event()
        app._playback_lock = threading.RLock()
        app._speech_requests = set()
        app._active_cancel = None
        app._active_pause = None
        app._pause_requested = threading.Event()
        app.speak_q = queue.Queue()
        app.voice_var = _Value("voice")
        app._voice_snapshot = lambda _label: ("fake", "voice-id", "en-us")
        app._repeat_snapshot = lambda: ("off", 1)
        app._set_playback_state = lambda _state: None

        app._queue_speech("text", "first", replace=False)
        first = app.speak_q.get_nowait()
        app._set_speed(1.23, save=False, snap=True)
        app._queue_speech("text", "second", replace=False)
        second = app.speak_q.get_nowait()

        self.assertEqual(first[7], 1.0)
        self.assertEqual(second[7], 1.25)

    def test_worker_passes_successive_speeds_to_the_same_engine(self):
        engine = _RecordingEngine()
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.speak_q = queue.Queue()
        app._shutdown_event = threading.Event()
        app._playback_lock = threading.RLock()
        app._pause_requested = threading.Event()
        app._active_cancel = None
        app._active_pause = None
        first_cancel = threading.Event()
        second_cancel = threading.Event()
        app._speech_requests = {first_cancel, second_cancel}
        app.engines = {"fake": engine}
        app.player = _NoOpPlayer()
        app.root = _ImmediateRoot()
        app._browser_bridge = None
        app._set_playback_state = lambda _state: None
        app.set_status = lambda _status: None
        app._safe_focus = lambda _meta: None
        app._complete_playback_request = lambda *_args: None
        app._finish_browser_session = lambda *_args: None

        with tempfile.TemporaryDirectory(prefix="piper_speed_worker_") as tmp:
            app.tmpdir = tmp
            app.speak_q.put((
                "text", "first", first_cancel, app._pause_requested,
                "fake", "voice", "en-us", 0.8, 1.0, "off", 1))
            app.speak_q.put((
                "text", "second", second_cancel, app._pause_requested,
                "fake", "voice", "en-us", 2.5, 1.0, "off", 1))
            app.speak_q.put(None)
            app._worker()

        self.assertEqual(engine.speeds, [0.8, 2.5])


if __name__ == "__main__":
    unittest.main()
