from pathlib import Path
import tempfile
import threading
import unittest
import wave

from edge_engine import OnlineSpeechError, SpeechCancelled

try:
    from clipboard_speech import ClipboardSpeechEngine
except ImportError:
    ClipboardSpeechEngine = None


class FakeEngine:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.ready_calls = 0
        self.install_calls = 0

    def ready(self):
        self.ready_calls += 1
        return True

    def install(self):
        self.install_calls += 1
        return True

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path,
                     **kwargs):
        self.calls.append((text, voice_id, lang, speed, volume,
                           Path(out_path), kwargs))
        result = self.results.pop(0) if self.results else None
        if callable(result):
            result(Path(out_path), kwargs)
        elif isinstance(result, Exception):
            raise result
        with wave.open(str(out_path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(22050)
            stream.writeframes(text.encode("utf-8") * 2)


class ClipboardSpeechTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name) / "speech.wav"
        self.online = FakeEngine()
        self.offline = FakeEngine()
        self.transitions = []
        self.statuses = []

    def wrapper(self, **options):
        self.assertIsNotNone(ClipboardSpeechEngine,
                             "ClipboardSpeechEngine must be implemented")
        settings = dict(
            offline_voice_id="en_US-lessac-medium",
            offline_available=lambda: True,
            on_fallback=self.transitions.append,
            status_cb=self.statuses.append,
        )
        settings.update(options)
        return ClipboardSpeechEngine(self.online, self.offline, **settings)

    def speak(self, engine, text="The entire copied sentence.", **options):
        return engine.synth_to_wav(
            text, "en-US-AndrewNeural", "en-US", 1.25, 0.7,
            str(self.output), **options)

    def test_fallback_preserves_entire_failed_chunk_and_selected_controls(self):
        error = OnlineSpeechError("empty_response")
        self.online.results = [error]
        engine = self.wrapper()
        text = "Start. " + ("Every important detail stays. " * 30) + "End."

        self.speak(engine, text)

        self.assertEqual(self.online.calls[0][:5],
                         (text, "en-US-AndrewNeural", "en-US", 1.25, 0.7))
        self.assertEqual(self.offline.calls[0][:5],
                         (text, "en_US-lessac-medium", "en-us", 1.25, 0.7))
        self.assertEqual(self.offline.calls[0][6], {})
        self.assertNotEqual(self.offline.calls[0][5], self.output)
        with wave.open(str(self.output), "rb") as stream:
            self.assertEqual(stream.readframes(stream.getnframes()),
                             text.encode("utf-8") * 2)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.output])
        self.assertEqual(engine.active_engine_key, "piper")
        self.assertEqual(engine.fallback_reason, "empty_response")
        self.assertIsNone(engine.fallback_http_status)

    def test_previous_chunk_is_not_replayed_and_remaining_chunks_stay_offline(self):
        error = OnlineSpeechError("rejected", 403)
        self.online.results = [None, error]
        engine = self.wrapper()

        for text in ("Already completed.", "Current chunk.", "Next chunk."):
            self.speak(engine, text)

        self.assertEqual([call[0] for call in self.online.calls],
                         ["Already completed.", "Current chunk."])
        self.assertEqual([call[0] for call in self.offline.calls],
                         ["Current chunk.", "Next chunk."])
        self.assertEqual(self.transitions, [error])
        self.assertEqual(self.statuses, [
            "Microsoft voice unavailable. Continuing with Lessac offline."])
        self.assertEqual(engine.fallback_reason, "rejected")
        self.assertEqual(engine.fallback_http_status, 403)

    def test_pre_cancelled_request_calls_neither_engine(self):
        event = threading.Event()
        event.set()
        engine = self.wrapper()
        self.output.write_bytes(b"existing audio")

        with self.assertRaises(SpeechCancelled):
            self.speak(engine, cancel_event=event)

        self.assertEqual(self.online.calls, [])
        self.assertEqual(self.offline.calls, [])
        self.assertEqual(self.transitions, [])
        self.assertEqual(self.output.read_bytes(), b"existing audio")

    def test_cancellation_during_online_failure_never_falls_back(self):
        event = threading.Event()

        def cancel_and_fail(path, kwargs):
            self.assertIs(kwargs["cancel_event"], event)
            event.set()
            raise OnlineSpeechError("timeout")

        self.online.results = [cancel_and_fail]
        engine = self.wrapper()
        with self.assertRaises(SpeechCancelled):
            self.speak(engine, cancel_event=event)

        self.assertEqual(self.offline.calls, [])
        self.assertEqual(self.transitions, [])
        self.assertEqual(engine.active_engine_key, "edge")

    def test_online_cancellation_and_unexpected_errors_propagate_without_fallback(self):
        for error in (SpeechCancelled("stopped"), ValueError("bad argument")):
            with self.subTest(error=type(error).__name__):
                self.online.results = [error]
                engine = self.wrapper()
                with self.assertRaises(type(error)) as raised:
                    self.speak(engine)
                self.assertIs(raised.exception, error)
        self.assertEqual(self.offline.calls, [])
        self.assertEqual(self.transitions, [])

    def test_unavailable_offline_voice_rethrows_original_without_download_or_transition(self):
        error = OnlineSpeechError("network")
        self.online.results = [error]
        engine = self.wrapper(offline_available=lambda: False)

        with self.assertRaises(OnlineSpeechError) as raised:
            self.speak(engine)

        self.assertIs(raised.exception, error)
        self.assertEqual(self.offline.calls, [])
        self.assertEqual(self.offline.install_calls, 0)
        self.assertEqual(self.transitions, [])
        self.assertEqual(engine.active_engine_key, "edge")

    def test_offline_write_failure_preserves_existing_destination_and_cleans_temp(self):
        self.online.results = [OnlineSpeechError("timeout")]

        def fail_mid_write(path, kwargs):
            path.write_bytes(b"incomplete audio")
            raise OSError("local write failed")

        self.offline.results = [fail_mid_write]
        engine = self.wrapper()
        self.output.write_bytes(b"previous complete audio")

        with self.assertRaises(OSError):
            self.speak(engine)

        self.assertEqual(self.output.read_bytes(), b"previous complete audio")
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.output])

    def test_cancellation_during_offline_work_preserves_output_and_cleans_temp(self):
        event = threading.Event()
        self.online.results = [OnlineSpeechError("timeout")]
        self.offline.results = [lambda path, kwargs: event.set()]
        engine = self.wrapper()
        self.output.write_bytes(b"previous complete audio")

        with self.assertRaises(SpeechCancelled):
            self.speak(engine, cancel_event=event)

        self.assertEqual(self.output.read_bytes(), b"previous complete audio")
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.output])

    def test_start_offline_uses_local_engine_without_transition_or_online_request(self):
        engine = self.wrapper(start_offline=True)
        self.assertEqual(engine.active_engine_key, "piper")
        self.assertIsNone(engine.fallback_reason)
        self.assertIsNone(engine.fallback_http_status)

        self.assertTrue(engine.ready())
        self.assertTrue(engine.install())
        self.assertFalse(engine.warm("en-US-AndrewNeural", "en-US", force=True))
        self.speak(engine)

        self.assertEqual(self.online.calls, [])
        self.assertEqual(self.online.ready_calls, 0)
        self.assertEqual(self.online.install_calls, 0)
        self.assertEqual(self.offline.ready_calls, 2)
        self.assertEqual(self.offline.install_calls, 0)
        self.assertEqual(len(self.offline.calls), 1)
        self.assertEqual(self.transitions, [])
        self.assertEqual(self.statuses, [])

    def test_start_offline_rechecks_model_availability_before_synthesis(self):
        engine = self.wrapper(start_offline=True,
                              offline_available=lambda: False)

        self.assertFalse(engine.ready())
        self.assertFalse(engine.install())
        with self.assertRaisesRegex(RuntimeError, "offline voice.*available"):
            self.speak(engine)

        self.assertEqual(self.online.calls, [])
        self.assertEqual(self.offline.calls, [])
        self.assertEqual(self.offline.install_calls, 0)

    def test_construction_and_warm_do_no_work_and_online_readiness_delegates(self):
        engine = self.wrapper()
        self.assertEqual(engine.name, "Microsoft online")
        self.assertEqual(engine.chunk_len, 700)
        self.assertEqual(engine.initial_chunk_len, 150)
        self.assertTrue(engine.lookahead)
        self.assertTrue(engine.supports_cancellation)
        self.assertEqual(self.online.ready_calls, 0)
        self.assertEqual(self.offline.ready_calls, 0)
        self.assertFalse(engine.warm("en-US-AndrewNeural"))
        self.assertTrue(engine.ready())
        self.assertTrue(engine.install())
        self.assertEqual(self.online.ready_calls, 1)
        self.assertEqual(self.online.install_calls, 1)
        self.assertEqual(self.online.calls, [])
        self.assertEqual(self.offline.calls, [])

    def test_callback_failures_do_not_prevent_offline_audio(self):
        def broken_callback(*args):
            raise RuntimeError("UI no longer available")

        self.online.results = [OnlineSpeechError("timeout")]
        engine = self.wrapper(on_fallback=broken_callback,
                              status_cb=broken_callback)
        self.speak(engine)
        self.assertTrue(self.output.is_file())
        self.assertEqual(engine.active_engine_key, "piper")

    def test_real_engine_local_only_method_is_preferred_without_cancel_argument(self):
        class LocalOnlyEngine(FakeEngine):
            def synth_to_wav(self, *args, **kwargs):
                raise AssertionError("Download-capable synthesis must not run")

            def synth_to_wav_local(self, text, voice_id, lang, speed, volume,
                                   out_path):
                return super().synth_to_wav(
                    text, voice_id, lang, speed, volume, out_path)

        self.offline = LocalOnlyEngine()
        self.online.results = [OnlineSpeechError("timeout")]
        engine = self.wrapper()
        self.speak(engine, cancel_event=threading.Event())

        self.assertEqual(len(self.offline.calls), 1)
        self.assertEqual(self.offline.calls[0][6], {})
        self.assertTrue(self.output.is_file())


if __name__ == "__main__":
    unittest.main()
