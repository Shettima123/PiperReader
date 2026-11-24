"""Offline integration checks for resumable, complete-text audio rendering."""

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import wave
from unittest import mock

from edge_engine import ANDREW_LABEL, SpeechCancelled
from gemini_engine import DEFAULT_GEMINI_LABEL, GEMINI_VOICES
from textbook_core import TextbookError, TextbookProject
import textbook_audio as AUDIO


class FakeEngine:
    def __init__(self, failure_at=None, cancel_at=None, cancel_event=None):
        self.calls = []
        self.style = ""
        self.failure_at = failure_at
        self.cancel_at = cancel_at
        self.cancel_event = cancel_event

    def synth_to_wav(self, text, voice_id, language, speed, volume, out_path, cancel_event=None):
        self.calls.append({"text": text, "voice": voice_id, "language": language,
                           "speed": speed, "volume": volume, "style": self.style,
                           "path": str(out_path)})
        if len(self.calls) == self.failure_at:
            raise RuntimeError("One failed service request")
        marker = int(hashlib.sha256(text.encode()).hexdigest()[:3], 16) + 1
        with wave.open(str(out_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(struct.pack("<h", marker) * 240)
        if len(self.calls) == self.cancel_at:
            self.cancel_event.set()


def project_with_parts():
    text = "".join(f"Section {i}\n" + (f"Sentence {i} explains a different idea. " * 9) + "\n\n"
                   for i in range(1, 5))
    return TextbookProject.create(text, "Four useful ideas", max_block_chars=400)


class TextbookAudioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.output = self.base / "audiobook.wav"
        self.cache = self.base / "cache"
        self.project = project_with_parts()

    def render(self, project=None, engine=None, **kwargs):
        return AUDIO.render_textbook_audio(project or self.project, self.output,
            kwargs.pop("voice_label", DEFAULT_GEMINI_LABEL), cache_dir=self.cache,
            engine=engine or FakeEngine(), **kwargs)

    def explained_project(self):
        result = self.project.with_mode("explain")
        for index, block in enumerate(result.blocks):
            result = result.with_explanation(block.source_id, f"A teaching explanation for part {index + 1}.")
        return result

    def test_complete_render_preserves_source_sequence_and_frame_timings(self):
        engine = FakeEngine()
        progress = mock.Mock()
        result = self.render(engine=engine, progress=progress)
        self.assertEqual("".join(call["text"] for call in engine.calls), self.project.source_text)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["source_sha256"], self.project.source_sha256)
        self.assertFalse(result["sample"])
        self.assertEqual(result["reused_clips"], 0)
        prior_end = 0
        for item, block in zip(result["segments"], self.project.blocks):
            self.assertEqual(item["source_id"], block.source_id)
            self.assertAlmostEqual(item["start_seconds"], prior_end)
            self.assertAlmostEqual(item["end_seconds"] - item["start_seconds"], .01)
            prior_end = item["end_seconds"] + item["pause_after"]
        self.assertAlmostEqual(result["duration_seconds"], prior_end)
        with wave.open(str(self.output), "rb") as audio:
            self.assertEqual(audio.getnframes(), round(prior_end * 24000))
            self.assertEqual(audio.getframerate(), 24000)
        self.assertEqual(progress.call_args.args[:2], (len(self.project.blocks), len(self.project.blocks)))

    def test_read_and_explain_share_all_source_audio_with_separate_styles(self):
        source_engine = FakeEngine()
        self.render(engine=source_engine)
        explained = self.explained_project()
        additions_engine = FakeEngine()
        result = self.render(project=explained, engine=additions_engine)
        self.assertEqual(result["reused_clips"], len(self.project.blocks))
        self.assertEqual(len(additions_engine.calls), len(self.project.blocks))
        self.assertTrue(all(call["text"].startswith("Explanation. ") for call in additions_engine.calls))
        self.assertTrue(all(call["style"] == AUDIO.EXPLANATION_STYLE for call in additions_engine.calls))
        self.assertTrue(all(call["style"] == AUDIO.NARRATION_STYLE for call in source_engine.calls))
        self.assertEqual([item["kind"] for item in result["segments"]], ["source", "explanation"] * len(self.project.blocks))
        third_engine = FakeEngine()
        third = self.render(engine=third_engine)
        self.assertEqual(third["reused_clips"], len(self.project.blocks))
        self.assertEqual(third_engine.calls, [])

    def test_voice_speed_volume_provider_and_style_invalidate_only_matching_cache(self):
        self.render()
        voice_labels = list(GEMINI_VOICES)
        variants = ({"speed": 1.1}, {"volume": .8}, {"voice_label": voice_labels[1]},
                    {"provider": "edge", "voice_label": ANDREW_LABEL})
        for changed in variants:
            with self.subTest(changed=changed):
                engine = FakeEngine()
                result = self.render(engine=engine, **changed)
                self.assertEqual(result["reused_clips"], 0)
                self.assertGreater(len(engine.calls), 0)
        with mock.patch.object(AUDIO, "NARRATION_STYLE", "Different delivery."):
            engine = FakeEngine()
            self.assertEqual(self.render(engine=engine)["reused_clips"], 0)
            self.assertGreater(len(engine.calls), 0)

    def test_cancel_after_clip_retains_valid_cache_and_previous_export_then_resumes(self):
        self.output.write_bytes(b"previous complete audiobook")
        cancelled = threading.Event()
        engine = FakeEngine(cancel_at=2, cancel_event=cancelled)
        with self.assertRaises(SpeechCancelled):
            self.render(engine=engine, cancel_event=cancelled)
        self.assertEqual(self.output.read_bytes(), b"previous complete audiobook")
        self.assertEqual(len(list(self.cache.glob("*.wav"))), 2)
        self.assertTrue(all(AUDIO._valid_wav(path) for path in self.cache.glob("*.wav")))
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["audiobook.wav", "cache"])
        resumed_engine = FakeEngine()
        result = self.render(engine=resumed_engine)
        self.assertEqual(result["reused_clips"], 2)
        self.assertEqual(len(resumed_engine.calls), len(self.project.blocks) - 2)
        self.assertTrue(result["coverage"]["complete"])

    def test_failure_is_not_retried_and_keeps_completed_cache_and_export(self):
        self.output.write_bytes(b"previous complete audiobook")
        engine = FakeEngine(failure_at=2)
        with self.assertRaisesRegex(RuntimeError, "One failed"):
            self.render(engine=engine)
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(len(list(self.cache.glob("*.wav"))), 1)
        self.assertEqual(self.output.read_bytes(), b"previous complete audiobook")
        self.assertFalse(list(self.base.glob("textbook-*.wav")))

    def test_corrupt_cached_audio_rebuilds_only_that_clip(self):
        engine = FakeEngine()
        self.render(engine=engine)
        target = Path(engine.calls[1]["path"])
        target.write_bytes(target.read_bytes()[:-2])
        repeat_engine = FakeEngine()
        result = self.render(engine=repeat_engine)
        self.assertEqual(len(repeat_engine.calls), 1)
        self.assertEqual(repeat_engine.calls[0]["text"], engine.calls[1]["text"])
        self.assertTrue(AUDIO._valid_wav(target))
        self.assertEqual(result["reused_clips"], len(self.project.blocks) - 1)

    def test_invalid_new_audio_never_publishes_export(self):
        self.output.write_bytes(b"previous complete audiobook")
        engine = FakeEngine()
        engine.synth_to_wav = mock.Mock(side_effect=lambda *args, **kw: Path(args[5]).write_bytes(b"not audio"))
        with self.assertRaisesRegex(TextbookError, "invalid audio"):
            self.render(engine=engine)
        engine.synth_to_wav.assert_called_once()
        self.assertEqual(self.output.read_bytes(), b"previous complete audiobook")

    def test_sample_marks_partial_coverage_and_is_reused_by_full_render(self):
        engine = FakeEngine()
        sample = self.render(engine=engine, sample=True)
        self.assertTrue(sample["sample"])
        self.assertEqual(sample["coverage"]["covered_blocks"], 2)
        self.assertFalse(sample["coverage"]["complete"])
        self.assertEqual([s["source_id"] for s in sample["segments"]], [b.source_id for b in self.project.blocks[:2]])
        complete_engine = FakeEngine()
        full = self.render(engine=complete_engine)
        self.assertEqual(full["reused_clips"], 2)
        self.assertTrue(full["coverage"]["complete"])
        self.assertEqual(len(complete_engine.calls), len(self.project.blocks) - 2)

    def test_incomplete_explanation_mode_full_export_is_rejected_before_requests(self):
        project = self.project.with_mode("explain")
        project = project.with_explanation(project.blocks[0].source_id, "One teaching note.")
        engine = FakeEngine()
        with self.assertRaisesRegex(TextbookError, "Finish preparing"):
            self.render(project=project, engine=engine)
        self.assertEqual(engine.calls, [])
        self.assertFalse(self.cache.exists())
        sample = self.render(project=project, engine=engine, sample=True)
        self.assertEqual([s["kind"] for s in sample["segments"]], ["source", "explanation", "source"])

    def test_invalid_settings_fail_before_synthesis(self):
        engine = FakeEngine()
        variants = ({"speed": True}, {"speed": "1"}, {"speed": float("nan")},
                    {"speed": 3}, {"volume": 0}, {"volume": True},
                    {"provider": "unknown"}, {"voice_label": "unknown"})
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(TextbookError):
                self.render(engine=engine, **changed)
        self.assertEqual(engine.calls, [])

    def test_estimate_rejects_bad_speed_and_includes_explanation_words(self):
        for speed in (None, "bad", True, float("inf"), .1):
            with self.assertRaises(TextbookError):
                AUDIO.estimate_render(self.project, speed=speed)
        basic = AUDIO.estimate_render(self.project)
        explained = AUDIO.estimate_render(self.explained_project())
        self.assertGreater(explained["seconds"], basic["seconds"])
        self.assertGreater(explained["words"], basic["words"])
        self.assertGreater(explained["audio_cost_usd"], basic["audio_cost_usd"])

    def test_edge_splits_long_blocks_at_reading_boundaries_without_word_loss(self):
        source = ("The first substantial sentence introduces the useful idea. " * 17 + "\n\n"
                  + "The second paragraph develops it carefully for the reader. " * 17)
        project = TextbookProject.create(source)
        engine = FakeEngine()
        result = self.render(project=project, engine=engine, provider="edge", voice_label=ANDREW_LABEL)
        self.assertTrue(all(len(call["text"]) <= 700 for call in engine.calls))
        self.assertEqual("".join(call["text"] for call in engine.calls), source)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(len(result["segments"]), len(project.blocks))

    def test_gemini_splits_4000_character_blocks_and_honors_style(self):
        source = "A complete thought with a meaningful conclusion. " * 70
        project = TextbookProject.create(source, max_block_chars=4000)
        engine = FakeEngine()
        result = self.render(project=project, engine=engine)
        self.assertGreater(len(engine.calls), 1)
        self.assertTrue(all(len(call["text"]) <= 2400 for call in engine.calls))
        self.assertEqual("".join(call["text"] for call in engine.calls), source)
        self.assertTrue(result["coverage"]["complete"])
        self.assertTrue(all(call["style"] == AUDIO.NARRATION_STYLE for call in engine.calls))

    def test_overlong_token_is_rejected_before_earlier_paid_requests(self):
        source = "A normal source passage. " * 30 + "\n\n" + "x" * 6001
        project = TextbookProject.create(source, max_block_chars=800)
        engine = FakeEngine()
        with self.assertRaisesRegex(TextbookError, "6,000"):
            self.render(project=project, engine=engine)
        self.assertEqual(engine.calls, [])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.cache.exists())

    def test_publish_failure_cleans_scratch_and_keeps_valid_cache(self):
        self.output.write_bytes(b"previous complete audiobook")
        with mock.patch.object(AUDIO.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.render()
        self.assertEqual(self.output.read_bytes(), b"previous complete audiobook")
        self.assertFalse(list(self.base.glob("textbook-*.wav")))
        self.assertEqual(len(list(self.cache.glob("*.wav"))), len(self.project.blocks))

    def test_pre_cancel_never_creates_output_or_cache(self):
        cancelled = threading.Event()
        cancelled.set()
        engine = FakeEngine()
        with self.assertRaises(SpeechCancelled):
            self.render(engine=engine, cancel_event=cancelled)
        self.assertEqual(engine.calls, [])
        self.assertEqual(os.listdir(self.base), [])


if __name__ == "__main__":
    unittest.main()
