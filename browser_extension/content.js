(() => {
  "use strict";

  if (globalThis.__PIPER_READER_PAGE_V1__) {
    return;
  }

  const HIGHLIGHT_NAME = "piper-reader-current";
  const MAX_CHUNK_CHARS = 280;
  const MAX_CHUNKS = 5000;
  const MAX_TEXT_CHARS = 250000;
  const BLOCK_SELECTOR = "h1,h2,h3,h4,h5,h6,p,li,blockquote,dt,dd,figcaption";
  const EXCLUDED_ANCESTOR_SELECTOR = [
    "nav", "aside", "footer", "header", "form", "dialog",
    "[role='navigation']", "[role='complementary']", "[role='banner']",
    "[role='contentinfo']", "[aria-hidden='true']", "[hidden]",
    ".infobox", ".navbox", ".sidebar", ".toc", ".mw-jump-link",
    ".mw-editsection", ".reference", ".references", ".reflist",
    ".advertisement", ".ad-container", ".cookie-banner", ".cookie-consent",
    ".newsletter", ".paywall", ".modal"
  ].join(",");
  const EXCLUDED_TEXT_SELECTOR = [
    "script", "style", "noscript", "template", "svg", "canvas",
    "button", "input", "textarea", "select", "option", "label",
    "code", "pre", "kbd", "samp", "sup", "sub", "ruby", "rt",
    "[aria-hidden='true']", "[hidden]", ".mw-editsection", ".reference"
  ].join(",");

  const sleep = (milliseconds) => new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });

  function newSessionId() {
    const bytes = new Uint8Array(18);
    crypto.getRandomValues(bytes);
    return "web-" + Array.from(
      bytes,
      (value) => value.toString(16).padStart(2, "0")
    ).join("");
  }

  async function rpc(message) {
    const response = await chrome.runtime.sendMessage(message);
    if (!response || response.ok !== true) {
      const error = new Error(
        response && response.error
          ? response.error
          : "Piper Reader did not answer the browser extension."
      );
      error.code = response && response.code ? response.code : "EXTENSION_ERROR";
      throw error;
    }
    return response.result;
  }

  function cleanText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function isVisible(element) {
    if (!(element instanceof Element) || !element.isConnected) {
      return false;
    }
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number.parseFloat(style.opacity || "1") > 0 &&
      element.getClientRects().length > 0;
  }

  function linkDensity(element) {
    const all = cleanText(element.innerText).length;
    if (!all) {
      return 1;
    }
    let links = 0;
    for (const anchor of element.querySelectorAll("a")) {
      links += cleanText(anchor.innerText).length;
    }
    return Math.min(1, links / all);
  }

  function selectReadingRoot() {
    const selectors = [
      "article", "main", "[role='main']", "#content", "#main-content",
      "#mw-content-text", ".article-content", ".post-content", ".entry-content"
    ];
    const candidates = [];
    const seen = new Set();
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        if (!seen.has(element) && isVisible(element)) {
          seen.add(element);
          candidates.push(element);
        }
      }
    }
    if (document.body) {
      candidates.push(document.body);
    }

    let best = document.body || document.documentElement;
    let bestScore = -1;
    for (const candidate of candidates) {
      const textLength = Math.min(300000, cleanText(candidate.innerText).length);
      if (textLength < 80) {
        continue;
      }
      const density = linkDensity(candidate);
      const blocks = candidate.querySelectorAll("p,li,blockquote,h1,h2,h3").length;
      const semanticBonus = candidate.matches("article,main,[role='main']") ? 2500 : 0;
      const bodyPenalty = candidate === document.body ? 3000 : 0;
      const score = textLength * (1 - density * 0.82) +
        Math.min(blocks, 250) * 90 + semanticBonus - bodyPenalty;
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    return best;
  }

  function blockTextMap(block) {
    const characters = [];
    const references = [];
    let pendingSpace = null;
    const walker = document.createTreeWalker(
      block,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          const parent = node.parentElement;
          if (!parent || !node.data || !node.data.trim()) {
            return NodeFilter.FILTER_REJECT;
          }
          if (parent.closest(EXCLUDED_TEXT_SELECTOR) || !isVisible(parent)) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );

    let node;
    while ((node = walker.nextNode())) {
      const raw = node.data;
      for (let offset = 0; offset < raw.length; offset += 1) {
        const character = raw[offset];
        if (/\s/.test(character)) {
          if (characters.length && characters[characters.length - 1] !== " " && !pendingSpace) {
            pendingSpace = { node, start: offset, end: offset + 1 };
          }
          continue;
        }
        if (pendingSpace) {
          characters.push(" ");
          references.push(pendingSpace);
          pendingSpace = null;
        }
        characters.push(character);
        references.push({ node, start: offset, end: offset + 1 });
      }
    }
    return { text: characters.join(""), references };
  }

  function sentenceOffsets(text) {
    const output = [];
    try {
      const language = document.documentElement.lang || navigator.language || "en";
      const segmenter = new Intl.Segmenter(language, { granularity: "sentence" });
      for (const segment of segmenter.segment(text)) {
        output.push([segment.index, segment.index + segment.segment.length]);
      }
    } catch {
      const pattern = /[^.!?]+(?:[.!?]+(?=\s|$)|$)/g;
      let match;
      while ((match = pattern.exec(text))) {
        output.push([match.index, match.index + match[0].length]);
      }
    }
    return output.length ? output : [[0, text.length]];
  }

  function boundedOffsets(text, rawStart, rawEnd) {
    let start = rawStart;
    let end = rawEnd;
    while (start < end && /\s/.test(text[start])) {
      start += 1;
    }
    while (end > start && /\s/.test(text[end - 1])) {
      end -= 1;
    }
    const parts = [];
    while (end - start > MAX_CHUNK_CHARS) {
      const hardCut = start + MAX_CHUNK_CHARS;
      const softFloor = start + Math.floor(MAX_CHUNK_CHARS * 0.58);
      let cut = hardCut;
      for (let cursor = hardCut; cursor >= softFloor; cursor -= 1) {
        if (/\s/.test(text[cursor])) {
          cut = cursor;
          break;
        }
      }
      let trimmedEnd = cut;
      while (trimmedEnd > start && /\s/.test(text[trimmedEnd - 1])) {
        trimmedEnd -= 1;
      }
      if (trimmedEnd > start) {
        parts.push([start, trimmedEnd]);
      }
      start = cut;
      while (start < end && /\s/.test(text[start])) {
        start += 1;
      }
    }
    if (end > start) {
      parts.push([start, end]);
    }
    return parts;
  }

  function rangesForOffsets(references, start, end) {
    const ranges = [];
    let group = null;
    const finishGroup = () => {
      if (!group) {
        return;
      }
      try {
        const range = document.createRange();
        range.setStart(group.node, group.start);
        range.setEnd(group.node, group.end);
        if (!range.collapsed) {
          ranges.push(range);
        }
      } catch {
        // A live page may replace a text node during extraction.
      }
      group = null;
    };

    for (let index = start; index < end; index += 1) {
      const reference = references[index];
      if (!reference) {
        continue;
      }
      if (group && group.node === reference.node && reference.start <= group.end + 1) {
        group.end = Math.max(group.end, reference.end);
      } else {
        finishGroup();
        group = { ...reference };
      }
    }
    finishGroup();
    return ranges;
  }

  class PiperPageReader {
    constructor() {
      this.host = null;
      this.shadow = null;
      this.elements = {};
      this.chunks = [];
      this.byId = new Map();
      this.nodeHits = new WeakMap();
      this.sessionId = null;
      this.cursor = 0;
      this.pollGeneration = 0;
      this.phase = "idle";
      this.currentIndex = -1;
      this.startIndex = 0;
      this.speed = 1;
      this.repeatMode = "off";
      this.repeatIndex = 1;
      this.repeatCount = 1;
      this.controlsVisible = true;
      this.extractedUrl = "";
      this.toastTimer = null;
      this.altClickHandler = this.onAltClick.bind(this);
      document.addEventListener("click", this.altClickHandler, true);
    }

    mount() {
      if (this.host && this.host.isConnected) {
        return;
      }
      this.host = document.createElement("div");
      this.host.id = "piper-reader-extension-host";
      this.host.setAttribute("data-piper-reader-ui", "true");
      this.shadow = this.host.attachShadow({ mode: "open" });
      this.shadow.innerHTML = `
        <style>
          :host { all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; color-scheme: light; }
          * { box-sizing: border-box; }
          button { font: inherit; }
          .player, .dock, .bubble, .toast { font-family: "Segoe UI", system-ui, sans-serif; color: #14213d; }
          .player {
            position: fixed; top: 18px; left: 50%; transform: translateX(-50%);
            width: min(820px, calc(100vw - 44px)); min-height: 78px;
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 16px;
            padding: 12px 14px; border: 1px solid rgba(175, 193, 222, .78); border-radius: 24px;
            background: rgba(255, 255, 255, .96); box-shadow: 0 16px 50px rgba(28, 51, 92, .20), 0 2px 8px rgba(28, 51, 92, .08);
            backdrop-filter: blur(18px) saturate(1.15); pointer-events: auto;
          }
          .brand { display: flex; align-items: center; gap: 10px; min-width: 130px; }
          .logo { width: 36px; height: 36px; display: grid; place-items: center; border-radius: 12px; background: #eef3ff; color: #3159e8; font-size: 20px; font-weight: 800; }
          .brand-copy { min-width: 0; }
          .brand-name { font-size: 13px; line-height: 1.2; font-weight: 700; color: #172544; white-space: nowrap; }
          .page-title { margin-top: 3px; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: #7483a0; }
          .primary {
            width: 44px; height: 44px; flex: 0 0 auto; border: 0; border-radius: 50%; color: white; background: #3159e8;
            display: grid; place-items: center; cursor: pointer; font-size: 19px; font-weight: 800; box-shadow: 0 7px 18px rgba(49, 89, 232, .30);
          }
          .primary:hover { background: #294ed3; transform: translateY(-1px); }
          .primary:active { transform: translateY(0); }
          .middle { min-width: 0; }
          .meta { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 8px; }
          .status { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; color: #243453; }
          .counter { flex: 0 0 auto; font-size: 11px; color: #7686a3; white-space: nowrap; }
          .progress { width: 100%; height: 18px; padding: 7px 0; border: 0; background: transparent; cursor: pointer; display: block; }
          .track { position: relative; width: 100%; height: 4px; overflow: hidden; border-radius: 999px; background: #dce4f2; }
          .fill { position: absolute; inset: 0 auto 0 0; width: 0%; border-radius: inherit; background: linear-gradient(90deg, #4773ff, #3159e8); transition: width .2s ease; }
          .actions { display: flex; align-items: center; gap: 5px; }
          .icon-button, .dock-button, .bubble {
            border: 1px solid #d9e2f0; background: rgba(255,255,255,.98); color: #29416d; cursor: pointer;
          }
          .icon-button { width: 36px; height: 36px; border-radius: 11px; display: grid; place-items: center; font-size: 18px; }
          .icon-button:hover, .dock-button:hover, .bubble:hover { background: #eef3ff; color: #3159e8; border-color: #c9d7ee; }
          .stop { font-size: 14px; }
          .dock {
            position: fixed; right: 20px; top: 42%; transform: translateY(-50%); display: flex; flex-direction: column; gap: 6px;
            padding: 7px; border: 1px solid rgba(181, 197, 222, .75); border-radius: 18px; background: rgba(255,255,255,.96);
            box-shadow: 0 12px 34px rgba(28, 51, 92, .18); backdrop-filter: blur(15px); pointer-events: auto;
          }
          .dock-button { width: 38px; height: 38px; border-radius: 12px; display: grid; place-items: center; font-size: 17px; }
          .dock-separator { height: 1px; margin: 1px 5px; background: #e3e9f3; }
          .bubble {
            position: fixed; display: none; width: 38px; height: 38px; border-radius: 50%; place-items: center; z-index: 2;
            font-size: 15px; font-weight: 800; box-shadow: 0 8px 22px rgba(28, 51, 92, .20); pointer-events: auto;
          }
          .bubble.visible { display: grid; }
          .toast {
            position: fixed; left: 50%; top: 108px; transform: translateX(-50%) translateY(-8px); max-width: min(560px, calc(100vw - 40px));
            padding: 10px 15px; border-radius: 13px; background: #172544; color: white; box-shadow: 0 10px 28px rgba(22, 37, 68, .24);
            font-size: 12px; line-height: 1.35; opacity: 0; transition: opacity .18s ease, transform .18s ease; pointer-events: none;
          }
          .toast.visible { opacity: 1; transform: translateX(-50%) translateY(0); }
          .toast.error { background: #a92843; }
          :host(.controls-hidden) .player, :host(.controls-hidden) .dock { display: none; }
          :host(.controls-hidden) .toast { top: 18px; }
          button:focus-visible { outline: 3px solid rgba(61, 103, 242, .35); outline-offset: 2px; }
          @media (max-width: 680px) {
            .player { width: calc(100vw - 22px); top: 10px; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; border-radius: 19px; }
            .brand { min-width: 0; } .brand-copy { display: none; } .logo { width: 34px; height: 34px; }
            .actions .restart-top { display: none; } .dock { right: 10px; }
          }
          @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; transition: none !important; } }
        </style>
        <section class="player" aria-label="Piper Reader webpage player">
          <div class="brand">
            <button class="primary" id="primary" type="button" aria-label="Start reading">▶</button>
            <div class="brand-copy"><div class="brand-name">Piper Reader</div><div class="page-title" id="pageTitle"></div></div>
          </div>
          <div class="middle">
            <div class="meta"><span class="status" id="status">Ready to read</span><span class="counter" id="counter">0 of 0</span></div>
            <button class="progress" id="progress" type="button" aria-label="Reading progress; click to start at another sentence"><span class="track"><span class="fill" id="fill"></span></span></button>
          </div>
          <div class="actions">
            <button class="icon-button restart-top" id="restart" type="button" title="Read from the beginning" aria-label="Read from the beginning">↺</button>
            <button class="icon-button stop" id="stop" type="button" title="Stop reading" aria-label="Stop reading">■</button>
            <button class="icon-button" id="hide" type="button" title="Hide controls" aria-label="Hide controls">×</button>
          </div>
        </section>
        <nav class="dock" aria-label="Piper Reader quick controls">
          <button class="dock-button" id="dockToggle" type="button" title="Play or pause" aria-label="Play or pause">▶</button>
          <button class="dock-button" id="locate" type="button" title="Show the sentence being read" aria-label="Show the sentence being read">⌖</button>
          <button class="dock-button" id="dockRestart" type="button" title="Read from the beginning" aria-label="Read from the beginning">↺</button>
          <div class="dock-separator"></div>
          <button class="dock-button stop" id="dockStop" type="button" title="Stop reading" aria-label="Stop reading">■</button>
        </nav>
        <button class="bubble" id="bubble" type="button" aria-label="Pause reading">Ⅱ</button>
        <div class="toast" id="toast" role="status" aria-live="polite"></div>
      `;
      (document.documentElement || document.body).appendChild(this.host);
      for (const id of [
        "primary", "pageTitle", "status", "counter", "progress", "fill",
        "restart", "stop", "hide", "dockToggle", "locate", "dockRestart",
        "dockStop", "bubble", "toast"
      ]) {
        this.elements[id] = this.shadow.getElementById(id);
      }
      this.elements.primary.addEventListener("click", () => this.togglePlayback());
      this.elements.dockToggle.addEventListener("click", () => this.togglePlayback());
      this.elements.bubble.addEventListener("click", () => this.togglePlayback());
      this.elements.restart.addEventListener("click", () => this.startFrom(0, false));
      this.elements.dockRestart.addEventListener("click", () => this.startFrom(0, false));
      this.elements.stop.addEventListener("click", () => this.stop());
      this.elements.dockStop.addEventListener("click", () => this.stop());
      this.elements.hide.addEventListener("click", () => this.hideControls());
      this.elements.locate.addEventListener("click", () => this.scrollToCurrent(true));
      this.elements.progress.addEventListener("click", (event) => this.seekFromProgress(event));
      this.elements.pageTitle.textContent = document.title || location.hostname;
      this.updateUI();
    }

    showControls() {
      this.mount();
      this.controlsVisible = true;
      this.host.classList.remove("controls-hidden");
    }

    hideControls() {
      this.controlsVisible = false;
      this.host.classList.add("controls-hidden");
      this.showToast("Controls hidden. Click the Piper Reader extension icon to show them again.");
    }

    async activate({ autoStart = true } = {}) {
      this.showControls();
      const pageChanged = Boolean(
        this.extractedUrl && this.extractedUrl !== location.href);
      const rangesStillConnected = this.chunks.some((chunk) =>
        chunk.ranges.some((range) => range.startContainer && range.startContainer.isConnected)
      );
      if (autoStart && (!this.sessionId || pageChanged || !rangesStillConnected)) {
        await this.startFrom(0, pageChanged || !rangesStillConnected || !this.chunks.length);
      } else {
        this.showToast("Alt+click any sentence to start reading there.");
      }
    }

    extractPage() {
      const root = selectReadingRoot();
      const chunks = [];
      const byId = new Map();
      const nodeHits = new WeakMap();
      let totalCharacters = 0;
      let paragraphId = 0;
      let sentenceId = 0;
      const blocks = root.querySelectorAll(BLOCK_SELECTOR);

      for (const block of blocks) {
        if (chunks.length >= MAX_CHUNKS || totalCharacters >= MAX_TEXT_CHARS) {
          break;
        }
        if (!isVisible(block) || block.closest(EXCLUDED_ANCESTOR_SELECTOR)) {
          continue;
        }
        if (block.querySelector(BLOCK_SELECTOR)) {
          continue;
        }
        const plain = cleanText(block.innerText);
        if (!plain || (plain.length > 40 && linkDensity(block) > 0.72)) {
          continue;
        }
        const mapped = blockTextMap(block);
        if (!mapped.text) {
          continue;
        }
        const currentParagraphId = paragraphId;
        paragraphId += 1;
        for (const [sentenceStart, sentenceEnd] of sentenceOffsets(mapped.text)) {
          const currentSentenceId = sentenceId;
          sentenceId += 1;
          for (const [start, end] of boundedOffsets(mapped.text, sentenceStart, sentenceEnd)) {
            const text = mapped.text.slice(start, end).trim();
            if (!text || text.length > MAX_CHUNK_CHARS || totalCharacters + text.length > MAX_TEXT_CHARS) {
              continue;
            }
            const ranges = rangesForOffsets(mapped.references, start, end);
            if (!ranges.length) {
              continue;
            }
            const index = chunks.length;
            const chunk = {
              id: `c${index}`,
              text,
              ranges,
              index,
              sentence_id: currentSentenceId,
              paragraph_id: currentParagraphId
            };
            chunks.push(chunk);
            byId.set(chunk.id, chunk);
            totalCharacters += text.length;
            for (const range of ranges) {
              if (range.startContainer !== range.endContainer ||
                  range.startContainer.nodeType !== Node.TEXT_NODE) {
                continue;
              }
              const node = range.startContainer;
              const hits = nodeHits.get(node) || [];
              hits.push({
                start: range.startOffset,
                end: range.endOffset,
                index
              });
              nodeHits.set(node, hits);
            }
            if (chunks.length >= MAX_CHUNKS) {
              break;
            }
          }
          if (chunks.length >= MAX_CHUNKS) {
            break;
          }
        }
      }

      if (!chunks.length) {
        throw new Error("I could not find readable article text on this page.");
      }
      this.chunks = chunks;
      this.byId = byId;
      this.nodeHits = nodeHits;
      this.extractedUrl = location.href;
      this.currentIndex = -1;
      this.startIndex = 0;
      this.elements.pageTitle.textContent = document.title || location.hostname;
      this.updateUI();
    }

    async startFrom(index = 0, refresh = false) {
      this.mount();
      this.showControls();
      const generation = ++this.pollGeneration;
      this.sessionId = null;
      this.cursor = 0;
      this.phase = "preparing";
      this.repeatMode = "off";
      this.repeatIndex = 1;
      this.repeatCount = 1;
      this.clearHighlight();
      this.updateUI();
      try {
        const health = await rpc({ type: "PIPER_HEALTH" });
        this.speed = Number(health.speed) || 1;
        if (refresh || !this.chunks.length) {
          this.setStatus("Finding the readable text…");
          this.extractPage();
        }
        const safeIndex = Math.max(0, Math.min(this.chunks.length - 1, Number(index) || 0));
        const sessionId = newSessionId();
        this.sessionId = sessionId;
        this.startIndex = safeIndex;
        this.currentIndex = safeIndex - 1;
        this.phase = "preparing";
        this.setStatus("Preparing local voice…");
        this.updateUI();
        await rpc({
          type: "PIPER_READ",
          sessionId,
          title: document.title || location.hostname,
          url: location.href,
          chunks: this.chunks.map(({ id, text, sentence_id, paragraph_id }) => ({
            id, text, sentence_id, paragraph_id
          })),
          startIndex: safeIndex
        });
        if (generation !== this.pollGeneration || sessionId !== this.sessionId) {
          return;
        }
        this.phase = "playing";
        this.updateUI();
        this.showToast("Reading locally. Alt+click any sentence to jump there.");
        this.poll(sessionId, generation);
      } catch (error) {
        if (generation !== this.pollGeneration) {
          return;
        }
        this.sessionId = null;
        this.phase = "error";
        this.setStatus(error.message || "Could not start reading.");
        this.updateUI();
        this.showToast(error.message || "Could not start reading.", true);
      }
    }

    async poll(sessionId, generation) {
      let consecutiveFailures = 0;
      while (generation === this.pollGeneration && sessionId === this.sessionId) {
        try {
          const result = await rpc({
            type: "PIPER_EVENTS",
            sessionId,
            after: this.cursor
          });
          if (generation !== this.pollGeneration || sessionId !== this.sessionId) {
            return;
          }
          consecutiveFailures = 0;
          if (result.reset) {
            this.cursor = 0;
          }
          for (const event of result.events || []) {
            if (event.seq <= this.cursor) {
              continue;
            }
            this.cursor = event.seq;
            this.applyEvent(event, sessionId);
            if (sessionId !== this.sessionId) {
              return;
            }
          }
          this.cursor = Math.max(this.cursor, Number(result.cursor || result.latest) || 0);
        } catch (error) {
          consecutiveFailures += 1;
          if (consecutiveFailures >= 3) {
            this.showToast(error.message || "Lost contact with Piper Reader.", true);
          }
        }
        await sleep(consecutiveFailures ? 700 : 250);
      }
    }

    applyEvent(event, sessionId) {
      switch (event.type) {
        case "session_started":
          this.phase = "playing";
          this.updateUI();
          break;
        case "highlight":
          this.phase = "playing";
          this.currentIndex = Number.isInteger(event.index)
            ? event.index
            : this.currentIndex;
          this.repeatMode = String(event.repeat_mode || "off");
          this.repeatIndex = Number(event.repeat_index) || 1;
          this.repeatCount = Number(event.repeat_count) || 1;
          this.focusChunk(event.chunk_id);
          this.updateUI();
          break;
        case "state":
          if (event.state === "paused" || event.state === "playing") {
            this.phase = event.state;
          }
          this.updateUI();
          break;
        case "complete":
          this.currentIndex = this.chunks.length - 1;
          this.endSession(sessionId, "complete", "Finished reading this page.");
          break;
        case "stopped":
          this.endSession(
            sessionId,
            "stopped",
            event.reason === "superseded" ? "Reading moved to another page." : "Reading stopped."
          );
          break;
        case "error":
          this.endSession(sessionId, "error", event.message || "Reading failed.");
          this.showToast(event.message || "Reading failed.", true);
          break;
        default:
          break;
      }
    }

    endSession(sessionId, phase, message) {
      if (sessionId !== this.sessionId) {
        return;
      }
      this.sessionId = null;
      this.pollGeneration += 1;
      this.phase = phase;
      this.clearHighlight();
      this.setStatus(message);
      this.updateUI();
    }

    async control(action) {
      const sessionId = this.sessionId;
      if (!sessionId) {
        if (action === "resume" || action === "toggle") {
          await this.startFrom(Math.max(0, this.currentIndex), !this.chunks.length);
        }
        return;
      }
      try {
        await rpc({
          type: "PIPER_CONTROL",
          sessionId,
          action
        });
      } catch (error) {
        this.showToast(error.message || "The reading control failed.", true);
      }
    }

    togglePlayback() {
      if (!this.sessionId) {
        this.startFrom(Math.max(0, this.currentIndex), !this.chunks.length);
      } else if (this.phase === "paused") {
        this.control("resume");
      } else {
        this.control("pause");
      }
    }

    async stop() {
      const sessionId = this.sessionId;
      if (!sessionId) {
        this.phase = "stopped";
        this.setStatus("Reading stopped.");
        this.clearHighlight();
        this.updateUI();
        return;
      }
      await this.control("stop");
    }

    seekFromProgress(event) {
      if (!this.chunks.length) {
        return;
      }
      const bounds = this.elements.progress.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(0.999999, (event.clientX - bounds.left) / bounds.width));
      this.startFrom(Math.floor(ratio * this.chunks.length), false);
    }

    focusChunk(chunkId) {
      const chunk = this.byId.get(chunkId);
      if (!chunk) {
        return;
      }
      const ranges = chunk.ranges.filter((range) => range.startContainer && range.startContainer.isConnected);
      if (!ranges.length) {
        this.showToast("This page changed, so I could not highlight the current sentence.", true);
        return;
      }
      if (globalThis.CSS && CSS.highlights && globalThis.Highlight) {
        CSS.highlights.set(HIGHLIGHT_NAME, new Highlight(...ranges));
      }
      this.positionBubble(ranges);
      this.scrollToRanges(ranges, false);
    }

    clearHighlight() {
      if (globalThis.CSS && CSS.highlights) {
        CSS.highlights.delete(HIGHLIGHT_NAME);
      }
      if (this.elements.bubble) {
        this.elements.bubble.classList.remove("visible");
      }
    }

    positionBubble(ranges) {
      const rects = [];
      for (const range of ranges) {
        rects.push(...range.getClientRects());
      }
      const rect = rects.find((item) => item.width > 0 && item.height > 0);
      if (!rect) {
        this.elements.bubble.classList.remove("visible");
        return;
      }
      const size = 38;
      const left = Math.max(8, Math.min(innerWidth - size - 8, rect.left - size - 10));
      const top = Math.max(8, Math.min(innerHeight - size - 8, rect.top + rect.height / 2 - size / 2));
      this.elements.bubble.style.left = `${left}px`;
      this.elements.bubble.style.top = `${top}px`;
      this.elements.bubble.classList.add("visible");
    }

    scrollToRanges(ranges, force) {
      const first = ranges[0];
      const rect = first.getBoundingClientRect();
      const topSafe = 120;
      const visible = rect.top >= topSafe && rect.bottom <= innerHeight - 70;
      if (!force && visible) {
        return;
      }
      const element = first.startContainer.parentElement;
      if (element) {
        element.scrollIntoView({
          behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
          block: "center",
          inline: "nearest"
        });
        setTimeout(() => this.positionBubble(ranges), 350);
      }
    }

    scrollToCurrent(force) {
      const chunk = this.chunks[this.currentIndex];
      if (chunk) {
        const ranges = chunk.ranges.filter((range) => range.startContainer && range.startContainer.isConnected);
        if (ranges.length) {
          this.scrollToRanges(ranges, force);
          this.positionBubble(ranges);
        }
      }
    }

    onAltClick(event) {
      if (!event.altKey || !this.chunks.length ||
          (this.host && event.composedPath().includes(this.host))) {
        return;
      }
      let node = null;
      let offset = 0;
      if (document.caretPositionFromPoint) {
        const position = document.caretPositionFromPoint(event.clientX, event.clientY);
        if (position) {
          node = position.offsetNode;
          offset = position.offset;
        }
      } else if (document.caretRangeFromPoint) {
        const range = document.caretRangeFromPoint(event.clientX, event.clientY);
        if (range) {
          node = range.startContainer;
          offset = range.startOffset;
        }
      }
      if (node && node.nodeType !== Node.TEXT_NODE) {
        node = node.childNodes[offset] || node.firstChild;
        offset = 0;
      }
      if (!node || node.nodeType !== Node.TEXT_NODE) {
        return;
      }
      const hits = this.nodeHits.get(node) || [];
      let hit = hits.find((candidate) => offset >= candidate.start && offset <= candidate.end);
      if (!hit && hits.length) {
        hit = hits.reduce((best, candidate) => {
          const distance = Math.min(
            Math.abs(offset - candidate.start),
            Math.abs(offset - candidate.end)
          );
          return !best || distance < best.distance ? { ...candidate, distance } : best;
        }, null);
      }
      if (!hit) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      this.startFrom(hit.index, false);
    }

    remainingLabel() {
      if (!this.chunks.length) {
        return "0 of 0";
      }
      if (this.phase === "complete") {
        return `${this.chunks.length} of ${this.chunks.length} · finished`;
      }
      const displayIndex = Math.max(this.startIndex, this.currentIndex);
      let words = 0;
      for (let index = Math.max(0, displayIndex + 1); index < this.chunks.length; index += 1) {
        words += this.chunks[index].text.split(/\s+/).filter(Boolean).length;
      }
      const minutes = Math.max(1, Math.ceil(words / (180 * Math.max(.5, this.speed))));
      const position = Math.min(this.chunks.length, Math.max(0, displayIndex + 1));
      const repeating = this.repeatCount > 1
        ? ` · repeat ${this.repeatIndex}/${this.repeatCount}`
        : "";
      return `${position} of ${this.chunks.length}${repeating} · ~${minutes} min left`;
    }

    setStatus(message) {
      if (this.elements.status) {
        this.elements.status.textContent = message;
      }
    }

    updateUI() {
      if (!this.host) {
        return;
      }
      const paused = this.phase === "paused";
      const active = this.sessionId && ["preparing", "playing", "paused"].includes(this.phase);
      const symbol = active ? (paused ? "▶" : "Ⅱ") : "▶";
      const label = active ? (paused ? "Resume reading" : "Pause reading") : "Start reading";
      this.elements.primary.textContent = symbol;
      this.elements.primary.setAttribute("aria-label", label);
      this.elements.dockToggle.textContent = symbol;
      this.elements.dockToggle.setAttribute("aria-label", label);
      this.elements.bubble.textContent = symbol;
      this.elements.bubble.setAttribute("aria-label", label);
      if (this.phase === "preparing") {
        this.setStatus("Preparing local voice…");
      } else if (this.phase === "playing") {
        const repeating = this.repeatCount > 1
          ? ` · repeat ${this.repeatIndex}/${this.repeatCount}`
          : "";
        this.setStatus(`Reading this page${repeating}`);
      } else if (this.phase === "paused") {
        this.setStatus("Paused");
      } else if (this.phase === "idle") {
        this.setStatus("Ready to read");
      }
      this.elements.counter.textContent = this.remainingLabel();
      const progress = this.chunks.length
        ? Math.max(0, Math.min(1, (this.currentIndex + 1) / this.chunks.length))
        : 0;
      this.elements.fill.style.width = `${(progress * 100).toFixed(2)}%`;
      this.elements.progress.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
      this.elements.progress.setAttribute("aria-valuemin", "0");
      this.elements.progress.setAttribute("aria-valuemax", "100");
      if (this.elements.bubble.classList.contains("visible")) {
        this.elements.bubble.style.opacity = this.phase === "paused" || this.phase === "playing" ? "1" : "0";
      }
    }

    showToast(message, isError = false) {
      this.mount();
      clearTimeout(this.toastTimer);
      this.elements.toast.textContent = message;
      this.elements.toast.classList.toggle("error", Boolean(isError));
      this.elements.toast.classList.add("visible");
      this.toastTimer = setTimeout(() => {
        this.elements.toast.classList.remove("visible");
      }, isError ? 6000 : 3500);
    }
  }

  const controller = new PiperPageReader();
  globalThis.__PIPER_READER_PAGE_V1__ = controller;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "PIPER_ACTIVATE") {
      return false;
    }
    controller.activate(message)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => {
        controller.showToast(error.message || "Could not open Piper Reader.", true);
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  });
})();
