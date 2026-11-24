"""Microsoft online voices, adapted to Piper Reader's PCM WAV interface.

Only synth_to_wav contacts Microsoft's speech service. Construction, readiness
checks and warm() are offline, including when an online voice is saved.
"""

import asyncio
import importlib
import io
import math
import os
import tempfile
import wave


ANDREW_VOICE_ID = "en-US-AndrewMultilingualNeural"
ANDREW_LABEL = "Andrew (US male) - Microsoft, online"
AVA_VOICE_ID = "en-US-AvaMultilingualNeural"
AVA_LABEL = "Ava (US female) - Microsoft, online"
EMMA_VOICE_ID = "en-US-EmmaMultilingualNeural"
EMMA_LABEL = "Emma (US female) - Microsoft, online"
SONIA_VOICE_ID = "en-GB-SoniaNeural"
SONIA_LABEL = "Sonia (UK female) - Microsoft, online"
ONLINE_VOICES = {
    ANDREW_LABEL: (ANDREW_VOICE_ID, "en-us"),
    AVA_LABEL: (AVA_VOICE_ID, "en-us"),
    EMMA_LABEL: (EMMA_VOICE_ID, "en-us"),
    SONIA_LABEL: (SONIA_VOICE_ID, "en-gb"),
}
SUPPORTED_VOICE_IDS = frozenset(voice_id for voice_id, _ in ONLINE_VOICES.values())
ONLINE_NOTICE = "Microsoft online voice: text is sent to Microsoft. Internet required."


def _bounded_number(value, minimum, maximum, default=1.0):
    if isinstance(value, bool):
        value = default
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def edge_rate(speed):
    """Translate the reader's 0.5x-2.5x speed to Edge's percent change."""
    speed = round(_bounded_number(speed, 0.5, 2.5), 2)
    return f"{round((speed - 1.0) * 100):+d}%"


class SpeechCancelled(RuntimeError):
    """Internal interruption; the reader suppresses errors after Stop."""


class OnlineSpeechError(RuntimeError):
    """Safe failure details for status messages and content-free diagnostics."""

    def __init__(self, reason, http_status=None):
        messages = {
            "empty_response": "Microsoft's online speech service returned no audio.",
            "timeout": "Microsoft's online speech service took too long to respond.",
            "network": "Microsoft's online speech service is unavailable.",
            "rejected": "Microsoft's online speech service did not accept the request.",
            "protocol": "Microsoft's online speech service is unavailable.",
        }
        self.reason = reason if reason in messages else "protocol"
        self.http_status = (
            http_status if isinstance(http_status, int)
            and not isinstance(http_status, bool) else None)
        super().__init__(
            messages[self.reason] + " Check your internet connection, or choose "
            "a Piper, Kokoro, or Windows voice for offline reading.")


def _online_speech_error(exc):
    """Classify known transport failures without exposing their raw messages."""
    if isinstance(exc, TimeoutError):
        return OnlineSpeechError("timeout")
    try:
        from edge_tts.exceptions import NoAudioReceived
        if isinstance(exc, NoAudioReceived):
            return OnlineSpeechError("empty_response")
    except ImportError:
        pass
    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientResponseError):
            return OnlineSpeechError("rejected", exc.status)
        if isinstance(exc, aiohttp.ClientConnectionError):
            return OnlineSpeechError("network")
    except ImportError:
        pass
    if isinstance(exc, (ConnectionError, OSError)):
        return OnlineSpeechError("network")
    return OnlineSpeechError("protocol")


class EdgeEngine:
    name = "Microsoft online"
    # Upstream's v2 protocol fix still reports version 7.2.8. Pin its complete
    # source archive so a source install cannot silently restore the v1 client.
    package = (
        "edge-tts @ https://github.com/rany2/edge-tts/archive/"
        "ba61d94442c18893f0c4d3c5ad253718b21ab93e.zip"
        "#sha256=7cb0766016de59572cb8c7c31ed865b995034d8db7872199e8a83172f7e9a34d"
    )
    module = "edge_tts"
    initial_chunk_len = 150
    chunk_len = 700
    lookahead = True
    supports_cancellation = True
    request_timeout = 30.0
    retry_delay = 0.35
    max_audio_bytes = 8 * 1024 * 1024

    def __init__(self, status_cb, install_cb=None, *, max_attempts=1,
                 request_timeout=30.0):
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or max_attempts not in (1, 2)):
            raise ValueError("Online speech permits one or two attempts")
        if (isinstance(request_timeout, bool)
                or not isinstance(request_timeout, (int, float))
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("Online speech timeout must be positive and finite")
        self.status = status_cb
        self._install = install_cb
        self.max_attempts = max_attempts
        self.request_timeout = float(request_timeout)

    def ready(self):
        try:
            for module in ("edge_tts", "soundfile", "numpy"):
                importlib.import_module(module)
            return True
        except (ImportError, OSError):
            return False

    def install(self):
        if self._install is None:
            self.status("Microsoft online voice components are missing. Reinstall Piper Reader.")
            return False
        for package in (self.package, "soundfile>=0.13.1"):
            if not self._install(package, self.status):
                return False
        return self.ready()

    def warm(self, voice_id, lang=None, force=False):
        """Online voices never send warm-up text or make startup requests."""
        return False

    async def _receive(self, text, voice_id, rate):
        import edge_tts

        # Volume is applied to PCM locally, consistently with the local engines.
        speech = edge_tts.Communicate(
            text, voice_id, rate=rate, volume="+0%",
            connect_timeout=10, receive_timeout=20)
        audio = bytearray()
        async for part in speech.stream():
            if part["type"] == "audio":
                audio.extend(part["data"])
                if len(audio) > self.max_audio_bytes:
                    raise RuntimeError("The speech response was too large")
        if not audio:
            raise edge_tts.exceptions.NoAudioReceived(
                "The speech service returned no audio")
        return bytes(audio)

    async def _request(self, text, voice_id, rate, cancel_event):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.request_timeout

        def remaining_time():
            if cancel_event is not None and cancel_event.is_set():
                raise SpeechCancelled("Speech stopped")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Online speech timed out")
            return remaining

        remaining_time()
        from edge_tts.exceptions import NoAudioReceived

        for attempt in range(self.max_attempts):
            remaining_time()
            task = asyncio.create_task(self._receive(text, voice_id, rate))
            try:
                while True:
                    finished, _ = await asyncio.wait(
                        (task,), timeout=min(0.05, remaining_time()))
                    remaining_time()
                    if finished:
                        return task.result()
            except NoAudioReceived:
                remaining_time()
                if attempt + 1 >= self.max_attempts:
                    raise
            finally:
                if not task.done():
                    task.cancel()
                # Reap each transport before retrying or returning to the worker.
                await asyncio.gather(task, return_exceptions=True)

            remaining_time()
            self.status("Microsoft returned no audio. Retrying once...")
            remaining_time()
            retry_at = loop.time() + self.retry_delay
            while loop.time() < retry_at:
                await asyncio.sleep(min(
                    0.05, remaining_time(), max(0.0, retry_at - loop.time())))
            remaining_time()

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path,
                     cancel_event=None):
        """Generate on a worker thread and atomically write mono PCM16 WAV."""
        if voice_id not in SUPPORTED_VOICE_IDS:
            raise ValueError("The selected Microsoft online voice is unavailable")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("There is no text to read")
        try:
            encoded = asyncio.run(self._request(
                text, voice_id, edge_rate(speed), cancel_event))
        except SpeechCancelled:
            raise
        except Exception as exc:
            raise _online_speech_error(exc) from None

        temporary = None
        try:
            import numpy as np
            import soundfile as sf

            samples, sample_rate = sf.read(
                io.BytesIO(encoded), dtype="float32", always_2d=True)
            if samples.size == 0 or sample_rate <= 0 or not np.isfinite(samples).all():
                raise ValueError("Invalid speech audio")
            # The service currently returns mono MP3; preserve mono playback
            # if a later service update returns stereo instead.
            samples = samples.mean(axis=1)
            gain = _bounded_number(volume, 0.0, 1.0)
            pcm = (np.clip(samples * gain, -1.0, 1.0) * 32767).astype("<i2")
            if cancel_event is not None and cancel_event.is_set():
                raise SpeechCancelled("Speech stopped")
            descriptor, temporary = tempfile.mkstemp(
                prefix="microsoft-", suffix=".wav", dir=os.path.dirname(
                    os.path.abspath(out_path)))
            os.close(descriptor)
            with wave.open(temporary, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(int(sample_rate))
                stream.writeframes(pcm.tobytes())
            if cancel_event is not None and cancel_event.is_set():
                raise SpeechCancelled("Speech stopped")
            os.replace(temporary, out_path)
            temporary = None
        except SpeechCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Microsoft online audio could not be saved. Check the output folder "
                "and available disk space. If the problem continues, reinstall "
                "Piper Reader or choose a local voice.") from exc
        finally:
            if temporary:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
