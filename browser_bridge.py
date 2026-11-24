"""Secure loopback bridge used by the Piper Reader browser extension."""

from __future__ import annotations

import hmac
import json
import queue
import re
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CHUNKS = 5000
MAX_TEXT_CHARS = 250000
MAX_CHUNK_CHARS = 2000
MAX_SESSIONS = 8
MAX_EVENTS_PER_SESSION = 600
SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
ORIGIN_RE = re.compile(r"^(?:chrome-extension|extension)://[a-p]{32}$")


class BridgeBusy(RuntimeError):
    pass


class BrowserBridge:
    """Own a localhost HTTP server, command queue, and playback event stream."""

    def __init__(self, command_queue: queue.Queue, config_path: str):
        with open(config_path, encoding="utf-8") as stream:
            config = json.load(stream)
        self.port = int(config["port"])
        self.token = str(config["token"])
        configured_origins = config.get("extension_origins")
        if not isinstance(configured_origins, list):
            configured_origins = [config.get("extension_origin", "")]
        self.extension_origins = frozenset(
            str(origin) for origin in configured_origins)
        if not (1024 <= self.port <= 65535):
            raise ValueError("browser bridge port must be between 1024 and 65535")
        if len(self.token) < 32:
            raise ValueError("browser bridge token is too short")
        if (not self.extension_origins or
                any(not ORIGIN_RE.fullmatch(origin)
                    for origin in self.extension_origins)):
            raise ValueError("browser extension origins are invalid")

        self.command_queue = command_queue
        self._lock = threading.RLock()
        self._events = OrderedDict()
        self._seen_sessions = OrderedDict()
        self._sequence = 0
        self._accepting = False
        self._state = {
            "ready": False,
            "api_version": 1,
            "playback_state": "idle",
            "active_session": None,
            "voice": "",
            "speed": 1.0,
            "volume": 1.0,
        }
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            return
        server = ThreadingHTTPServer(("127.0.0.1", self.port), _BridgeHandler)
        server.daemon_threads = True
        server.bridge = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="PiperBrowserBridge",
            daemon=True,
        )
        self._thread.start()
        with self._lock:
            self._accepting = True
        self.update_state(ready=True)

    def stop(self):
        server, self._server = self._server, None
        with self._lock:
            self._accepting = False
        self.update_state(ready=False, playback_state="idle",
                          active_session=None)
        if server is not None:
            try:
                server.shutdown()
            finally:
                server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def update_state(self, **changes):
        with self._lock:
            self._state.update(changes)

    def health(self):
        with self._lock:
            state = dict(self._state)
        state["ok"] = bool(state.get("ready"))
        return state

    def publish(self, session_id, event_type, **payload):
        if not SESSION_RE.fullmatch(str(session_id or "")):
            return None
        with self._lock:
            self._sequence += 1
            event = {
                **payload,
                "seq": self._sequence,
                "type": str(event_type),
                "session_id": session_id,
                "timestamp": round(time.time(), 3),
            }
            events = self._events.get(session_id)
            if events is None:
                events = deque(maxlen=MAX_EVENTS_PER_SESSION)
                self._events[session_id] = events
            else:
                self._events.move_to_end(session_id)
            events.append(event)
            while len(self._events) > MAX_SESSIONS:
                self._events.popitem(last=False)
            return event

    def events_since(self, session_id, after):
        with self._lock:
            events = list(self._events.get(session_id, ()))
            state = dict(self._state)
            latest = events[-1]["seq"] if events else max(0, after)
            reset = bool(
                after and events and after < events[0]["seq"] - 1)
        return {
            "ok": True,
            "events": [event for event in events if event["seq"] > after],
            "latest": latest,
            "cursor": latest,
            "reset": reset,
            "state": state,
        }

    def _enqueue(self, command):
        with self._lock:
            if not self._accepting:
                raise BridgeBusy("Piper Reader is shutting down")
            try:
                self.command_queue.put_nowait(command)
            except queue.Full as exc:
                raise BridgeBusy("Piper Reader is busy; try again") from exc

    def enqueue_read(self, payload):
        session_id = str(payload.get("session_id", ""))
        if not SESSION_RE.fullmatch(session_id):
            raise ValueError("invalid browser session id")
        raw_chunks = payload.get("chunks")
        if not isinstance(raw_chunks, list) or not raw_chunks:
            raise ValueError("the webpage contained no readable chunks")
        if len(raw_chunks) > MAX_CHUNKS:
            raise ValueError("the webpage contains too many reading chunks")

        chunks, total_chars, ids = [], 0, set()
        for index, item in enumerate(raw_chunks):
            if not isinstance(item, dict):
                raise ValueError("invalid webpage chunk")
            chunk_id = str(item.get("id", f"c{index}"))[:100]
            text = str(item.get("text", "")).strip()
            sentence_id = item.get("sentence_id", index)
            paragraph_id = item.get("paragraph_id", index)
            if not text or len(text) > MAX_CHUNK_CHARS:
                raise ValueError("invalid webpage chunk text")
            if (isinstance(sentence_id, bool) or
                    not isinstance(sentence_id, int) or
                    not 0 <= sentence_id <= MAX_CHUNKS or
                    isinstance(paragraph_id, bool) or
                    not isinstance(paragraph_id, int) or
                    not 0 <= paragraph_id <= MAX_CHUNKS):
                raise ValueError("invalid webpage reading group")
            if chunk_id in ids:
                raise ValueError("duplicate webpage chunk id")
            ids.add(chunk_id)
            total_chars += len(text)
            if total_chars > MAX_TEXT_CHARS:
                raise ValueError("the webpage is too long to read safely")
            chunks.append({
                "id": chunk_id,
                "text": text,
                "index": index,
                "sentence_id": sentence_id,
                "paragraph_id": paragraph_id,
            })

        try:
            start_index = int(payload.get("start_index", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid start position") from exc
        if not (0 <= start_index < len(chunks)):
            raise ValueError("start position is outside the webpage")

        command = {
            "action": "read",
            "session_id": session_id,
            "title": str(payload.get("title", "Webpage"))[:500],
            "url": str(payload.get("url", ""))[:4096],
            "chunks": chunks,
            "start_index": start_index,
        }
        with self._lock:
            if session_id in self._seen_sessions:
                raise ValueError("browser session id has already been used")
            self._seen_sessions[session_id] = None
            while len(self._seen_sessions) > 2048:
                self._seen_sessions.popitem(last=False)
            try:
                self._enqueue(command)
            except Exception:
                self._seen_sessions.pop(session_id, None)
                raise
        return {
            "ok": True,
            "accepted": True,
            "session_id": session_id,
            "chunk_count": len(chunks),
        }

    def enqueue_control(self, payload):
        session_id = str(payload.get("session_id", ""))
        action = str(payload.get("action", "")).casefold()
        if not SESSION_RE.fullmatch(session_id):
            raise ValueError("invalid browser session id")
        if action not in {"pause", "resume", "toggle", "stop"}:
            raise ValueError("invalid playback control")
        self._enqueue({
            "action": action,
            "session_id": session_id,
        })
        return {"ok": True, "accepted": True, "action": action}


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "PiperReaderBridge/1.0"
    sys_version = ""

    @property
    def bridge(self):
        return self.server.bridge

    def setup(self):
        super().setup()
        self.connection.settimeout(3.0)

    def log_message(self, _format, *_args):
        return

    def _extension_origin(self):
        origin = self.headers.get("Origin", "")
        if origin in self.bridge.extension_origins:
            return origin
        return ""

    def _local_request(self):
        host = self.headers.get("Host", "")
        return (self.client_address[0] == "127.0.0.1" and
                host == f"127.0.0.1:{self.bridge.port}")

    def end_headers(self):
        origin = self._extension_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _authorized(self):
        if not self._local_request():
            return False
        origin = self.headers.get("Origin", "")
        if origin and origin not in self.bridge.extension_origins:
            return False
        supplied = self.headers.get("X-Piper-Token", "")
        return hmac.compare_digest(supplied, self.bridge.token)

    def _json_response(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._json_response(status, {"ok": False, "error": str(message)})

    def _read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not (0 < length <= MAX_REQUEST_BYTES):
            raise ValueError("request body is empty or too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON request must be an object")
        return value

    def do_OPTIONS(self):
        if not self._local_request() or not self._extension_origin():
            self._error(403, "browser extension origin required")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Piper-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._error(401, "unauthorized")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/v1/health":
            self._json_response(200, self.bridge.health())
            return
        if parsed.path == "/v1/events":
            query = urllib.parse.parse_qs(parsed.query)
            session_id = str(query.get("session_id", [""])[0])
            if not SESSION_RE.fullmatch(session_id):
                self._error(400, "invalid browser session id")
                return
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._error(400, "invalid event cursor")
                return
            self._json_response(
                200, self.bridge.events_since(session_id, after))
            return
        self._error(404, "unknown bridge endpoint")

    def do_POST(self):
        if not self._authorized():
            self._error(401, "unauthorized")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in {"/v1/read", "/v1/control"}:
            self._error(404, "unknown bridge endpoint")
            return
        try:
            payload = self._read_json()
            if parsed.path == "/v1/read":
                response = self.bridge.enqueue_read(payload)
            elif parsed.path == "/v1/control":
                response = self.bridge.enqueue_control(payload)
        except BridgeBusy as exc:
            self._error(503, str(exc))
            return
        except ValueError as exc:
            self._error(400, str(exc))
            return
        self._json_response(202, response)
