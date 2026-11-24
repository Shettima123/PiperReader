"""Offline tests: no account, network access, or real text transmission."""

import asyncio
import importlib.machinery
import importlib.util
import io
import os
import queue
import tempfile
import threading
import unittest
import wave
from collections import deque
from unittest import mock

import numpy as np

import edge_engine as EDGE


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADER = importlib.machinery.SourceFileLoader(
    "piper_reader_edge_tests", os.path.join(ROOT, "piper_reader.pyw"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)

# Expected saved labels and service voices are independent of the app catalog.
ONLINE_CASES = (
    ("Andrew (US male) - Microsoft, online", "en-US-AndrewMultilingualNeural", "en-us"),
    ("Ava (US female) - Microsoft, online", "en-US-AvaMultilingualNeural", "en-us"),
    ("Emma (US female) - Microsoft, online", "en-US-EmmaMultilingualNeural", "en-us"),
    ("Sonia (UK female) - Microsoft, online", "en-GB-SoniaNeural", "en-gb"),
)


class EdgeRateTests(unittest.TestCase):
    def test_speed_matches_reader_multiplier(self):
        for speed, rate in ((0.5, "-50%"), (0.8, "-20%"), (1, "+0%"),
                            (1.05, "+5%"), (1.5, "+50%"), (2.5, "+150%")):
            with self.subTest(speed=speed):
                self.assertEqual(EDGE.edge_rate(speed), rate)

    def test_invalid_values_match_reader_bounds_and_defaults(self):
        for value in (-10, 0, 900, None, "bad", float("inf"), float("nan"),
                      True, "1.25"):
            with self.subTest(value=value):
                expected = round((APP.normalize_speed(value) - 1) * 100)
                self.assertEqual(EDGE.edge_rate(value), f"{expected:+d}%")


class EdgeEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = os.path.join(self.directory.name, "speech.wav")
        self.engine = EDGE.EdgeEngine(mock.Mock())

    def synthesize(self, volume=1.0, **kwargs):
        self.engine.synth_to_wav(
            "Test speech.", EDGE.ANDREW_VOICE_ID, "en-us", 1.0, volume,
            self.output, **kwargs)

    def test_transport_selects_exact_voice_and_ignores_metadata(self):
        class Communicate:
            async def stream(self):
                yield {"type": "SentenceBoundary", "text": "Test speech."}
                yield {"type": "audio", "data": b"first"}
                yield {"type": "audio", "data": b"second"}

        samples = np.array([[0.25], [-0.25]], dtype=np.float32)
        for label, voice_id, lang in ONLINE_CASES:
            with self.subTest(voice=label), mock.patch(
                    "edge_tts.Communicate", return_value=Communicate()) as factory, \
                    mock.patch("soundfile.read", return_value=(samples, 24000)) as decoder:
                self.engine.synth_to_wav(
                    "Test speech.", voice_id, lang, 1.2, 1, self.output)
                factory.assert_called_once_with(
                    "Test speech.", voice_id, rate="+20%", volume="+0%",
                    connect_timeout=10, receive_timeout=20)
                self.assertEqual(decoder.call_args.args[0].getvalue(), b"firstsecond")
                with wave.open(self.output, "rb") as stream:
                    self.assertEqual(stream.getnframes(), 2)

    def test_pcm_wav_has_local_volume_and_expected_format(self):
        samples = np.array([[1.0], [-1.0], [0.5], [0.0]], dtype=np.float32)
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock(
                return_value=b"mock MP3")), mock.patch(
                    "soundfile.read", return_value=(samples, 24000)) as decoder:
            self.synthesize(volume=0.5)
        decoder.assert_called_once()
        self.assertEqual(decoder.call_args.kwargs,
                         {"dtype": "float32", "always_2d": True})
        with wave.open(self.output, "rb") as stream:
            self.assertEqual((stream.getnchannels(), stream.getsampwidth(),
                              stream.getframerate(), stream.getcomptype()),
                             (1, 2, 24000, "NONE"))
            values = np.frombuffer(stream.readframes(100), dtype="<i2")
        np.testing.assert_array_equal(values, [16383, -16383, 8191, 0])
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_volume_bounds_silence_and_invalid_default(self):
        samples = np.ones((3, 1), dtype=np.float32)
        for volume, expected in ((-1, 0), (0, 0), (5, 32767),
                                 (float("nan"), 32767), (None, 32767)):
            with self.subTest(volume=volume), mock.patch.object(
                    self.engine, "_receive", new=mock.AsyncMock(return_value=b"mp3")), \
                    mock.patch("soundfile.read", return_value=(samples, 24000)):
                self.synthesize(volume=volume)
                with wave.open(self.output, "rb") as stream:
                    values = np.frombuffer(stream.readframes(100), dtype="<i2")
                np.testing.assert_array_equal(values, [expected] * 3)

    def test_network_failure_is_actionable_and_preserves_existing_output(self):
        with open(self.output, "wb") as stream:
            stream.write(b"previous output")
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock(
                side_effect=ConnectionError("private service details"))):
            with self.assertRaisesRegex(RuntimeError, "internet.*offline") as caught:
                self.synthesize()
        self.assertNotIn("private service details", str(caught.exception))
        with open(self.output, "rb") as stream:
            self.assertEqual(stream.read(), b"previous output")
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_failed_write_cleans_partial_file_and_preserves_existing(self):
        with open(self.output, "wb") as stream:
            stream.write(b"previous output")
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock(
                return_value=b"mp3")), mock.patch("soundfile.read", return_value=(
                    np.ones((4, 1), dtype=np.float32), 24000)), mock.patch.object(
                        EDGE.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk space"):
                self.synthesize()
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])
        with open(self.output, "rb") as stream:
            self.assertEqual(stream.read(), b"previous output")

    def test_bad_audio_never_creates_output(self):
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock(
                return_value=b"mp3")), mock.patch("soundfile.read", return_value=(
                    np.array([[np.nan]], dtype=np.float32), 24000)):
            with self.assertRaises(RuntimeError):
                self.synthesize()
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_timeout_cancels_and_closes_transport(self):
        closed = threading.Event()

        async def stalled(*_):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        self.engine.request_timeout = 0.02
        with mock.patch.object(self.engine, "_receive", new=stalled):
            with self.assertRaisesRegex(RuntimeError, "internet"):
                self.synthesize()
        self.assertTrue(closed.is_set())
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_stop_cancels_inflight_transport(self):
        cancelled = threading.Event()
        closed = threading.Event()

        async def cancel_inflight(*_):
            cancelled.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with mock.patch.object(self.engine, "_receive", new=cancel_inflight):
            with self.assertRaises(EDGE.SpeechCancelled):
                self.synthesize(cancel_event=cancelled)
        self.assertTrue(closed.is_set())
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_cancelled_request_never_contacts_service(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock()) as receive:
            with self.assertRaises(EDGE.SpeechCancelled):
                self.synthesize(cancel_event=cancelled)
        receive.assert_not_awaited()

    def test_warm_and_readiness_make_no_speech_request(self):
        with mock.patch("edge_tts.Communicate") as communicate:
            self.assertTrue(self.engine.ready())
            for _, voice_id, lang in ONLINE_CASES:
                self.assertFalse(self.engine.warm(voice_id, lang, force=True))
        communicate.assert_not_called()

    def test_wrong_voice_and_empty_text_are_rejected_before_network(self):
        with mock.patch.object(self.engine, "_receive", new=mock.AsyncMock()) as receive:
            for text, voice in (("", EDGE.ANDREW_VOICE_ID), ("test", "other")):
                with self.assertRaises(ValueError):
                    self.engine.synth_to_wav(text, voice, None, 1, 1, self.output)
        receive.assert_not_awaited()


class EdgeRecoveryTests(unittest.TestCase):
    """Exercise recovery around a fake service, with the real WAV pipeline."""

    def setUp(self):
        from edge_tts.exceptions import NoAudioReceived
        self.no_audio = NoAudioReceived
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = os.path.join(self.directory.name, "speech.wav")
        self.statuses = []
        self.engine = EDGE.EdgeEngine(
            self.statuses.append, max_attempts=2, request_timeout=1.0)
        self.engine.retry_delay = 0.01
        encoded = io.BytesIO()
        with wave.open(encoded, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b"\x00\x20\x00\xe0" * 30)
        self.audio = encoded.getvalue()

    def synthesize(self, cancel_event=None):
        self.engine.synth_to_wav(
            "First sentence. Keep every word!", EDGE.ANDREW_VOICE_ID,
            "en-us", 1.0, 1.0, self.output, cancel_event=cancel_event)

    def test_empty_response_retries_same_passage_once_and_publishes_valid_audio(self):
        calls = []
        async def service(text, voice, rate):
            calls.append((text, voice, rate))
            if len(calls) == 1:
                raise self.no_audio("private service text")
            return self.audio
        with mock.patch.object(self.engine, "_receive", new=service):
            self.synthesize()
        self.assertEqual(calls, [
            ("First sentence. Keep every word!", EDGE.ANDREW_VOICE_ID, "+0%"),
            ("First sentence. Keep every word!", EDGE.ANDREW_VOICE_ID, "+0%")])
        with wave.open(self.output, "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(),
                              wav.getframerate(), wav.getnframes()), (1, 2, 24000, 60))
        self.assertEqual(len(self.statuses), 1)
        self.assertNotIn("private", " ".join(self.statuses))

    def test_repeated_empty_responses_stop_after_two_and_preserve_output(self):
        with open(self.output, "wb") as stream:
            stream.write(b"previous audio")
        service = mock.AsyncMock(side_effect=self.no_audio("private text and URL"))
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.OnlineSpeechError) as caught:
                self.synthesize()
        self.assertEqual(service.await_count, 2)
        self.assertEqual(caught.exception.reason, "empty_response")
        self.assertNotIn("private", str(caught.exception))
        with open(self.output, "rb") as stream:
            self.assertEqual(stream.read(), b"previous audio")
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_deadline_is_shared_with_backoff_and_second_attempt_is_reaped(self):
        self.engine.request_timeout = 0.12
        calls = []
        closed = threading.Event()
        async def service(*args):
            calls.append(args)
            if len(calls) == 1:
                await asyncio.sleep(0.07)
                raise self.no_audio("empty")
            try:
                await asyncio.sleep(0.09)
                return self.audio
            finally:
                closed.set()
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.OnlineSpeechError) as caught:
                self.synthesize()
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertTrue(closed.is_set())
        self.assertFalse(os.path.exists(self.output))

    def test_deadline_expiring_in_backoff_prevents_second_attempt(self):
        self.engine.request_timeout = 0.03
        self.engine.retry_delay = 0.35
        service = mock.AsyncMock(side_effect=self.no_audio("empty"))
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.OnlineSpeechError) as caught:
                self.synthesize()
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertEqual(service.await_count, 1)
        self.assertFalse(os.path.exists(self.output))

    def test_cancel_during_backoff_prevents_second_attempt(self):
        cancel = threading.Event()
        self.engine.retry_delay = 0.35
        def status(message):
            self.statuses.append(message)
            asyncio.get_running_loop().call_later(0.01, cancel.set)
        self.engine.status = status
        service = mock.AsyncMock(side_effect=self.no_audio("empty"))
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.SpeechCancelled):
                self.synthesize(cancel)
        self.assertEqual(service.await_count, 1)
        self.assertEqual(len(self.statuses), 1)
        self.assertFalse(os.path.exists(self.output))

    def test_cancel_with_empty_response_does_not_post_retry_status(self):
        cancel = threading.Event()
        async def service(*_):
            cancel.set()
            raise self.no_audio("empty")
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.SpeechCancelled):
                self.synthesize(cancel)
        self.assertEqual(self.statuses, [])
        self.assertFalse(os.path.exists(self.output))

    def test_rejected_http_is_safe_and_never_retried(self):
        import aiohttp
        from types import SimpleNamespace
        for status in (400, 403, 429, 503):
            error = aiohttp.WSServerHandshakeError(
                SimpleNamespace(real_url="https://private.example?secret=hidden"),
                (), status=status, message="private response")
            service = mock.AsyncMock(side_effect=error)
            with self.subTest(status=status), mock.patch.object(
                    self.engine, "_receive", new=service):
                with self.assertRaises(EDGE.OnlineSpeechError) as caught:
                    self.synthesize()
                self.assertEqual(service.await_count, 1)
                self.assertEqual(caught.exception.reason, "rejected")
                self.assertEqual(caught.exception.http_status, status)
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.statuses, [])

    def test_network_error_is_categorized_without_retry(self):
        service = mock.AsyncMock(side_effect=ConnectionError("private network data"))
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaises(EDGE.OnlineSpeechError) as caught:
                self.synthesize()
        self.assertEqual(service.await_count, 1)
        self.assertEqual(caught.exception.reason, "network")
        self.assertIsNone(caught.exception.http_status)
        self.assertNotIn("private", str(caught.exception))

    def test_default_does_not_retry_and_custom_policy_is_instance_local(self):
        default = EDGE.EdgeEngine(self.statuses.append)
        service = mock.AsyncMock(side_effect=self.no_audio("empty"))
        with mock.patch.object(default, "_receive", new=service):
            with self.assertRaises(RuntimeError):
                default.synth_to_wav("Text", EDGE.ANDREW_VOICE_ID, "en-us",
                                     1, 1, self.output)
        self.assertEqual(service.await_count, 1)
        custom = EDGE.EdgeEngine(self.statuses.append, max_attempts=2, request_timeout=12)
        self.assertEqual((custom.max_attempts, custom.request_timeout), (2, 12))
        self.assertEqual((default.max_attempts, default.request_timeout), (1, 30))

    def test_decode_failure_is_never_retried(self):
        service = mock.AsyncMock(return_value=b"not audio")
        with mock.patch.object(self.engine, "_receive", new=service):
            with self.assertRaisesRegex(RuntimeError, "audio could not be saved"):
                self.synthesize()
        self.assertEqual(service.await_count, 1)
        self.assertEqual(self.statuses, [])
        self.assertFalse(os.path.exists(self.output))


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ReaderIntegrationTests(unittest.TestCase):
    def test_online_choices_route_to_edge_without_changing_local_defaults(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        engine = EDGE.EdgeEngine(mock.Mock())
        app.engines = {"edge": engine}
        for label, voice_id, lang in ONLINE_CASES:
            with self.subTest(voice=label):
                app.voice_var = _Value(label)
                self.assertEqual(app.current_engine(), (engine, voice_id, lang))
        self.assertEqual(app._voice_snapshot("Emma (British female) - Kokoro"),
                         ("kokoro", "bf_emma", "en-gb"))
        self.assertEqual(APP.VOICES[APP.DEFAULT_VOICE][0], "piper")
        self.assertEqual(APP.VOICES[APP.DEFAULT_CLIPBOARD_VOICE][0], "piper")

    def test_persistent_notice_covers_primary_or_clipboard_voice(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.voice_privacy_var = _Value("")
        app.voice_var = _Value(APP.DEFAULT_VOICE)
        app.clipboard_voice_var = _Value(APP.DEFAULT_CLIPBOARD_VOICE)
        for label, _, _ in ONLINE_CASES:
            for field in (app.voice_var, app.clipboard_voice_var):
                with self.subTest(voice=label, field=field):
                    original = field.get()
                    field.set(label)
                    app._update_voice_notice()
                    notice = app.voice_privacy_var.get()
                    self.assertIn("text sent to Microsoft", notice)
                    self.assertNotIn("Andrew", notice)
                    field.set(original)
        app._update_voice_notice()
        self.assertIn("local voices", app.voice_privacy_var.get())

    def test_online_saved_selections_are_excluded_from_app_warmup(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.engines = {"edge": mock.Mock()}
        with mock.patch("threading.Thread") as worker:
            for label, _, _ in ONLINE_CASES:
                self.assertFalse(app._warm_voice_label_async(label, force=True))
        worker.assert_not_called()
        app.engines["edge"].warm.assert_not_called()

    def test_clipboard_uses_its_own_voice_without_changing_primary_selection(self):
        for label, voice_id, lang in ONLINE_CASES:
            with self.subTest(voice=label):
                app, _, _ = self._app_with_active_export()
                app._shutdown_event = threading.Event()
                app.voice_var = _Value(APP.DEFAULT_VOICE)
                app.clipboard_voice_var = _Value(label)
                app.speed_var = _Value(0.95)
                app.vol_var = _Value(0.8)
                app._repeat_snapshot = lambda: ("off", 1)
                app._queue_clipboard_text("Chosen clipboard voice.")
                request = app.speak_q.get_nowait()
                self.assertEqual(request[0:2], ("clipboard", "Chosen clipboard voice."))
                self.assertEqual(request[4:9], ("edge", voice_id, lang, 0.95, 0.8))
                self.assertEqual(app.voice_var.get(), APP.DEFAULT_VOICE)

    def test_chunking_preserves_full_text_and_short_opening(self):
        text = "An example paragraph with ordinary words. " * 60
        units = APP.build_repeat_units(
            "text", text, "off", EDGE.EdgeEngine.chunk_len,
            first_max_len=EDGE.EdgeEngine.initial_chunk_len)
        chunks = [part for unit in units for part, _ in unit]
        self.assertLessEqual(len(chunks[0]), 150)
        self.assertGreater(max(map(len, chunks[1:])), 150)
        self.assertLessEqual(max(map(len, chunks)), 700)
        self.assertEqual(" ".join(chunks), text.strip())

    def test_lookahead_passes_stop_event_and_cancels_remaining_chunks(self):
        engine = EDGE.EdgeEngine(mock.Mock())
        cancel = threading.Event()
        rendered = []

        def synth(*args, cancel_event=None):
            self.assertIs(cancel_event, cancel)
            rendered.append(args[0])

        engine.synth_to_wav = synth
        items = [(f"Text {index}", f"file{index}.wav") for index in range(8)]
        iterator = APP.ReaderApp._iter_synthesized_chunks(
            engine, items, EDGE.ANDREW_VOICE_ID, "en-us", 1, 1, cancel)
        self.assertEqual(next(iterator), items[0])
        cancel.set()
        iterator.close()
        # At most one additional chunk can already have entered lookahead.
        self.assertLessEqual(len(rendered), 2)

    def test_export_stop_preserves_destination_and_cleans_all_partial_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            app = APP.ReaderApp.__new__(APP.ReaderApp)
            app.tmpdir = directory
            app.set_status = mock.Mock()
            cancel = threading.Event()
            output = os.path.join(directory, "export.wav")
            with open(output, "wb") as stream:
                stream.write(b"existing audiobook")
            engine = EDGE.EdgeEngine(mock.Mock())
            rendered = []

            def synth(text, _vid, _lang, _speed, _vol, path, cancel_event=None):
                self.assertIs(cancel_event, cancel)
                rendered.append(text)
                with wave.open(path, "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(24000)
                    stream.writeframes(b"\0\0" * 10)
                if len(rendered) == 2:
                    cancel.set()

            engine.synth_to_wav = synth
            with self.assertRaises(EDGE.SpeechCancelled):
                app._synthesize_text_to_wav(
                    engine, EDGE.ANDREW_VOICE_ID, "en-us", 1, 1,
                    "Example sentences should be read. " * 100, output, cancel)
            self.assertEqual(len(rendered), 2)
            self.assertEqual(os.listdir(directory), ["export.wav"])
            with open(output, "rb") as stream:
                self.assertEqual(stream.read(), b"existing audiobook")

    def _app_with_active_export(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._studio_clipboard_queue = deque()
        app.auto_var = _Value(True)
        app._content_load_serial = 0
        app._browser_active_session = None
        app._browser_bridge = None
        app._playback_lock = threading.RLock()
        speech, export = threading.Event(), threading.Event()
        app._speech_requests = {speech}
        app._online_export_cancels = {export}
        app._active_cancel = speech
        app.player = mock.Mock()
        app._pause_requested = threading.Event()
        app.speak_q = queue.Queue()
        app._set_playback_state = mock.Mock()
        app.set_status = mock.Mock()
        return app, speech, export

    def test_stop_signals_online_export_and_speech_events(self):
        app, speech, export = self._app_with_active_export()
        app.stop()
        self.assertTrue(speech.is_set())
        self.assertTrue(export.is_set())
        app.set_status.assert_called_once_with("Stopped.")

    def test_preview_replaces_speech_but_preserves_independent_export(self):
        for label, voice_id, lang in ONLINE_CASES:
            with self.subTest(voice=label):
                app, previous_speech, export = self._app_with_active_export()
                app._shutdown_event = threading.Event()
                app.voice_var = _Value(label)
                app.speed_var = _Value(1.0)
                app.vol_var = _Value(1.0)
                app.preview_voice()
                self.assertTrue(previous_speech.is_set())
                self.assertFalse(export.is_set())
                preview = app.speak_q.get_nowait()
                self.assertEqual(preview[4:7], ("edge", voice_id, lang))
                self.assertIn("a free Microsoft online voice", preview[1])
                self.assertNotIn("built-in Windows", preview[1])
                self.assertFalse(preview[2].is_set())
                # The user's subsequent explicit Stop still stops both jobs.
                app.stop()
                self.assertTrue(preview[2].is_set())
                self.assertTrue(export.is_set())


if __name__ == "__main__":
    unittest.main()
