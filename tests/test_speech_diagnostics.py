"""Real-file tests for bounded diagnostics without copied text or secrets."""
import contextlib
import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

try:
    from speech_diagnostics import SpeechDiagnostics
except ImportError:
    SpeechDiagnostics = None


class SpeechDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "new-directory" / "speech.jsonl"

    def make_log(self, path=None):
        self.assertIsNotNone(SpeechDiagnostics, "The bounded diagnostics utility is missing")
        log = SpeechDiagnostics(path or self.path)
        self.addCleanup(log.close)
        return log

    def records(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_construct_and_close_do_not_create_a_file(self):
        log = self.make_log()
        self.assertFalse(self.path.parent.exists())
        log.close()
        log.close()
        log.record(outcome="completed")
        self.assertFalse(self.path.exists())

    def test_record_contains_only_safe_structured_fields(self):
        log = self.make_log()
        log.record(outcome="failed", reason="timeout", http_status=503,
                   elapsed_seconds=12.23456, queue_wait_seconds=3.125,
                   voice_engine="edge", chunks_played=2)
        record, = self.records()
        self.assertEqual(set(record), {"timestamp_utc", "outcome", "reason", "http_status",
                                      "elapsed_seconds", "queue_wait_seconds", "voice_engine", "chunks_played"})
        stamp = datetime.datetime.fromisoformat(record.pop("timestamp_utc").replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset(), datetime.timedelta(0))
        self.assertEqual(record, {"outcome": "failed", "reason": "timeout", "http_status": 503,
                                  "elapsed_seconds": 12.235, "queue_wait_seconds": 3.125,
                                  "voice_engine": "edge", "chunks_played": 2})

    def test_unrecognized_text_is_never_persisted(self):
        log = self.make_log()
        private = "private copied sentence https://example.invalid/?key=secret"
        log.record(outcome=private, reason=private, voice_engine=private)
        self.assertFalse(self.path.exists())
        log.record(outcome="failed", reason=private, http_status=private,
                   elapsed_seconds=private, queue_wait_seconds=private,
                   voice_engine=private, chunks_played=private)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(private, raw)
        self.assertNotIn("secret", raw)
        record, = self.records()
        self.assertEqual(record["reason"], "unknown")
        self.assertEqual(record["voice_engine"], "unknown")
        self.assertIsNone(record["http_status"])
        self.assertEqual(record["elapsed_seconds"], 0)
        self.assertEqual(record["queue_wait_seconds"], 0)
        self.assertEqual(record["chunks_played"], 0)

    def test_malformed_and_nonfinite_fields_are_bounded(self):
        log = self.make_log()
        for value in (None, True, [], {}, object(), float("nan"), float("inf"), -10):
            log.record(outcome="cancelled", reason=value, http_status=value,
                       elapsed_seconds=value, queue_wait_seconds=value,
                       voice_engine=value, chunks_played=value)
        log.record(outcome="completed", elapsed_seconds=10**1000,
                   queue_wait_seconds=86401, chunks_played=10**1000, http_status=999)
        for record in self.records():
            self.assertGreaterEqual(record["elapsed_seconds"], 0)
            self.assertLessEqual(record["elapsed_seconds"], 86400)
            self.assertGreaterEqual(record["queue_wait_seconds"], 0)
            self.assertLessEqual(record["queue_wait_seconds"], 86400)
            self.assertGreaterEqual(record["chunks_played"], 0)
            self.assertLessEqual(record["chunks_played"], 1000000)
        self.assertNotIn("NaN", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("Infinity", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.records()[-1]["queue_wait_seconds"], 86400)
        self.assertEqual(self.records()[-1]["chunks_played"], 1000000)
        self.assertIsNone(self.records()[-1]["http_status"])

    def test_rotation_preserves_latest_records_and_bounds_two_files(self):
        log = self.make_log()
        for index in range(1200):
            log.record(outcome="completed", chunks_played=index)
        log.close()
        files = list(self.path.parent.iterdir())
        self.assertEqual({file.name for file in files}, {"speech.jsonl", "speech.jsonl.1"})
        for file in files:
            self.assertLessEqual(file.stat().st_size, 65536)
            for line in file.read_text(encoding="utf-8").splitlines():
                json.loads(line)
        self.assertEqual(self.records()[-1]["chunks_played"], 1199)
        self.assertLess(sum(len(file.read_text(encoding="utf-8").splitlines()) for file in files), 1200)

    def test_file_errors_are_silent_and_do_not_break_reading(self):
        blocker = Path(self.directory.name) / "not-a-directory"
        blocker.write_text("keep", encoding="utf-8")
        log = self.make_log(blocker / "speech.jsonl")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            log.record(outcome="failed", reason="network")
            log.close()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(blocker.read_text(encoding="utf-8"), "keep")

    def test_concurrent_records_are_complete_and_instances_do_not_duplicate(self):
        log = self.make_log()
        threads = [threading.Thread(target=lambda: [log.record(outcome="completed") for _ in range(20)])
                   for _ in range(4)]
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join()
        self.assertEqual(len(self.records()), 80)
        log.close()
        replacement = self.make_log()
        replacement.record(outcome="cancelled")
        self.assertEqual(len(self.records()), 81)


if __name__ == "__main__":
    unittest.main()
