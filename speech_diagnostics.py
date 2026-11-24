"""Small local speech diagnostics that never accept source text or secrets.

Only fixed enums and bounded numbers are written. Two files use at most 64 KiB
each. Construction is offline and creates no file; diagnostic failures never
prevent reading or expose exception details through logging's stderr handler.
"""

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import threading


_OUTCOMES = frozenset({"completed", "failed", "cancelled"})
_REASONS = frozenset({"empty_response", "timeout", "network", "rejected", "protocol", "unknown"})
_ENGINES = frozenset({"edge", "piper", "kokoro", "sapi", "unknown"})


def _safe_enum(value, choices, default="unknown"):
    return value if type(value) is str and value in choices else default


def _seconds(value):
    if type(value) not in (int, float):
        return 0.0
    if type(value) is float and not math.isfinite(value):
        return 0.0
    return round(float(max(0, min(value, 86400))), 3)


class _QuietRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # A failed log write must not emit a traceback, paths, or record details.
        pass


class SpeechDiagnostics:
    """Thread-safe per-reader logger with no shared/global logging handlers."""

    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        self._handler = _QuietRotatingHandler(
            self._path, maxBytes=65536, backupCount=1,
            encoding="utf-8", delay=True)
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def record(self, *, outcome, reason=None, http_status=None,
               elapsed_seconds=0, queue_wait_seconds=0,
               voice_engine="edge", chunks_played=0):
        """Append one safe timing record; unsupported outcome values are ignored."""
        if _safe_enum(outcome, _OUTCOMES, None) is None:
            return
        item = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "outcome": outcome,
            "reason": None if reason is None else _safe_enum(reason, _REASONS),
            "http_status": http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
            "elapsed_seconds": _seconds(elapsed_seconds),
            "queue_wait_seconds": _seconds(queue_wait_seconds),
            "voice_engine": _safe_enum(voice_engine, _ENGINES),
            "chunks_played": max(0, min(chunks_played, 1000000)) if type(chunks_played) is int else 0,
        }
        message = json.dumps(item, separators=(",", ":"), allow_nan=False)
        record = logging.LogRecord("PiperReader.speech", logging.INFO, "", 0, message, (), None)
        with self._lock:
            if self._closed:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._handler.handle(record)
            except (OSError, UnicodeError, ValueError):
                pass

    def close(self):
        """Close the owned file handle once; later records are ignored."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handler.close()
            except (OSError, ValueError):
                pass
