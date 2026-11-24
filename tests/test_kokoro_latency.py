import importlib.machinery
import importlib.util
import os
import threading
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader(
    "piper_reader_kokoro_latency_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class KokoroStartupChunkTests(unittest.TestCase):
    def test_first_chunk_has_a_strict_latency_cap(self):
        text = "word " * 80
        chunks = APP.split_chunks(
            text, 180, first_max_len=APP.KOKORO_INITIAL_CHUNK_LEN)
        self.assertTrue(chunks)
        self.assertLessEqual(
            len(chunks[0]), APP.KOKORO_INITIAL_CHUNK_LEN)
        self.assertEqual(" ".join(chunks), text.strip())

        # Punctuation immediately beyond the budget must not expand it by one.
        edge = "a" * APP.KOKORO_INITIAL_CHUNK_LEN + ". trailing text"
        edge_chunks = APP.split_chunks(
            edge, 180, first_max_len=APP.KOKORO_INITIAL_CHUNK_LEN)
        self.assertLessEqual(
            len(edge_chunks[0]), APP.KOKORO_INITIAL_CHUNK_LEN)

    def test_natural_boundary_is_preferred_only_inside_cap(self):
        first = (
            "Natural speech has rhythm, emphasis, and small changes in pitch.")
        second = (
            "A good reader groups words into meaningful phrases and does not "
            "sound mechanical.")
        chunks = APP.split_chunks(
            f"{first} {second}", 180,
            first_max_len=APP.KOKORO_INITIAL_CHUNK_LEN)
        self.assertEqual(chunks, [first, second])
        self.assertLessEqual(len(chunks[0]), APP.KOKORO_INITIAL_CHUNK_LEN)

    def test_cap_and_metadata_survive_every_request_path(self):
        text = (
            "This deliberately long opening contains enough ordinary words to "
            "cross the fast Heart startup limit without punctuation and then "
            "continues into another engine block.")
        metadata = {"sentence_id": 4, "paragraph_id": 2}
        requests = (
            ("text", text),
            ("clipboard", text),
            ("doc", [(text, metadata)]),
            ("browser", [(text, metadata)]),
        )
        for mode in APP.REPEAT_MODE_LABELS:
            for kind, payload in requests:
                with self.subTest(mode=mode, kind=kind):
                    units = APP.build_repeat_units(
                        kind, payload, mode, 180,
                        first_max_len=APP.KOKORO_INITIAL_CHUNK_LEN)
                    first_text, first_meta = units[0][0]
                    self.assertLessEqual(
                        len(first_text), APP.KOKORO_INITIAL_CHUNK_LEN)
                    if kind in ("doc", "browser"):
                        self.assertIs(first_meta, metadata)


class KokoroReprimeTests(unittest.TestCase):
    def test_force_warm_reprises_an_already_warmed_runtime(self):
        class FakeKokoro:
            def __init__(self):
                self.calls = []

            def create(self, text, **kwargs):
                self.calls.append((text, kwargs))

        engine = APP.KokoroEngine.__new__(APP.KokoroEngine)
        engine._lock = threading.RLock()
        engine._runtime_warmed = True
        fake = FakeKokoro()
        engine._get = mock.Mock(return_value=fake)
        with mock.patch.object(APP.os.path, "exists", return_value=True):
            self.assertTrue(engine.warm("af_heart", "en-us"))
            self.assertEqual(fake.calls, [])
            self.assertTrue(engine.warm(
                "af_heart", "en-us", force=True))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "Ready.")

    def test_probe_triggers_on_user_return_and_resume_gap_only(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._runtime_probe_last = 100.0
        app._runtime_user_was_idle = False
        app._runtime_reprime_pending = False
        requested = []
        app._request_runtime_reprime = requested.append

        APP.ReaderApp._probe_runtime_reprime(
            app, now=106.0,
            user_idle_seconds=APP.RUNTIME_USER_IDLE_SECONDS + 1)
        self.assertTrue(app._runtime_user_was_idle)
        self.assertEqual(requested, [])

        APP.ReaderApp._probe_runtime_reprime(
            app, now=112.0, user_idle_seconds=0.0)
        self.assertEqual(requested, [112.0])

        # Ordinary five-second probes do not repeatedly run inference.
        APP.ReaderApp._probe_runtime_reprime(
            app, now=118.0, user_idle_seconds=0.0)
        self.assertEqual(requested, [112.0])

        APP.ReaderApp._probe_runtime_reprime(
            app, now=160.0, user_idle_seconds=0.0)
        self.assertEqual(requested, [112.0, 160.0])

    def test_reprime_waits_for_speech_and_save_wav(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._shutdown_event = threading.Event()
        app._playback_lock = threading.RLock()
        app._speech_requests = {object()}
        app._save_wav_jobs = 0
        app._runtime_reprime_pending = True
        app._runtime_reprime_inflight = False
        app._clipboard_warm_lock = threading.Lock()
        app._clipboard_warming = set()
        app.clipboard_voice_var = _Value(
            "Heart (US female) - Kokoro, most natural")
        app.voice_var = _Value(
            "Heart (US female) - Kokoro, most natural")
        launched = []

        def launch(label, force=False, completion=None):
            launched.append((label, force, completion))
            return True

        app._warm_voice_label_async = launch

        APP.ReaderApp._try_runtime_reprime(
            app, now=1000.0,
            user_idle_seconds=APP.RUNTIME_REPRIME_QUIET_SECONDS + 1)
        self.assertEqual(launched, [])
        self.assertTrue(app._runtime_reprime_pending)

        app._speech_requests.clear()
        app._save_wav_jobs = 1
        APP.ReaderApp._try_runtime_reprime(
            app, now=1000.0,
            user_idle_seconds=APP.RUNTIME_REPRIME_QUIET_SECONDS + 1)
        self.assertEqual(launched, [])
        self.assertTrue(app._runtime_reprime_pending)

        app._save_wav_jobs = 0
        APP.ReaderApp._try_runtime_reprime(
            app, now=1000.0,
            user_idle_seconds=APP.RUNTIME_REPRIME_QUIET_SECONDS + 1)
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0][1])
        self.assertIs(launched[0][2].__self__, app)
        self.assertIs(
            launched[0][2].__func__,
            APP.ReaderApp._finish_runtime_reprime)
        self.assertTrue(app._runtime_reprime_inflight)
        self.assertFalse(app._runtime_reprime_pending)

    def test_idle_return_grace_lets_real_speech_claim_kokoro_first(self):
        class ImmediateRoot:
            def after(self, _delay, callback, *args):
                callback(*args)

        class LockedEngine:
            name = "Kokoro"

            def __init__(self):
                self.lock = threading.Lock()
                self.warm_entered = threading.Event()
                self.warm_release = threading.Event()
                self.warm_finished = threading.Event()
                self.speech_entered = threading.Event()

            @staticmethod
            def ready():
                return True

            def warm(self, *_args, **_kwargs):
                with self.lock:
                    self.warm_entered.set()
                    self.warm_release.wait(1)
                self.warm_finished.set()
                return True

            def synthesize_speech(self):
                with self.lock:
                    self.speech_entered.set()

        engine = LockedEngine()
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._shutdown_event = threading.Event()
        app._playback_lock = threading.RLock()
        app._speech_requests = set()
        app._save_wav_jobs = 0
        app._clipboard_warm_lock = threading.Lock()
        app._clipboard_warming = set()
        app._clipboard_warmed = set()
        app._runtime_reprime_pending = False
        app._runtime_reprime_inflight = False
        app._runtime_reprime_last = 0.0
        app._runtime_reprime_not_before = 0.0
        app.clipboard_voice_var = _Value(
            "Heart (US female) - Kokoro, most natural")
        app.voice_var = app.clipboard_voice_var
        app.engines = {"kokoro": engine}
        app.root = ImmediateRoot()

        APP.ReaderApp._request_runtime_reprime(app, now=1000.0)
        self.assertTrue(app._runtime_reprime_pending)
        self.assertFalse(engine.warm_entered.wait(0.05))

        # This models a copy arriving just after the first post-idle input.
        speech = threading.Thread(target=engine.synthesize_speech)
        speech.start()
        self.assertTrue(engine.speech_entered.wait(0.2))
        speech.join()

        # Neither the grace window nor continuing user input may start warmup.
        APP.ReaderApp._try_runtime_reprime(
            app, now=1011.0,
            user_idle_seconds=APP.RUNTIME_REPRIME_QUIET_SECONDS + 1)
        self.assertFalse(engine.warm_entered.is_set())
        APP.ReaderApp._try_runtime_reprime(
            app, now=1012.0, user_idle_seconds=0.0)
        self.assertFalse(engine.warm_entered.is_set())

        # Once both grace and quiet time have elapsed, exactly one maintenance
        # inference may run.
        with mock.patch.object(APP.os.path, "exists", return_value=True):
            APP.ReaderApp._try_runtime_reprime(
                app, now=1012.0,
                user_idle_seconds=APP.RUNTIME_REPRIME_QUIET_SECONDS + 1)
        self.assertTrue(engine.warm_entered.wait(0.2))
        engine.warm_release.set()
        self.assertTrue(engine.warm_finished.wait(0.5))

    def test_launch_race_is_deferred_until_engine_is_idle(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._shutdown_event = threading.Event()
        app._runtime_reprime_inflight = True
        app._runtime_reprime_pending = False
        app._runtime_reprime_last = 0.0
        APP.ReaderApp._finish_runtime_reprime(
            app, warmed=False, blocked=True)
        self.assertFalse(app._runtime_reprime_inflight)
        self.assertTrue(app._runtime_reprime_pending)


if __name__ == "__main__":
    unittest.main()
