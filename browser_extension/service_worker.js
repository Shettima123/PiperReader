"use strict";

importScripts("bridge-config.js");

const BRIDGE = globalThis.PIPER_BRIDGE_CONFIG;
const HIGHLIGHT_CSS = "page-highlight.css";
const CONTENT_SCRIPT = "content.js";
const SESSION_RE = /^[A-Za-z0-9._:-]{1,100}$/;
const MAX_CHUNKS = 5000;
const MAX_TEXT_CHARS = 250000;
const MAX_CHUNK_CHARS = 280;

class BridgeError extends Error {
  constructor(message, code = "BRIDGE_ERROR", status = 0) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.status = status;
  }
}

function publicError(error) {
  if (error instanceof BridgeError) {
    return { ok: false, error: error.message, code: error.code, status: error.status };
  }
  return {
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    code: "EXTENSION_ERROR",
    status: 0
  };
}

async function bridgeFetch(path, { method = "GET", body = null, timeout = 5000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const headers = {
    "Accept": "application/json",
    "X-Piper-Token": BRIDGE.token
  };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
  }
  try {
    const response = await fetch(BRIDGE.baseUrl + path, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal: controller.signal
    });
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message = payload && typeof payload.error === "string"
        ? payload.error
        : `Piper Reader returned HTTP ${response.status}`;
      const code = response.status === 401 ? "UNAUTHORIZED"
        : response.status === 503 ? "BUSY"
        : "BRIDGE_HTTP_ERROR";
      throw new BridgeError(message, code, response.status);
    }
    if (!payload || payload.ok !== true) {
      throw new BridgeError("Piper Reader returned an invalid response.");
    }
    return payload;
  } catch (error) {
    if (error instanceof BridgeError) {
      throw error;
    }
    if (error && error.name === "AbortError") {
      throw new BridgeError("Piper Reader did not respond in time.", "TIMEOUT");
    }
    throw new BridgeError(
      "Piper Reader is not running. Start it from the Windows tray and try again.",
      "OFFLINE"
    );
  } finally {
    clearTimeout(timer);
  }
}

function requirePageSender(sender) {
  if (!sender.tab || !Number.isInteger(sender.tab.id) || sender.frameId !== 0) {
    throw new BridgeError("This request did not come from the active webpage.", "BAD_SENDER");
  }
  return sender.tab.id;
}

function validateSessionId(value) {
  const sessionId = String(value || "");
  if (!SESSION_RE.test(sessionId)) {
    throw new BridgeError("Invalid browser reading session.", "BAD_REQUEST");
  }
  return sessionId;
}

function validateChunks(rawChunks) {
  if (!Array.isArray(rawChunks) || rawChunks.length === 0 || rawChunks.length > MAX_CHUNKS) {
    throw new BridgeError("The webpage did not provide a valid reading list.", "BAD_REQUEST");
  }
  let total = 0;
  const ids = new Set();
  return rawChunks.map((raw, index) => {
    const id = String(raw && raw.id || `c${index}`).slice(0, 100);
    const text = String(raw && raw.text || "").trim();
    const sentenceId = raw && raw.sentence_id !== undefined
      ? Number(raw.sentence_id)
      : index;
    const paragraphId = raw && raw.paragraph_id !== undefined
      ? Number(raw.paragraph_id)
      : index;
    if (!id || ids.has(id) || !text || text.length > MAX_CHUNK_CHARS) {
      throw new BridgeError("The webpage provided an invalid reading chunk.", "BAD_REQUEST");
    }
    if (!Number.isSafeInteger(sentenceId) || sentenceId < 0 || sentenceId > MAX_CHUNKS ||
        !Number.isSafeInteger(paragraphId) || paragraphId < 0 || paragraphId > MAX_CHUNKS) {
      throw new BridgeError("The webpage provided invalid reading groups.", "BAD_REQUEST");
    }
    ids.add(id);
    total += text.length;
    if (total > MAX_TEXT_CHARS) {
      throw new BridgeError("The webpage is too long to read safely.", "BAD_REQUEST");
    }
    return {
      id,
      text,
      sentence_id: sentenceId,
      paragraph_id: paragraphId
    };
  });
}

async function handlePageMessage(message, sender) {
  requirePageSender(sender);
  const type = String(message && message.type || "");
  if (type === "PIPER_HEALTH") {
    return bridgeFetch("/v1/health");
  }
  if (type === "PIPER_READ") {
    const sessionId = validateSessionId(message.sessionId);
    const chunks = validateChunks(message.chunks);
    const startIndex = Number(message.startIndex);
    if (!Number.isInteger(startIndex) || startIndex < 0 || startIndex >= chunks.length) {
      throw new BridgeError("The requested reading position is invalid.", "BAD_REQUEST");
    }
    return bridgeFetch("/v1/read", {
      method: "POST",
      body: {
        session_id: sessionId,
        title: String(message.title || "Webpage").slice(0, 500),
        url: String(message.url || "").slice(0, 4096),
        chunks,
        start_index: startIndex
      }
    });
  }
  if (type === "PIPER_CONTROL") {
    const sessionId = validateSessionId(message.sessionId);
    const action = String(message.action || "").toLowerCase();
    if (!["pause", "resume", "toggle", "stop"].includes(action)) {
      throw new BridgeError("Invalid playback control.", "BAD_REQUEST");
    }
    return bridgeFetch("/v1/control", {
      method: "POST",
      body: { session_id: sessionId, action }
    });
  }
  if (type === "PIPER_EVENTS") {
    const sessionId = validateSessionId(message.sessionId);
    const after = Math.max(0, Number.parseInt(message.after, 10) || 0);
    const query = new URLSearchParams({ session_id: sessionId, after: String(after) });
    return bridgeFetch(`/v1/events?${query}`, { timeout: 4000 });
  }
  throw new BridgeError("Unknown Piper Reader extension request.", "BAD_REQUEST");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handlePageMessage(message, sender)
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse(publicError(error)));
  return true;
});

async function setActionError(tabId, message) {
  await chrome.action.setBadgeBackgroundColor({ tabId, color: "#d93f3f" });
  await chrome.action.setBadgeText({ tabId, text: "!" });
  await chrome.action.setTitle({ tabId, title: message });
  setTimeout(() => {
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
    chrome.action.setTitle({
      tabId,
      title: "Read this page with Piper Reader"
    }).catch(() => {});
  }, 5000);
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!Number.isInteger(tab.id)) {
    return;
  }
  const url = String(tab.url || "");
  if (url && !/^https?:\/\//i.test(url)) {
    await setActionError(
      tab.id,
      "Piper Reader can read normal http and https webpages only."
    );
    return;
  }
  try {
    const probe = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "ISOLATED",
      func: () => Boolean(globalThis.__PIPER_READER_PAGE_V1__)
    });
    const alreadyInjected = Boolean(probe && probe[0] && probe[0].result);
    if (!alreadyInjected) {
      await chrome.scripting.insertCSS({
        target: { tabId: tab.id },
        files: [HIGHLIGHT_CSS],
        origin: "USER"
      });
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "ISOLATED",
        files: [CONTENT_SCRIPT]
      });
    }
    await chrome.tabs.sendMessage(tab.id, {
      type: "PIPER_ACTIVATE",
      autoStart: true
    });
    await chrome.action.setBadgeText({ tabId: tab.id, text: "" });
  } catch (error) {
    await setActionError(
      tab.id,
      "Piper Reader cannot access this browser page."
    );
  }
});
