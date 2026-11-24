"""Offline Gemini contract, cancellation, atomic output and credential tests."""

import asyncio
import base64
import datetime
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import traceback
import unittest
import wave
from unittest import mock

import gemini_engine as GEMINI


def wav_bytes(samples=(12000, -12000, 3000, 0), rate=24000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return output.getvalue()


def audio_document(encoded=None):
    return {"status": "completed", "steps": [
        {"type": "user_input", "content": [{"type": "text", "text": "Not audio."}]},
        {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav",
            "data": base64.b64encode(encoded if encoded is not None else wav_bytes()).decode("ascii")}]}]}


class Response:
    def __init__(self, body, status=200, content_length=None):
        self.body = body
        self.status = status
        self.content_length = len(body) if content_length is None else content_length
        self.content = self
        self.closed = False
        self.bytes_yielded = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            block = self.body[offset:offset + size]
            self.bytes_yielded += len(block)
            yield block


class Session:
    def __init__(self, response):
        self.response = response
        self.post = mock.Mock(return_value=response)
        self.get = mock.Mock(return_value=response)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class GeminiEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "speech.wav"
        self.engine = GEMINI.GeminiEngine(api_key="synthetic-key-for-offline-tests")

    def synthesize(self, **kwargs):
        return self.engine.synth_to_wav("First sentence.\nSecond sentence.", "Charon", "en-us",
                                        kwargs.pop("speed", 1), kwargs.pop("volume", 1), self.output, **kwargs)

    def test_payload_keeps_every_transcript_character_and_instructions_separate(self):
        text = 'Read this: "Explain every detail."\nThis remains source text.'
        payload = self.engine._payload(text, "Charon", 1.2)
        self.assertEqual(payload["model"], "gemini-3.8-flash-tts")
        self.assertEqual(payload["input"][0]["type"], "user_input")
        content = payload["input"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["text"], text)
        self.assertEqual(content[0]["annotations"][0]["type"], "speech_metadata")
        self.assertIn("1.2 times", content[0]["annotations"][0]["style"])
        self.assertEqual(payload["generation_config"], {"speech_config": [{"voice": "Charon"}]})
        self.assertEqual(payload["response_format"], {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000})
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], False)

    def test_http_contract_keeps_key_in_header_and_closes_connections(self):
        response = Response(json.dumps(audio_document()).encode())
        session = Session(response)
        with mock.patch("aiohttp.ClientSession", return_value=session) as factory:
            self.synthesize()
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        session.post.assert_called_once()
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://generativelanguage.googleapis.com/v1beta/interactions",))
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": "synthetic-key-for-offline-tests"})
        self.assertNotIn("synthetic-key", json.dumps(kwargs["json"]))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)
        with wave.open(str(self.output), "rb") as stream:
            self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 24000))
            self.assertEqual(stream.getnframes(), 4)

    def test_selected_voice_and_local_volume(self):
        self.engine._receive = mock.AsyncMock(return_value=wav_bytes())
        self.engine.synth_to_wav("Plain transcript", "Sulafat", "en-us", 1, .5, self.output)
        payload = self.engine._receive.call_args.args[0]
        self.assertEqual(payload["generation_config"]["speech_config"][0]["voice"], "Sulafat")
        with wave.open(str(self.output), "rb") as stream:
            self.assertEqual(struct.unpack("<hhhh", stream.readframes(4)), (6000, -6000, 1500, 0))
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_each_catalog_voice_is_accepted_without_network(self):
        for label, (voice, _lang) in GEMINI.GEMINI_VOICES.items():
            with self.subTest(label=label):
                self.assertEqual(self.engine._payload("Text", voice, 1)["generation_config"]["speech_config"], [{"voice": voice}])

    def test_bad_inputs_and_mutated_model_fail_before_any_request(self):
        self.engine._receive = mock.AsyncMock()
        for text, voice in (("", "Charon"), ("Text", "fake"), ("a" * 6001, "Charon")):
            with self.assertRaises(ValueError):
                self.engine.synth_to_wav(text, voice, None, 1, 1, self.output)
        with self.assertRaises(ValueError):
            GEMINI.GeminiEngine(model="gemini-2.5-pro")
        self.engine.model = "gemini-2.5-pro"
        with self.assertRaises(ValueError):
            self.synthesize()
        self.engine._receive.assert_not_awaited()

    def test_missing_key_fails_without_network(self):
        self.engine = GEMINI.GeminiEngine()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                self.engine, "_receive", new=mock.AsyncMock()) as receive:
            self.assertFalse(self.engine.key_available())
            with self.assertRaisesRegex(GEMINI.GeminiError, "API key"):
                self.synthesize()
            receive.assert_not_awaited()

    def test_key_sources_are_explicit_then_provider_then_allowed_environment(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-env", "GOOGLE_API_KEY": "google-env"}, clear=True):
            self.assertEqual(GEMINI.GeminiEngine(api_key="explicit")._get_key(), "explicit")
            self.assertEqual(GEMINI.GeminiEngine(key_provider=lambda: "provider")._get_key(), "provider")
            self.assertEqual(GEMINI.GeminiEngine()._get_key(), "gemini-env")
            del os.environ["GEMINI_API_KEY"]
            self.assertEqual(GEMINI.GeminiEngine()._get_key(), "google-env")
            with self.assertRaises(GEMINI.GeminiError):
                GEMINI.GeminiEngine(api_key="")._get_key()

    def test_provider_and_network_errors_do_not_expose_keys_or_causes_in_traceback(self):
        secret = "a-synthetic-secret-that-must-not-appear"
        self.engine._receive = mock.AsyncMock(side_effect=RuntimeError(secret))
        try:
            self.synthesize()
        except GEMINI.GeminiError:
            self.assertNotIn(secret, traceback.format_exc())
        else:
            self.fail("Expected a connection error")
        self.engine = GEMINI.GeminiEngine(key_provider=mock.Mock(side_effect=RuntimeError(secret)))
        try:
            self.synthesize()
        except GEMINI.GeminiError:
            self.assertNotIn(secret, traceback.format_exc())
        else:
            self.fail("Expected a key error")

    def test_status_errors_never_include_server_body_and_never_retry(self):
        for status in (301, 400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                session = Session(Response(b'private-key-and-transcript', status=status))
                with mock.patch("aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(GEMINI.GeminiError) as caught:
                        self.synthesize()
                self.assertNotIn("private-key", str(caught.exception))
                self.assertEqual(caught.exception.http_status, status)
                session.post.assert_called_once()
                self.assertFalse(self.output.exists())

    def test_interactions_errors_show_only_allowlisted_code_and_action(self):
        cases = ((400, "invalid_request", "compatibility"),
                 (400, "parameter_unknown", "unrecognized"),
                 (400, "failed_precondition", "prerequisite"),
                 (401, "authentication", "API key"),
                 (402, "payment_required", "credits"),
                 (403, "permission_denied", "permission"),
                 (404, "model_not_found", "model availability"),
                 (429, "quota_exceeded", "quota"))
        for status, code, action in cases:
            with self.subTest(code=code):
                body = {"error": {"code": code, "message": "synthetic-secret and source transcript",
                                  "request_id": "private-request-id"}}
                session = Session(Response(json.dumps(body).encode(), status=status))
                with mock.patch("aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(GEMINI.GeminiError) as caught:
                        self.synthesize()
                error = caught.exception
                self.assertEqual(error.http_status, status)
                self.assertEqual(error.error_code, code)
                self.assertIn(code, str(error))
                self.assertIn(action, str(error))
                self.assertIn(f"HTTP {status}", str(error))
                self.assertNotIn("synthetic-secret", str(error))
                self.assertNotIn("transcript", str(error))
                self.assertNotIn("private-request-id", repr(error.__dict__))
                session.post.assert_called_once()
                session.get.assert_not_called()

    def test_legacy_error_status_and_errorinfo_reason_distinguish_bad_key(self):
        body = {"error": {"code": 400, "message": "Server text with private material",
                          "status": "INVALID_ARGUMENT", "details": [
                              {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                               "reason": "API_KEY_INVALID", "domain": "googleapis.com",
                               "metadata": {"service": "generativelanguage.googleapis.com",
                                            "secret": "must-not-leak"}}]}}
        error = GEMINI.GeminiEngine._http_error(400, body)
        self.assertEqual(error.error_code, "API_KEY_INVALID")
        self.assertIn("rejected the API key", str(error))
        self.assertNotIn("private", str(error))
        self.assertNotIn("must-not-leak", repr(error.__dict__))
        body["error"].pop("details")
        error = GEMINI.GeminiEngine._http_error(400, body)
        self.assertEqual(error.error_code, "INVALID_ARGUMENT")
        self.assertIn("compatibility", str(error))

    def test_fixed_key_messages_are_classified_but_arbitrary_variants_are_not_echoed(self):
        for message, expected in GEMINI._FIXED_KEY_ERRORS.items():
            error = self.engine._http_error(400, {"error": {"message": message}})
            self.assertEqual(error.error_code, expected)
        error = self.engine._http_error(400, {"error": {
            "code": "invalid-secret-code", "status": "PRIVATE_STATUS",
            "message": "API key not valid. Please pass a valid API key. private-key",
            "param": "input.secret", "details": [{"reason": "PRIVATE_REASON"}]}})
        self.assertIsNone(error.error_code)
        self.assertIsNone(error.parameter)
        for private in ("PRIVATE", "private", "invalid-secret-code", "input.secret"):
            self.assertNotIn(private, str(error))
            self.assertNotIn(private, repr(error.__dict__))

    def test_only_known_parameter_names_are_displayed(self):
        body = {"error": {"code": "parameter_unknown", "param": "response_format.sample_rate",
                          "message": "private source"}}
        error = self.engine._http_error(400, body)
        self.assertEqual(error.parameter, "response_format.sample_rate")
        self.assertIn("Parameter: response_format.sample_rate", str(error))
        body["error"] = {"code": 400, "status": "INVALID_ARGUMENT", "details": [{
            "@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [
                {"field": "private-field", "description": "private source"},
                {"field": "generation_config.speech_config", "description": "private source"}]}]}
        error = self.engine._http_error(400, body)
        self.assertEqual(error.parameter, "generation_config.speech_config")
        self.assertNotIn("private", str(error))

    def test_error_body_read_is_bounded_even_when_server_length_is_wrong(self):
        for claimed in (1, self.engine.max_error_bytes + 1):
            response = Response(b"x" * (self.engine.max_error_bytes * 4), status=400, content_length=claimed)
            session = Session(response)
            with mock.patch("aiohttp.ClientSession", return_value=session):
                with self.assertRaises(GEMINI.GeminiError) as caught:
                    self.synthesize()
            self.assertEqual(caught.exception.http_status, 400)
            self.assertIsNone(caught.exception.error_code)
            self.assertLessEqual(response.bytes_yielded, self.engine.max_error_bytes + 4096)
            if claimed > self.engine.max_error_bytes:
                self.assertEqual(response.bytes_yielded, 0)
            self.assertTrue(response.closed)
            session.post.assert_called_once()

    def test_malformed_error_shapes_never_become_connect_errors_or_leak(self):
        bodies = (b"not-json-private", b"[]", b'{"error":[]}',
                  b'{"error":{"code":[],"status":{},"details":[null,1,{}, {"reason":[]}],"param":[]}}',
                  b"[" * 2000 + b"]" * 2000)
        for body in bodies:
            session = Session(Response(body, status=400))
            with mock.patch("aiohttp.ClientSession", return_value=session):
                with self.assertRaises(GEMINI.GeminiError) as caught:
                    self.synthesize()
            self.assertEqual(caught.exception.http_status, 400)
            self.assertIn("HTTP 400", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))

    def test_access_check_gets_only_metadata_and_does_not_claim_generation_access(self):
        document = {"name": "models/" + GEMINI.GEMINI_MODEL, "inputTokenLimit": 8192,
                    "outputTokenLimit": 16384, "supportedGenerationMethods": ["generateContent"],
                    "description": "private metadata that must not escape"}
        response = Response(json.dumps(document).encode())
        session = Session(response)
        with mock.patch("aiohttp.ClientSession", return_value=session) as factory:
            result = self.engine.check_access()
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        session.post.assert_not_called()
        session.get.assert_called_once_with(GEMINI.GEMINI_MODEL_ENDPOINT,
            headers={"x-goog-api-key": "synthetic-key-for-offline-tests"}, allow_redirects=False)
        self.assertTrue(result["key_accepted"])
        self.assertTrue(result["model_available"])
        self.assertEqual(result["model"], GEMINI.GEMINI_MODEL)
        for field in ("generation_verified", "quota_verified", "billing_verified"):
            self.assertIs(result[field], False)
        self.assertIn("remain unverified", result["message"])
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)
        self.assertFalse(self.output.exists())

    def test_access_errors_do_not_generate_or_retry_or_overclaim_model_absence(self):
        cases = ((400, {"code": 400, "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}),
                 (401, {"code": "authentication"}), (403, {"code": "permission_denied"}),
                 (404, {"code": "model_not_found"}))
        for status, error in cases:
            session = Session(Response(json.dumps({"error": error}).encode(), status=status))
            with mock.patch("aiohttp.ClientSession", return_value=session):
                with self.assertRaises(GEMINI.GeminiError) as caught:
                    self.engine.check_access()
            self.assertEqual(caught.exception.http_status, status)
            if status == 404:
                self.assertIn("cannot establish speech access", str(caught.exception))
            session.get.assert_called_once()
            session.post.assert_not_called()

    def test_access_metadata_is_bounded_and_must_identify_exact_target(self):
        cases = ((b"{}", 2), (b"[]", 2), (b"not-json", 8),
                 (b'{"name":"models/other-private-model"}', 37),
                 (b"{}", self.engine.max_metadata_bytes + 1),
                 (b"x" * (self.engine.max_metadata_bytes + 1), 1))
        for body, claimed in cases:
            response = Response(body, content_length=claimed)
            session = Session(response)
            with mock.patch("aiohttp.ClientSession", return_value=session):
                with self.assertRaises(GEMINI.GeminiError) as caught:
                    self.engine.check_access()
            self.assertIn("unverified", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))
            self.assertLessEqual(response.bytes_yielded, self.engine.max_metadata_bytes + 4096)
            session.get.assert_called_once()
            session.post.assert_not_called()

    def test_access_cancel_before_start_never_loads_key_or_connects(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(self.engine, "_get_key") as key, mock.patch("aiohttp.ClientSession") as session:
            with self.assertRaises(GEMINI.SpeechCancelled):
                self.engine.check_access(cancel)
        key.assert_not_called()
        session.assert_not_called()

    def test_access_inflight_cancel_reaps_transport(self):
        cancel = threading.Event()
        closed = threading.Event()

        async def receive(_key):
            cancel.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with mock.patch.object(self.engine, "_receive_access", side_effect=receive) as receive_mock:
            with self.assertRaises(GEMINI.SpeechCancelled):
                self.engine.check_access(cancel)
        self.assertTrue(closed.is_set())
        receive_mock.assert_awaited_once()

    def test_access_missing_key_and_network_errors_are_safe_without_retries(self):
        with mock.patch.object(self.engine, "_get_key", side_effect=GEMINI.GeminiError("Enter a Gemini API key")), mock.patch("aiohttp.ClientSession") as session:
            with self.assertRaisesRegex(GEMINI.GeminiError, "API key"):
                self.engine.check_access()
        session.assert_not_called()
        with mock.patch.object(self.engine, "_receive_access", side_effect=RuntimeError("synthetic-private-key")) as receive:
            try:
                self.engine.check_access()
            except GEMINI.GeminiError:
                self.assertNotIn("synthetic-private-key", traceback.format_exc())
            else:
                self.fail("Expected connection error")
        receive.assert_awaited_once()

    def test_access_timeout_reaps_without_generation_warning_or_retry(self):
        closed = threading.Event()

        async def receive(_key):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        self.engine.request_timeout = .01
        with mock.patch.object(self.engine, "_receive_access", side_effect=receive) as receive_mock:
            with self.assertRaisesRegex(GEMINI.GeminiError, "access check timed out.*No audio"):
                self.engine.check_access()
        self.assertTrue(closed.is_set())
        receive_mock.assert_awaited_once()

    def test_response_json_and_stream_size_limits_fail_before_output(self):
        for body, claimed in ((b'not JSON', 8), (b'{}', self.engine.max_response_bytes + 1)):
            session = Session(Response(body, content_length=claimed))
            with mock.patch("aiohttp.ClientSession", return_value=session), self.assertRaises(GEMINI.GeminiError):
                self.synthesize()
        self.engine.max_response_bytes = 10
        response = Response(b'12345678901234567890', content_length=1)
        with mock.patch("aiohttp.ClientSession", return_value=Session(response)), self.assertRaises(GEMINI.GeminiError):
            self.synthesize()
        self.assertFalse(self.output.exists())

    def test_rejects_noncompleted_missing_ambiguous_and_bad_base64_responses(self):
        bad_status = audio_document()
        bad_status["status"] = "in_progress"
        duplicate = audio_document()
        duplicate["steps"].append(duplicate["steps"][-1])
        bad_base64 = audio_document()
        bad_base64["steps"][-1]["content"][0]["data"] = "%%%%"
        raw = audio_document()
        raw["steps"][-1]["content"][0]["mime_type"] = "audio/l16"
        malformed = audio_document()
        malformed["steps"][-1]["content"][0]["mime_type"] = {"bad": "type"}
        error = audio_document()
        error["error"] = {"message": "private details"}
        for document in (None, {}, {"steps": []}, bad_status, duplicate, bad_base64, raw, malformed, error):
            with self.subTest(document=document):
                with self.assertRaises(GEMINI.GeminiError):
                    self.engine._decode_audio(document)

    def test_bad_or_truncated_wav_preserves_previous_file(self):
        self.output.write_bytes(b"previous recording")
        for encoded in (b"not WAV", wav_bytes(rate=16000), wav_bytes()[:-2], wav_bytes(samples=())):
            self.engine._receive = mock.AsyncMock(return_value=encoded)
            with self.assertRaises(GEMINI.GeminiError):
                self.synthesize()
            self.assertEqual(self.output.read_bytes(), b"previous recording")
            self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_failed_publish_preserves_previous_file_and_cleans_temporary(self):
        self.output.write_bytes(b"previous recording")
        self.engine._receive = mock.AsyncMock(return_value=wav_bytes())
        with mock.patch.object(GEMINI.os, "replace", side_effect=OSError("private details")):
            with self.assertRaisesRegex(GEMINI.GeminiError, "disk space"):
                self.synthesize()
        self.assertEqual(self.output.read_bytes(), b"previous recording")
        self.assertEqual(os.listdir(self.directory.name), ["speech.wav"])

    def test_cancellation_before_request_never_loads_key_or_network(self):
        cancelled = threading.Event()
        cancelled.set()
        self.engine._receive = mock.AsyncMock()
        self.engine._get_key = mock.Mock()
        with self.assertRaises(GEMINI.SpeechCancelled):
            self.synthesize(cancel_event=cancelled)
        self.engine._receive.assert_not_awaited()
        self.engine._get_key.assert_not_called()

    def test_inflight_cancel_reaps_transport_and_preserves_existing_output(self):
        cancelled = threading.Event()
        closed = threading.Event()
        self.output.write_bytes(b"previous recording")

        async def receive(*_args):
            cancelled.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        self.engine._receive = receive
        with self.assertRaises(GEMINI.SpeechCancelled):
            self.synthesize(cancel_event=cancelled)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.output.read_bytes(), b"previous recording")

    def test_timeout_reaps_request_without_retry(self):
        closed = threading.Event()

        async def receive(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        self.engine.request_timeout = .01
        self.engine._receive = mock.AsyncMock(side_effect=receive)
        with self.assertRaisesRegex(GEMINI.GeminiError, "timed out.*may still count"):
            self.synthesize()
        self.assertTrue(closed.is_set())
        self.engine._receive.assert_awaited_once()

    def test_cancel_after_audio_arrives_preserves_file(self):
        cancelled = threading.Event()
        self.output.write_bytes(b"previous recording")

        async def receive(*_args):
            cancelled.set()
            return wav_bytes()

        self.engine._receive = receive
        with self.assertRaises(GEMINI.SpeechCancelled):
            self.synthesize(cancel_event=cancelled)
        self.assertEqual(self.output.read_bytes(), b"previous recording")

    def test_construct_warm_and_ready_are_offline(self):
        with mock.patch("aiohttp.ClientSession") as network:
            engine = GEMINI.GeminiEngine()
            self.assertTrue(engine.ready())
            self.assertFalse(engine.warm("Charon", force=True))
            self.assertFalse(engine.lookahead)
        network.assert_not_called()


class CostAndCredentialTests(unittest.TestCase):
    def test_cost_date_boundary_and_duration_validation(self):
        self.assertAlmostEqual(GEMINI.estimate_audio_cost(3600, datetime.date(2026, 12, 31)), .81)
        self.assertAlmostEqual(GEMINI.estimate_audio_cost(3600, datetime.date(2027, 1, 1)), 1.62)
        self.assertEqual(GEMINI.estimate_audio_cost(0), 0)
        for duration in (-1, float("nan"), float("inf"), True, "bad"):
            with self.assertRaises(ValueError):
                GEMINI.estimate_audio_cost(duration)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
    def test_real_dpapi_roundtrip_encrypted_only_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GEMINI.CredentialStore(Path(directory) / "keys")
            self.assertFalse(store.exists())
            self.assertIsNone(store.load())
            self.assertFalse((Path(directory) / "keys").exists())
            synthetic = "synthetic-offline-test-key-never-transmitted"
            store.save(synthetic)
            self.assertTrue(store.exists())
            self.assertNotIn(synthetic.encode(), store.path.read_bytes())
            self.assertEqual(store.load(), synthetic)
            store.remove()
            self.assertFalse(store.exists())
            store.remove()

    def test_plaintext_and_oversized_credentials_are_never_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GEMINI.CredentialStore(directory)
            for content in (b'plaintext-key', GEMINI._CREDENTIAL_MAGIC + b'a' * 16385):
                store.path.write_bytes(content)
                with self.assertRaises(GEMINI.GeminiError):
                    store.load()

    def test_failed_key_save_preserves_existing_encrypted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GEMINI.CredentialStore(directory)
            store.path.write_bytes(b"existing encrypted data")
            with mock.patch.object(GEMINI, "_dpapi", return_value=b"encrypted bytes"), mock.patch.object(
                    GEMINI.os, "replace", side_effect=OSError("failure")):
                with self.assertRaises(GEMINI.GeminiError):
                    store.save("synthetic-key")
            self.assertEqual(store.path.read_bytes(), b"existing encrypted data")
            self.assertEqual(os.listdir(directory), [store.path.name])


if __name__ == "__main__":
    unittest.main()
