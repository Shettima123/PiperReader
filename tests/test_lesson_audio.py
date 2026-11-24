"""Offline coverage of multi-speaker streaming and failure-safe WAV exports."""

import os
import struct
import tempfile
import threading
import unittest
import wave

import edge_engine as EDGE
import lesson_audio as AUDIO


class StubEngine:
    def __init__(self, fail_at=None, cancel_event=None, rate=24000,
                 channels=1, sample_width=2):
        self.calls = []
        self.fail_at = fail_at
        self.cancel_event = cancel_event
        self.rate = rate
        self.channels = channels
        self.sample_width = sample_width

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path,
                     cancel_event=None):
        self.calls.append((text, voice_id, lang, speed, volume, cancel_event))
        if len(self.calls) == self.fail_at:
            with open(out_path, "wb") as failed:
                failed.write(b"unfinished speech response")
            raise RuntimeError("Speech request failed")
        with wave.open(out_path, "wb") as speech:
            speech.setnchannels(self.channels)
            speech.setsampwidth(self.sample_width)
            speech.setframerate(self.rate)
            value = 1000 if voice_id == EDGE.ANDREW_VOICE_ID else -1000
            speech.writeframes(struct.pack("<h", value) * 240)
        if self.cancel_event is not None:
            self.cancel_event.set()


class SplitSpeechTests(unittest.TestCase):
    def test_long_paragraphs_preserve_every_word_in_order(self):
        text = "\t" + " ".join("word%04d" % n for n in range(1200)) + "\n\nLast paragraph."
        chunks = AUDIO.split_speech_text(text)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(chunk) <= 700 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), text.split())
        self.assertEqual(chunks[-1], "Last paragraph.")

    def test_prefers_complete_sentence_and_preserves_long_word(self):
        first = "One short explanation. " * 20
        second = "Another longer explanation. " * 20
        chunks = AUDIO.split_speech_text(first + second)
        self.assertTrue(chunks[0].endswith("."))
        self.assertEqual(" ".join(chunks).split(), (first + second).split())
        long_word = "x" * 1000
        self.assertEqual(AUDIO.split_speech_text(long_word + " next"),
                         [long_word, "next"])


class LessonAudioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = os.path.join(self.directory.name, "lesson.wav")
        self.voice_map = {"Host": EDGE.ANDREW_LABEL,
                          "Guest": (EDGE.AVA_VOICE_ID, "en-us"),
                          "Narrator": EDGE.SONIA_LABEL}

    def render(self, segments, **kwargs):
        return AUDIO.render_lesson_audio(segments, self.output, self.voice_map,
                                         **kwargs)

    def existing_output(self):
        with open(self.output, "wb") as output:
            output.write(b"Existing recording must be preserved")

    def assert_existing_output_and_no_temporary_files(self):
        with open(self.output, "rb") as output:
            self.assertEqual(output.read(), b"Existing recording must be preserved")
        self.assertEqual(os.listdir(self.directory.name), ["lesson.wav"])

    def test_speaker_switching_pcm_pauses_and_chunk_progress(self):
        engine = StubEngine()
        event = threading.Event()
        progress = []
        host_text = " ".join("explanation%04d" % n for n in range(120))
        segments = [{"speaker": "Host", "text": host_text, "pause_after": 0.25},
                    {"speaker": "Guest", "text": "Here is an example.", "pause_after": 0.1}]
        result = self.render(segments, engine=engine, speed=1.2, volume=0.6,
                             cancel_event=event, progress=lambda *p: progress.append(p))
        host_chunks = AUDIO.split_speech_text(host_text)
        count = len(host_chunks) + 1
        self.assertEqual([call[0] for call in engine.calls],
                         host_chunks + ["Here is an example."])
        self.assertEqual([call[1] for call in engine.calls],
                         [EDGE.ANDREW_VOICE_ID] * len(host_chunks) + [EDGE.AVA_VOICE_ID])
        self.assertTrue(all(call[3:] == (1.2, 0.6, event) for call in engine.calls))
        self.assertEqual(progress[0], (0, count, ""))
        self.assertEqual([p[0] for p in progress], list(range(count + 1)))
        self.assertEqual(progress[-1], (count, count, "Guest"))
        expected_frames = count * 240 + 8400
        with wave.open(self.output, "rb") as output:
            self.assertEqual((output.getnchannels(), output.getsampwidth(),
                              output.getframerate(), output.getcomptype()),
                             (1, 2, 24000, "NONE"))
            self.assertEqual(output.getnframes(), expected_frames)
            pcm = output.readframes(expected_frames)
        values = struct.unpack("<%dh" % expected_frames, pcm)
        boundary = len(host_chunks) * 240
        self.assertEqual(values[:boundary], (1000,) * boundary)
        self.assertEqual(values[boundary:boundary + 6000], (0,) * 6000)
        self.assertEqual(values[boundary + 6000:boundary + 6240], (-1000,) * 240)
        self.assertEqual(values[-2400:], (0,) * 2400)
        self.assertAlmostEqual(result["duration_seconds"], expected_frames / 24000)
        self.assertEqual(result["sample_rate"], 24000)
        self.assertEqual(result["segments"][0]["chunks"], len(host_chunks))
        self.assertAlmostEqual(result["segments"][1]["start_seconds"],
                               len(host_chunks) * 0.01 + 0.25)
        self.assertEqual(os.listdir(self.directory.name), ["lesson.wav"])

    def test_study_pauses_are_bounded_and_invalid_values_use_default(self):
        pauses = [-5, 99, float("nan"), float("inf"), "bad", None, True]
        expected = [0, 10, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4]
        segments = [{"speaker": "Narrator", "text": "Reflect.", "pause_after": pause}
                    for pause in pauses]
        segments.append({"speaker": "Narrator", "text": "Continue."})
        result = self.render(segments, engine=StubEngine())
        self.assertEqual([item["pause_after"] for item in result["segments"]], expected)
        self.assertAlmostEqual(result["duration_seconds"], sum(expected) + 0.08)

    def test_failure_after_first_chunk_preserves_existing_output(self):
        self.existing_output()
        text = "A detailed explanation. " * 100
        with self.assertRaisesRegex(RuntimeError, "Speech request failed"):
            self.render([{"speaker": "Host", "text": text}], engine=StubEngine(fail_at=2))
        self.assert_existing_output_and_no_temporary_files()

    def test_cancel_from_engine_is_propagated_and_preserves_existing_output(self):
        self.existing_output()
        event = threading.Event()
        with self.assertRaises(EDGE.SpeechCancelled):
            self.render([{"speaker": "Host", "text": "Hello."}],
                        engine=StubEngine(cancel_event=event), cancel_event=event)
        self.assert_existing_output_and_no_temporary_files()

    def test_cancel_during_pause_does_not_publish_complete_speech(self):
        self.existing_output()
        event = threading.Event()
        def on_progress(completed, total, speaker):
            if completed:
                event.set()
        with self.assertRaises(EDGE.SpeechCancelled):
            self.render([{"speaker": "Guest", "text": "Think about this.", "pause_after": 10}],
                        engine=StubEngine(), cancel_event=event, progress=on_progress)
        self.assert_existing_output_and_no_temporary_files()

    def test_pre_cancel_does_not_make_any_requests_or_create_files(self):
        engine = StubEngine()
        event = threading.Event()
        event.set()
        with self.assertRaises(EDGE.SpeechCancelled):
            self.render([{"speaker": "Host", "text": "Hello."}], engine=engine,
                        cancel_event=event)
        self.assertEqual(engine.calls, [])
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_wrong_audio_format_preserves_existing_output(self):
        for kwargs in ({"rate": 22050}, {"channels": 2}, {"sample_width": 1}):
            with self.subTest(kwargs=kwargs):
                self.existing_output()
                with self.assertRaisesRegex(ValueError, "mono PCM16"):
                    self.render([{"speaker": "Host", "text": "Hello."}],
                                engine=StubEngine(**kwargs))
                self.assert_existing_output_and_no_temporary_files()

    def test_invalid_lesson_is_rejected_before_any_speech_request(self):
        cases = [[], [{"speaker": "Host", "text": "  "}],
                 [{"speaker": "Unknown", "text": "Hello."}],
                 [{"speaker": "Host", "text": None}], "some text", None,
                 [{"speaker": ["Host"], "text": "Hello."}]]
        for segments in cases:
            with self.subTest(segments=segments):
                engine = StubEngine()
                with self.assertRaises(ValueError):
                    self.render(segments, engine=engine)
                self.assertEqual(engine.calls, [])
                self.assertEqual(os.listdir(self.directory.name), [])

    def test_truncated_response_does_not_publish_partial_audio(self):
        self.existing_output()
        class TruncatedEngine(StubEngine):
            def synth_to_wav(self, *args, **kwargs):
                super().synth_to_wav(*args, **kwargs)
                with open(args[5], "r+b") as output:
                    output.truncate(100)
        with self.assertRaisesRegex(ValueError, "incomplete audio"):
            self.render([{"speaker": "Host", "text": "Hello."}],
                        engine=TruncatedEngine())
        self.assert_existing_output_and_no_temporary_files()

    def test_success_replaces_existing_output_with_complete_wav(self):
        self.existing_output()
        self.render([{"speaker": "Guest", "text": "New recording.", "pause_after": 0}],
                    engine=StubEngine())
        with wave.open(self.output, "rb") as output:
            self.assertEqual(output.getnframes(), 240)
            self.assertEqual(output.readframes(240), struct.pack("<h", -1000) * 240)
        self.assertEqual(os.listdir(self.directory.name), ["lesson.wav"])

    def test_unmapped_or_unsupported_voice_fails_before_network(self):
        for mapping in ({}, {"Host": "Andrew"},
                        {"Host": (EDGE.ANDREW_VOICE_ID, "wrong-language")}):
            with self.subTest(mapping=mapping):
                engine = StubEngine()
                with self.assertRaises(ValueError):
                    AUDIO.render_lesson_audio([{"speaker": "Host", "text": "Hello."}],
                                              self.output, mapping, engine=engine)
                self.assertEqual(engine.calls, [])


if __name__ == "__main__":
    unittest.main()
