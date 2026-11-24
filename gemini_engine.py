"""Opt-in Gemini 3.8 Flash TTS, with no startup requests or automatic retries.

REST contract and prices verified 2026-09-27:
https://ai.google.dev/gemini-api/docs/speech-generation
https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash-tts

Text is a verbatim transcript. Delivery instructions belong in speech_metadata,
not in spoken text. Each synth_to_wav call makes at most one API request. Stop
closes the local connection; it cannot promise that Google cancels billing.
"""

import asyncio
import base64
import binascii
import ctypes
import datetime
import importlib
import io
import json
import math
import os
import tempfile
import wave
from pathlib import Path

from edge_engine import SpeechCancelled


GEMINI_MODEL = "gemini-3.8-flash-tts"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MODEL_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL
DEFAULT_STYLE = "Warm, clear audiobook narration; thoughtful pacing and natural emphasis."
DEFAULT_GEMINI_VOICE = "Charon"
DEFAULT_GEMINI_LABEL = "Charon (informative) - Gemini 3.8, online"
# Descriptors, not inferred genders or accents, from Google's prebuilt catalog.
GEMINI_VOICES = {
    DEFAULT_GEMINI_LABEL: ("Charon", "en-us"),
    "Sulafat (warm) - Gemini 3.8, online": ("Sulafat", "en-us"),
    "Algieba (smooth) - Gemini 3.8, online": ("Algieba", "en-us"),
    "Kore (firm) - Gemini 3.8, online": ("Kore", "en-us"),
    "Iapetus (clear) - Gemini 3.8, online": ("Iapetus", "en-us"),
    "Sadaltager (knowledgeable) - Gemini 3.8, online": ("Sadaltager", "en-us"),
}
SUPPORTED_VOICE_IDS = frozenset(voice for voice, _ in GEMINI_VOICES.values())
GEMINI_NOTICE = (
    "Gemini sends the spoken text to Google. An API key and internet are required. "
    "Google API usage may be billed separately; estimates are not spending limits.")
SAMPLE_RATE = 24000
_CREDENTIAL_MAGIC = b"PiperReader Gemini DPAPI v1\0"


class GeminiError(RuntimeError):
    """A safe message and allowlisted diagnostics, never a raw server response."""

    def __init__(self, message, *, http_status=None, error_code=None, parameter=None):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.parameter = parameter


# Only these fixed strings may leave the error parser. In particular, never
# append a server message, arbitrary code, metadata, parameter or request ID.
_ERROR_CATEGORIES = {
    "invalid_request": "request", "parameter_unknown": "parameter",
    "failed_precondition": "precondition", "authentication": "key",
    "payment_required": "payment", "permission_denied": "permission",
    "not_found": "missing", "model_not_found": "model",
    "rate_limit_exceeded": "rate", "quota_exceeded": "quota",
    "too_many_requests": "rate", "out_of_range": "request",
    "api_error": "server", "service_unavailable": "server",
    "deadline_exceeded": "server", "unimplemented": "unsupported",
    "already_exists": "request", "aborted": "server", "cancelled": "cancelled",
    "safety": "blocked", "recitation": "blocked", "language": "language",
    "prohibited_content": "blocked", "spii": "blocked",
    "blocklist": "blocked", "content_blocked": "blocked",
    "INVALID_ARGUMENT": "request", "FAILED_PRECONDITION": "precondition",
    "UNAUTHENTICATED": "key", "PERMISSION_DENIED": "permission",
    "NOT_FOUND": "missing", "RESOURCE_EXHAUSTED": "quota",
    "OUT_OF_RANGE": "request", "INTERNAL": "server",
    "UNAVAILABLE": "server", "DEADLINE_EXCEEDED": "server",
    "UNIMPLEMENTED": "unsupported", "CANCELLED": "cancelled",
    "API_KEY_INVALID": "key", "API_KEY_EXPIRED": "key",
    "API_KEY_NOT_FOUND": "key", "API_KEY_LEAKED": "key_leaked",
    "API_KEY_SERVICE_BLOCKED": "restriction",
    "API_KEY_HTTP_REFERRER_BLOCKED": "restriction",
    "API_KEY_IP_ADDRESS_BLOCKED": "restriction",
    "API_KEY_ANDROID_APP_BLOCKED": "restriction",
    "API_KEY_IOS_APP_BLOCKED": "restriction",
    "SERVICE_DISABLED": "service", "BILLING_DISABLED": "precondition",
    "CONSUMER_INVALID": "permission", "CONSUMER_SUSPENDED": "permission",
}
_ERROR_PARAMETERS = frozenset({
    "model", "input", "input[0]", "input[0].content",
    "input[0].content[0]", "input[0].content[0].text",
    "input[0].content[0].annotations",
    "input[0].content[0].annotations[0].style",
    "input[0].content[0].annotations[0].type",
    "generation_config", "generation_config.speech_config",
    "generation_config.speech_config[0].voice",
    "generation_config.speech_config.speakers",
    "response_format", "response_format.type", "response_format.mime_type",
    "response_format.sample_rate", "store", "stream",
})
_FIXED_KEY_ERRORS = {
    "API key not valid. Please pass a valid API key.": "API_KEY_INVALID",
    "API key expired. Please renew the API key.": "API_KEY_EXPIRED",
    "Your API key was reported as leaked. Please use another API key.": "API_KEY_LEAKED",
}


def _cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SpeechCancelled("Gemini speech stopped")


def _number(value, minimum, maximum, default=1.0):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value)) if math.isfinite(value) else default


def estimate_audio_cost(seconds, on_date=None):
    """Return a USD estimate for standard audio output only, not a charge cap.

    Uses 25 audio tokens/second, $9/million through 2026-12-31 and $18/million
    thereafter as published 2026-09-27. Excludes input tokens, script writing,
    tax and regenerated attempts. Free-tier eligibility is not assumed.
    """
    if isinstance(seconds, bool):
        raise ValueError("Audio duration must be a nonnegative number")
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Audio duration must be a nonnegative number") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Audio duration must be a nonnegative number")
    day = on_date if on_date is not None else datetime.date.today()
    if isinstance(day, datetime.datetime):
        day = day.date()
    if not isinstance(day, datetime.date):
        raise ValueError("The estimate date must be a calendar date")
    price = 9.0 if day <= datetime.date(2026, 12, 31) else 18.0
    return seconds * 25.0 * price / 1_000_000


def _validate_key(value):
    if not isinstance(value, str) or not value.strip():
        raise GeminiError("Enter a Gemini API key in Textbook Studio to use this voice.")
    key = value.strip()
    if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise GeminiError("The Gemini API key has an invalid format. Paste the key again.")
    return key


def _dpapi(data, protect):
    """Encrypt/decrypt for this Windows user, with no interactive dialogs."""
    if os.name != "nt":
        raise GeminiError("Saving a Gemini key requires Windows account encryption.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = DataBlob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_uint32, ctypes.POINTER(DataBlob)]
    function.restype = ctypes.c_int
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately omit LOCAL_MACHINE scope.
    success = function(ctypes.byref(source), None, None, None, None,
                       0x1, ctypes.byref(result))
    try:
        if not success or not result.data or not result.size:
            raise GeminiError("Windows could not unlock or save the Gemini key. Enter it again.")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel32.LocalFree(result.data)


class CredentialStore:
    """Only explicit save() persists a key, encrypted for this Windows account.

    Constructing this object does not touch disk. Never include this file in
    lesson projects, application settings, support logs or release packages.
    """

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "gemini_api_key.dpapi"

    def exists(self):
        return self.path.is_file()

    def save(self, key):
        key = _validate_key(key)
        encoded = _CREDENTIAL_MAGIC + _dpapi(key.encode("ascii"), True)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".gemini-key-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise GeminiError("The encrypted Gemini key could not be saved. Check the app data folder.") from None
        finally:
            if temporary:
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def load(self):
        try:
            with self.path.open("rb") as stream:
                encoded = stream.read(16385)
        except FileNotFoundError:
            return None
        except OSError:
            raise GeminiError("The saved Gemini key could not be opened. Enter it again.") from None
        if len(encoded) > 16384 or not encoded.startswith(_CREDENTIAL_MAGIC):
            raise GeminiError("The saved Gemini key is invalid. Remove it and enter it again.")
        try:
            return _validate_key(_dpapi(encoded[len(_CREDENTIAL_MAGIC):], False).decode("ascii"))
        except (UnicodeError, ValueError):
            raise GeminiError("The saved Gemini key is invalid. Remove it and enter it again.") from None

    def remove(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GeminiError("The saved Gemini key could not be removed. Check the app data folder.") from None


class GeminiEngine:
    name = "Gemini 3.8 Flash TTS"
    initial_chunk_len = 1800
    chunk_len = 2400
    lookahead = False  # Do not start extra billed requests ahead of playback.
    supports_cancellation = True
    request_timeout = 180.0
    max_text_characters = 6000
    max_audio_bytes = 32 * 1024 * 1024
    max_response_bytes = 45 * 1024 * 1024
    max_error_bytes = 16 * 1024
    max_metadata_bytes = 64 * 1024

    def __init__(self, status_cb=None, api_key=None, key_provider=None,
                 model=GEMINI_MODEL, style=DEFAULT_STYLE):
        if model != GEMINI_MODEL:
            raise ValueError("Choose the supported Gemini 3.8 Flash TTS model")
        if key_provider is not None and not callable(key_provider):
            raise ValueError("The Gemini key provider must be callable")
        if not isinstance(style, str) or len(style) > 2000:
            raise ValueError("Gemini delivery instructions must be a short text string")
        self.status = status_cb if status_cb is not None else lambda _message: None
        self._api_key = api_key
        self._key_provider = key_provider
        self.model = model
        self.style = style.strip()

    def ready(self):
        """Offline dependency check; deliberately does not validate a key online."""
        try:
            importlib.import_module("aiohttp")
            importlib.import_module("numpy")
            return True
        except (ImportError, OSError):
            return False

    def warm(self, voice_id, lang=None, force=False):
        return False

    def _get_key(self):
        if self._api_key is not None:
            return _validate_key(self._api_key)
        if self._key_provider is not None:
            try:
                key = self._key_provider()
            except Exception:
                raise GeminiError("The Gemini key could not be loaded. Enter it again.") from None
            if key:
                return _validate_key(key)
        return _validate_key(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def key_available(self):
        """Offline presence/format check only; no secret is returned to the UI."""
        try:
            self._get_key()
            return True
        except GeminiError:
            return False

    def _payload(self, text, voice_id, speed):
        if self.model != GEMINI_MODEL:
            raise ValueError("Choose the supported Gemini 3.8 Flash TTS model")
        if voice_id not in SUPPORTED_VOICE_IDS:
            raise ValueError("Choose an available Gemini preset voice")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("There is no text to read")
        if len(text) > self.max_text_characters:
            raise ValueError("This Gemini speech passage is too long. Split it into shorter passages.")
        speed = _number(speed, 0.5, 2.5)
        style = self.style
        if speed != 1:
            style = (style + f" Speak at approximately {speed:g} times a natural reading pace.").strip()
        return {
            "model": self.model,
            "input": [{"type": "user_input", "content": [{
                "type": "text", "text": text,
                "annotations": [{"type": "speech_metadata", "style": style}],
            }]}],
            "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": SAMPLE_RATE},
            "generation_config": {"speech_config": [{"voice": voice_id}]},
            "store": False,
            "stream": False,
        }

    @staticmethod
    def _http_error(status, document=None, *, access_check=False):
        code = parameter = None
        error = document.get("error") if isinstance(document, dict) else None
        if isinstance(error, dict):
            # Interactions uses a string code; the Models/legacy API uses a
            # numeric code plus an uppercase status and optional ErrorInfo.
            for candidate in (error.get("code"), error.get("status")):
                if isinstance(candidate, str) and candidate in _ERROR_CATEGORIES:
                    code = candidate
                    break
            candidate = error.get("param", error.get("parameter"))
            if isinstance(candidate, str) and candidate in _ERROR_PARAMETERS:
                parameter = candidate
            details = error.get("details")
            if isinstance(details, list):
                for detail in details[:64]:
                    if not isinstance(detail, dict):
                        continue
                    reason = detail.get("reason")
                    if isinstance(reason, str) and reason in _ERROR_CATEGORIES:
                        code = reason
                    violations = detail.get("fieldViolations")
                    if isinstance(violations, list):
                        for violation in violations[:64]:
                            candidate = violation.get("field") if isinstance(violation, dict) else None
                            if isinstance(candidate, str) and candidate in _ERROR_PARAMETERS:
                                parameter = candidate
                                break
            message = error.get("message")
            if isinstance(message, str) and message in _FIXED_KEY_ERRORS:
                code = _FIXED_KEY_ERRORS[message]
        category = _ERROR_CATEGORIES.get(code)
        if category is None:
            category = {400: "request", 401: "key", 402: "payment", 403: "permission",
                        404: "missing", 416: "request", 422: "request", 429: "quota"}.get(status, "server")
        advice = {
            "key": "Google rejected the API key. Check or replace it in Google AI Studio, then enter it again in Textbook Studio.",
            "key_leaked": "Google has blocked this API key. Create a replacement in Google AI Studio and enter it in Textbook Studio.",
            "restriction": "The API key's restrictions blocked this app. Check its Gemini API and application restrictions in your Google project.",
            "permission": "The Google project or API key does not have permission. Check project access and API key restrictions in Google AI Studio.",
            "service": "The Gemini API is disabled for this Google project. Check the project's API settings in Google AI Studio.",
            "precondition": "Google requires an account or project prerequisite. Check Google AI Studio for billing, region or account requirements; the app has not enabled billing.",
            "payment": "Google reports that billing credits are unavailable. Review your project's billing status in Google AI Studio; the app has not purchased credits.",
            "model": "Google could not find Gemini 3.8 Flash TTS for this request. Check model availability for your project in Google AI Studio.",
            "missing": "Google could not find the requested Gemini resource. Check Gemini 3.8 Flash TTS availability for your project in Google AI Studio.",
            "parameter": "Google rejected an unrecognized request parameter. This needs an app compatibility check; changing the key may not help.",
            "request": "Google rejected the request parameters. This needs an app compatibility check; the response does not establish a key or billing problem.",
            "rate": "The Gemini request rate limit was reached. Wait before trying again and check Google AI Studio's limits.",
            "quota": "The Gemini quota or rate limit was reached. Check Google AI Studio's limits before trying again.",
            "unsupported": "Google does not support this request or feature for the current model. Check the project's model availability in Google AI Studio.",
            "blocked": "Google blocked speech generation for this text. Review the text and Google's content guidance before trying again.",
            "language": "Google did not support the requested speech language. Review the text and the model's supported languages.",
            "cancelled": "Google cancelled this request. Try again only when you are ready.",
            "server": "Gemini is temporarily unavailable or returned an unexpected response. Check your connection and try again when ready.",
        }[category]
        if access_check and category in ("model", "missing"):
            advice = ("Google did not return metadata for Gemini 3.8 Flash TTS. "
                      "This read-only check cannot establish speech access; check model availability "
                      "in Google AI Studio. No audio was generated.")
        label = f"Google Gemini error (HTTP {status}"
        if code:
            label += f"; {code}"
        label += "). "
        if parameter:
            advice += f" Parameter: {parameter}."
        return GeminiError(label + advice + " No automatic retry was made.",
                           http_status=status, error_code=code, parameter=parameter)

    async def _response_error(self, response, *, access_check=False):
        document = None
        if response.content_length is None or response.content_length <= self.max_error_bytes:
            data = bytearray()
            async for block in response.content.iter_chunked(4096):
                if len(data) + len(block) > self.max_error_bytes:
                    break
                data.extend(block)
            else:
                try:
                    document = json.loads(data)
                except (ValueError, UnicodeError, RecursionError):
                    pass
        return self._http_error(response.status, document, access_check=access_check)

    async def _receive(self, payload, key):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.request_timeout, connect=15, sock_read=self.request_timeout)
        # Never forward the secret to a redirect or a URL query parameter.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(
                    GEMINI_ENDPOINT, headers={"x-goog-api-key": key},
                    json=payload, allow_redirects=False) as response:
                if response.status != 200:
                    raise await self._response_error(response)
                if response.content_length is not None and response.content_length > self.max_response_bytes:
                    raise GeminiError("The Gemini audio response exceeded the safe size limit.")
                data = bytearray()
                async for block in response.content.iter_chunked(65536):
                    if len(data) + len(block) > self.max_response_bytes:
                        raise GeminiError("The Gemini audio response exceeded the safe size limit.")
                    data.extend(block)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeError):
            raise GeminiError("Google returned an unreadable speech response. No automatic retry was made.") from None
        return self._decode_audio(document)

    def _decode_audio(self, document):
        invalid = "Google returned incomplete or invalid speech audio. No automatic retry was made."
        if (not isinstance(document, dict) or document.get("error")
                or document.get("status", "completed") != "completed"):
            raise GeminiError(invalid)
        steps = document.get("steps")
        if not isinstance(steps, list) or len(steps) > 1000:
            raise GeminiError(invalid)
        audio = []
        for step in steps:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                raise GeminiError(invalid)
            audio.extend(item for item in content if isinstance(item, dict) and item.get("type") == "audio")
        if len(audio) != 1:
            raise GeminiError(invalid)
        item = audio[0]
        mime_type = item.get("mime_type", "audio/wav")
        if (not isinstance(mime_type, str)
                or mime_type.split(";", 1)[0].lower() not in ("audio/wav", "audio/x-wav")):
            raise GeminiError(invalid)
        encoded = item.get("data")
        if not isinstance(encoded, str) or not encoded or len(encoded) > (self.max_audio_bytes + 2) // 3 * 4:
            raise GeminiError(invalid)
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise GeminiError(invalid) from None
        if len(decoded) > self.max_audio_bytes:
            raise GeminiError(invalid)
        return decoded

    async def _receive_access(self, key):
        """One metadata GET, documented at https://ai.google.dev/api/models.

        Metadata is weaker evidence than a successful synthesis. In particular,
        this does not verify Interactions access, speech quota or billing.
        """
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.request_timeout, connect=15, sock_read=self.request_timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(
                    GEMINI_MODEL_ENDPOINT, headers={"x-goog-api-key": key},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise await self._response_error(response, access_check=True)
                if response.content_length is not None and response.content_length > self.max_metadata_bytes:
                    raise GeminiError("Google returned oversized model metadata. Speech access remains unverified.")
                data = bytearray()
                async for block in response.content.iter_chunked(4096):
                    if len(data) + len(block) > self.max_metadata_bytes:
                        raise GeminiError("Google returned oversized model metadata. Speech access remains unverified.")
                    data.extend(block)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise GeminiError("Google returned unreadable model metadata. Speech access remains unverified.") from None
        if not isinstance(document, dict) or document.get("name") != "models/" + GEMINI_MODEL:
            raise GeminiError("Google did not return the expected model metadata. Speech access remains unverified.")
        # Do not return arbitrary metadata, request identifiers or a false
        # assurance of synthesis access based on catalog visibility alone.
        return {
            "key_accepted": True,
            "model_available": True,
            "model": GEMINI_MODEL,
            "generation_verified": False,
            "quota_verified": False,
            "billing_verified": False,
            "message": ("Google accepted the key for a read-only model lookup and returned "
                        "Gemini 3.8 Flash TTS metadata. Speech generation, quota and billing "
                        "remain unverified. No text was sent and no audio was generated."),
        }

    async def _wait_for_transport(self, receive, cancel_event, timeout_message):
        _cancelled(cancel_event)
        task = asyncio.create_task(receive())
        deadline = asyncio.get_running_loop().time() + self.request_timeout
        try:
            while True:
                _cancelled(cancel_event)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GeminiError(timeout_message)
                finished, _ = await asyncio.wait((task,), timeout=min(0.05, remaining))
                if finished:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _request(self, payload, key, cancel_event):
        return await self._wait_for_transport(
            lambda: self._receive(payload, key), cancel_event,
            "Gemini speech timed out. No automatic retry was made; Google may still count this request.")

    def check_access(self, cancel_event=None):
        """Worker-thread API: explicit read-only lookup, no audio or retry.

        Success confirms catalog metadata only. Missing metadata raises a safe
        error without claiming that the model is unavailable for synthesis.
        """
        _cancelled(cancel_event)
        if self.model != GEMINI_MODEL:
            raise ValueError("Choose the supported Gemini 3.8 Flash TTS model")
        key = self._get_key()
        try:
            result = asyncio.run(self._wait_for_transport(
                lambda: self._receive_access(key), cancel_event,
                "The Gemini access check timed out. No audio was generated and no automatic retry was made."))
            _cancelled(cancel_event)
            return result
        except (SpeechCancelled, GeminiError):
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise GeminiError("The Gemini access check timed out. No audio was generated and no automatic retry was made.") from None
        except Exception:
            raise GeminiError("The Gemini access check could not connect. Check your internet connection. No audio was generated and no automatic retry was made.") from None
        finally:
            key = None

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path, cancel_event=None):
        """Worker-thread interface: one request, validated PCM16/24kHz, atomic save.

        Language is auto-detected by Gemini. Speed is a delivery instruction,
        not an exact time-stretch ratio. Volume is applied to PCM locally.
        """
        _cancelled(cancel_event)
        payload = self._payload(text, voice_id, speed)
        destination = os.path.abspath(os.fspath(out_path))
        if not os.path.isdir(os.path.dirname(destination)):
            raise GeminiError("The audio output folder does not exist.")
        key = self._get_key()
        try:
            encoded = asyncio.run(self._request(payload, key, cancel_event))
        except (SpeechCancelled, GeminiError):
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise GeminiError("Gemini speech timed out. No automatic retry was made; Google may still count this request.") from None
        except Exception:
            # Never include exception text, response bodies, headers or cause.
            raise GeminiError("Gemini speech could not connect. Check your internet connection and try again when ready. No automatic retry was made.") from None
        finally:
            key = None
        _cancelled(cancel_event)
        temporary = None
        try:
            import numpy as np

            if not isinstance(encoded, bytes) or len(encoded) > self.max_audio_bytes:
                raise ValueError("Invalid audio")
            with wave.open(io.BytesIO(encoded), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                    raise ValueError("Invalid audio format")
                frames = source.getnframes()
                if frames <= 0 or frames * 2 > self.max_audio_bytes:
                    raise ValueError("Invalid audio size")
                pcm = source.readframes(frames)
                if len(pcm) != frames * 2:
                    raise ValueError("Incomplete audio")
            gain = _number(volume, 0.0, 1.0)
            if gain != 1:
                pcm = (np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain).astype("<i2").tobytes()
            _cancelled(cancel_event)
            descriptor, temporary = tempfile.mkstemp(prefix="gemini-", suffix=".wav", dir=os.path.dirname(destination))
            os.close(descriptor)
            with wave.open(temporary, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                output.writeframes(pcm)
            _cancelled(cancel_event)
            os.replace(temporary, destination)
            temporary = None
        except SpeechCancelled:
            raise
        except (ValueError, wave.Error, EOFError):
            raise GeminiError("Google returned invalid or incomplete WAV audio. No automatic retry was made.") from None
        except Exception:
            raise GeminiError("Gemini audio could not be saved. Check the output folder and free disk space.") from None
        finally:
            if temporary:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
