"""Per-copy online speech with an already-installed offline fallback."""

import os
import tempfile
import threading

from edge_engine import OnlineSpeechError, SpeechCancelled


class ClipboardSpeechEngine:
    """Keep one copied passage on the local voice after an online failure.

    The availability callback must check local files only. This wrapper never
    installs or downloads a voice during synthesis, and never stores the text.
    """

    name = "Microsoft online"
    chunk_len = 700
    initial_chunk_len = 150
    lookahead = True
    supports_cancellation = True

    def __init__(self, online, offline, *, offline_voice_id,
                 offline_lang="en-us", offline_available=lambda: False,
                 start_offline=False, on_fallback=None, status_cb=None):
        self._online = online
        self._offline = offline
        self._offline_voice_id = offline_voice_id
        self._offline_lang = offline_lang
        self._offline_available = offline_available
        self._on_fallback = on_fallback
        self._status = status_cb
        self._synthesis_lock = threading.Lock()
        self._fallback_error = None
        self.active_engine_key = "piper" if start_offline else "edge"
        self.fallback_reason = None
        self.fallback_http_status = None

    def _local_available(self):
        try:
            return bool(self._offline_available())
        except Exception:
            return False

    def ready(self):
        if self.active_engine_key == "piper":
            return self._local_available() and self._offline.ready()
        return self._online.ready()

    def install(self):
        if self.active_engine_key == "piper":
            return self.ready()
        return self._online.install()

    def warm(self, voice_id, lang=None, force=False):
        return False

    @staticmethod
    def _check_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise SpeechCancelled("Speech stopped")

    @staticmethod
    def _notify(callback, value):
        if callback is not None:
            try:
                callback(value)
            except Exception:
                # A closing UI must not interrupt speech or expose its errors.
                pass

    def _use_offline(self, error):
        self._fallback_error = error
        self.fallback_reason = error.reason
        self.fallback_http_status = error.http_status
        self.active_engine_key = "piper"
        self._notify(self._on_fallback, error)
        self._notify(
            self._status,
            "Microsoft voice unavailable. Continuing with Lessac offline.")

    def _synth_offline(self, text, speed, volume, out_path, cancel_event):
        self._check_cancelled(cancel_event)
        if not self._local_available():
            if self._fallback_error is not None:
                raise self._fallback_error
            raise RuntimeError("The Lessac offline voice is not available.")
        self._check_cancelled(cancel_event)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix="clipboard-offline-", suffix=".wav",
                dir=os.path.dirname(os.path.abspath(out_path)))
            os.close(descriptor)
            local_synthesis = getattr(
                type(self._offline), "synth_to_wav_local", None)
            if callable(local_synthesis):
                local_synthesis(
                    self._offline, text, self._offline_voice_id,
                    self._offline_lang, speed, volume, temporary)
            else:
                self._offline.synth_to_wav(
                    text, self._offline_voice_id, self._offline_lang,
                    speed, volume, temporary)
            self._check_cancelled(cancel_event)
            os.replace(temporary, out_path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def synth_to_wav(self, text, voice_id, lang, speed, volume, out_path,
                     cancel_event=None):
        self._check_cancelled(cancel_event)
        # Serialize synthesis if a caller overlaps lookahead requests. Once a
        # failure switches the passage to offline, no later chunk retries online.
        while not self._synthesis_lock.acquire(timeout=0.05):
            self._check_cancelled(cancel_event)
        try:
            self._check_cancelled(cancel_event)
            if self.active_engine_key == "edge":
                try:
                    return self._online.synth_to_wav(
                        text, voice_id, lang, speed, volume, out_path,
                        cancel_event=cancel_event)
                except OnlineSpeechError as error:
                    self._check_cancelled(cancel_event)
                    if not self._local_available():
                        raise
                    self._check_cancelled(cancel_event)
                    self._use_offline(error)
            return self._synth_offline(
                text, speed, volume, out_path, cancel_event)
        finally:
            self._synthesis_lock.release()
