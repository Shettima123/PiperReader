import importlib.machinery
import importlib.util
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "piper_reader.pyw")
LOADER = importlib.machinery.SourceFileLoader("piper_reader_repeat_tests", SOURCE)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
APP = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(APP)


class _ImmediateRoot:
    def after(self, _delay, callback, *args):
        callback(*args)
        return "after-id"


class _FakeEngine:
    def __init__(self, name="Fake", chunk_len=500, initial_chunk_len=None):
        self.name = name
        self.chunk_len = chunk_len
        self.initial_chunk_len = initial_chunk_len
        self.synthesized = []

    @staticmethod
    def ready():
        return True

    def synth_to_wav(self, text, _voice, _lang, _speed, _volume, path):
        self.synthesized.append((text, path))
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)


class _FakePlayer:
    def __init__(self, cancel_after=None):
        self.cancel_after = cancel_after
        self.played = []

    def play_blocking(self, path, cancel_event, _pause_event):
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        self.played.append((text, path))
        if self.cancel_after and len(self.played) >= self.cancel_after:
            cancel_event.set()


class _FakeBridge:
    def __init__(self):
        self.events = []

    def publish(self, session, event_type, **payload):
        self.events.append((session, event_type, payload))


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _run_worker(kind, payload, mode, count, engine=None, player=None,
                browser=False):
    engine = engine or _FakeEngine()
    player = player or _FakePlayer()
    app = APP.ReaderApp.__new__(APP.ReaderApp)
    app.speak_q = queue.Queue()
    app._shutdown_event = threading.Event()
    app._playback_lock = threading.RLock()
    cancel = threading.Event()
    pause = threading.Event()
    app._speech_requests = {cancel}
    app._active_cancel = None
    app._active_pause = None
    app._set_playback_state = lambda _state: None
    app.set_status = lambda _status: None
    app._last_speaking_status = ""
    app.engines = {"fake": engine}
    app.player = player
    app.root = _ImmediateRoot()
    app.focused = []
    app._safe_focus = app.focused.append
    app.completed = []
    app._complete_playback_request = (
        lambda event, completed=False: app.completed.append((event, completed)))
    app._finish_browser_session = lambda *_args: None
    app._browser_bridge = _FakeBridge() if browser else None
    app.tmpdir = tempfile.mkdtemp(prefix="piper_repeat_test_")
    request = (
        kind, payload, cancel, pause, "fake", "voice", "en-us", 1.0, 1.0,
        mode, count,
    )
    app.speak_q.put(request)
    app.speak_q.put(None)
    try:
        APP.ReaderApp._worker(app)
        remaining = os.listdir(app.tmpdir)
    finally:
        shutil.rmtree(app.tmpdir, ignore_errors=True)
    return app, engine, player, cancel, remaining


class RepeatPlanningTests(unittest.TestCase):
    def test_unicode_sentence_and_paragraph_units(self):
        text = "First. Second!\n\nArabic one\u061f Arabic two\u06d4"
        self.assertEqual(
            APP.split_repeat_sentences(text),
            ["First.", "Second!", "Arabic one\u061f", "Arabic two\u06d4"],
        )
        self.assertEqual(
            APP.split_repeat_paragraphs(text),
            ["First. Second!", "Arabic one\u061f Arabic two\u06d4"],
        )

    def test_long_sentence_parts_stay_in_one_unit(self):
        text = "one two three four five six seven eight nine ten eleven twelve. Next."
        units = APP.build_repeat_units("text", text, "sentence", 40)
        self.assertEqual(len(units), 2)
        self.assertGreater(len(units[0]), 1)
        self.assertEqual(" ".join(part for part, _meta in units[0]),
                         text.split(". Next.")[0] + ".")

    def test_metadata_groups_consecutive_document_parts(self):
        payload = [
            ("A", {"sentence_id": 4, "paragraph_id": 1}),
            ("B", {"sentence_id": 4, "paragraph_id": 1}),
            ("C", {"sentence_id": 5, "paragraph_id": 1}),
        ]
        sentence_units = APP.build_repeat_units(
            "doc", payload, "sentence", 500)
        paragraph_units = APP.build_repeat_units(
            "doc", payload, "paragraph", 500)
        self.assertEqual([[part for part, _ in unit]
                          for unit in sentence_units], [["A", "B"], ["C"]])
        self.assertEqual([[part for part, _ in unit]
                          for unit in paragraph_units], [["A", "B", "C"]])

    def test_corrupt_settings_are_safe(self):
        self.assertEqual(APP.normalize_repeat_mode("unknown"), "off")
        self.assertEqual(APP.normalize_repeat_mode("Sentence"), "sentence")
        self.assertEqual(APP.normalize_repeat_count("bad"), 7)
        self.assertEqual(APP.normalize_repeat_count(999), 20)
        self.assertEqual(APP.normalize_repeat_count(-1), 2)


class RepeatWorkerTests(unittest.TestCase):
    def test_sentence_repeats_synthesize_once(self):
        app, engine, player, _cancel, remaining = _run_worker(
            "text", "First sentence. Second sentence.", "sentence", 3)
        self.assertEqual([text for text, _path in engine.synthesized],
                         ["First sentence.", "Second sentence."])
        self.assertEqual([text for text, _path in player.played], [
            "First sentence.", "First sentence.", "First sentence.",
            "Second sentence.", "Second sentence.", "Second sentence.",
        ])
        self.assertEqual(player.played[0][1], player.played[1][1])
        self.assertEqual(remaining, [])
        self.assertTrue(app.completed[-1][1])

    def test_long_sentence_repeats_all_parts_before_advancing(self):
        engine = _FakeEngine(name="Kokoro", chunk_len=40)
        app, engine, player, _cancel, remaining = _run_worker(
            "text",
            "one two three four five six seven eight nine ten eleven twelve. Next.",
            "sentence", 2, engine=engine)
        first_unit = [text for text, _meta in APP.build_repeat_units(
            "text",
            "one two three four five six seven eight nine ten eleven twelve. Next.",
            "sentence", 40)[0]]
        self.assertEqual([text for text, _path in player.played],
                         first_unit + first_unit + ["Next.", "Next."])
        self.assertEqual(len(engine.synthesized), len(first_unit) + 1)
        self.assertEqual(remaining, [])
        self.assertTrue(app.completed[-1][1])

    def test_paragraph_and_passage_order(self):
        paragraph = _run_worker(
            "text", "Paragraph one.\n\nParagraph two.", "paragraph", 2)[2]
        passage = _run_worker(
            "text", "Paragraph one.\n\nParagraph two.", "passage", 2,
            engine=_FakeEngine(chunk_len=40))[2]
        self.assertEqual([text for text, _ in paragraph.played], [
            "Paragraph one.", "Paragraph one.",
            "Paragraph two.", "Paragraph two.",
        ])
        passage_once = [text for text, _meta in APP.build_repeat_units(
            "text", "Paragraph one.\n\nParagraph two.", "passage", 40)[0]]
        self.assertEqual([text for text, _ in passage.played],
                         passage_once + passage_once)

    def test_browser_highlights_repeat_and_complete_once(self):
        payload = [
            ("A.", {"browser_session": "s1", "chunk_id": "c0",
                    "index": 0, "total": 2, "sentence_id": 0,
                    "paragraph_id": 0}),
            ("B.", {"browser_session": "s1", "chunk_id": "c1",
                    "index": 1, "total": 2, "sentence_id": 1,
                    "paragraph_id": 0}),
        ]
        app, engine, player, _cancel, remaining = _run_worker(
            "browser", payload, "paragraph", 2, browser=True)
        highlights = [event for event in app._browser_bridge.events
                      if event[1] == "highlight"]
        completes = [event for event in app._browser_bridge.events
                     if event[1] == "complete"]
        self.assertEqual([event[2]["chunk_id"] for event in highlights],
                         ["c0", "c1", "c0", "c1"])
        self.assertEqual([event[2]["repeat_index"] for event in highlights],
                         [1, 1, 2, 2])
        self.assertEqual(len(completes), 1)
        self.assertEqual(len(engine.synthesized), 2)
        self.assertEqual(len(player.played), 4)
        self.assertEqual(remaining, [])

    def test_cancel_stops_remaining_repetitions_and_cleans_cache(self):
        player = _FakePlayer(cancel_after=2)
        app, engine, player, cancel, remaining = _run_worker(
            "text", "Remember this.", "sentence", 7, player=player)
        self.assertTrue(cancel.is_set())
        self.assertEqual(len(engine.synthesized), 1)
        self.assertEqual(len(player.played), 2)
        self.assertEqual(remaining, [])
        self.assertFalse(app.completed[-1][1])

    def test_oversized_passage_resynthesizes_with_bounded_cache(self):
        text = ("one two three four five six seven eight nine ten. "
                "eleven twelve thirteen fourteen fifteen sixteen.")
        old_limit = APP.REPEAT_WAV_CACHE_MAX_BYTES
        APP.REPEAT_WAV_CACHE_MAX_BYTES = 1
        try:
            engine = _FakeEngine(chunk_len=40)
            _app, engine, player, _cancel, remaining = _run_worker(
                "text", text, "passage", 3, engine=engine)
        finally:
            APP.REPEAT_WAV_CACHE_MAX_BYTES = old_limit
        one_pass = [part for part, _meta in APP.build_repeat_units(
            "text", text, "passage", 40)[0]]
        self.assertEqual([value for value, _path in player.played],
                         one_pass * 3)
        self.assertEqual(len(engine.synthesized), len(one_pass) * 3)
        self.assertEqual(remaining, [])

    def test_off_mode_matches_single_play(self):
        _app, engine, player, _cancel, remaining = _run_worker(
            "text", "First. Second.", "off", 20)
        self.assertEqual(len(engine.synthesized), 1)
        self.assertEqual(len(player.played), 1)
        self.assertEqual(remaining, [])

    def test_pause_between_repetitions_waits_for_resume(self):
        class PausePlayer(_FakePlayer):
            def __init__(self):
                super().__init__()
                self.pause_started = None
                self.second_started = None

            def play_blocking(self, path, cancel_event, pause_event):
                super().play_blocking(path, cancel_event, pause_event)
                if len(self.played) == 1:
                    self.pause_started = time.monotonic()
                    pause_event.set()

                    def resume():
                        time.sleep(0.1)
                        pause_event.clear()

                    threading.Thread(target=resume, daemon=True).start()
                elif len(self.played) == 2:
                    self.second_started = time.monotonic()

        player = PausePlayer()
        _run_worker("text", "Pause here.", "sentence", 2, player=player)
        self.assertIsNotNone(player.second_started)
        self.assertGreaterEqual(
            player.second_started - player.pause_started, 0.08)


class RepeatRequestSnapshotTests(unittest.TestCase):
    def test_fifo_requests_capture_their_own_repeat_settings(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app._shutdown_event = threading.Event()
        app.speak_q = queue.Queue()
        app._playback_lock = threading.RLock()
        app._speech_requests = set()
        app._active_cancel = None
        app._active_pause = None
        app._pause_requested = threading.Event()
        app.voice_var = _Value(APP.DEFAULT_VOICE)
        app.speed_var = _Value(1.0)
        app.vol_var = _Value(1.0)
        app.repeat_mode_var = _Value("Sentence")
        app.repeat_count_var = _Value("7")
        app._set_playback_state = lambda _state: None

        APP.ReaderApp._queue_speech(
            app, "clipboard", "First", replace=False)
        app.repeat_mode_var.set("Paragraph")
        app.repeat_count_var.set("3")
        APP.ReaderApp._queue_speech(
            app, "clipboard", "Second", replace=False)

        first = app.speak_q.get_nowait()
        second = app.speak_q.get_nowait()
        self.assertEqual(first[1], "First")
        self.assertEqual(first[-2:], ("sentence", 7))
        self.assertEqual(second[1], "Second")
        self.assertEqual(second[-2:], ("paragraph", 3))

    def test_voice_preview_forces_one_play(self):
        app = APP.ReaderApp.__new__(APP.ReaderApp)
        app.voice_var = _Value(APP.DEFAULT_VOICE)
        captured = []
        app.speak = lambda text, use_repeat=True: captured.append(
            (text, use_repeat))
        APP.ReaderApp.preview_voice(app)
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0][1])


class BrowserBridgeGroupingTests(unittest.TestCase):
    def test_bridge_preserves_valid_group_ids(self):
        from browser_bridge import BrowserBridge

        temporary = tempfile.mkdtemp(prefix="piper_bridge_repeat_")
        try:
            config_path = os.path.join(temporary, "bridge.json")
            with open(config_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "port": 57638,
                    "token": "x" * 40,
                    "extension_origin":
                        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                }, stream)
            commands = queue.Queue(maxsize=4)
            bridge = BrowserBridge(commands, config_path)
            bridge._accepting = True
            bridge.enqueue_read({
                "session_id": "repeat-test",
                "chunks": [{"id": "c0", "text": "Text.",
                            "sentence_id": 8, "paragraph_id": 3}],
                "start_index": 0,
            })
            chunk = commands.get_nowait()["chunks"][0]
            self.assertEqual(chunk["sentence_id"], 8)
            self.assertEqual(chunk["paragraph_id"], 3)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
