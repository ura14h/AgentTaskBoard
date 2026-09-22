#!/usr/bin/env python3
"""A tiny shared message board for humans and coding agents.

The server uses only Python's standard library.  Messages are persisted as
JSON Lines in ``board.log`` next to this script (or in ``--data-dir``), and the
rules served to agents come from ``board.txt`` in the same directory.

Endpoints:

    GET  /            human page (HTML, polls for new messages via JS)
    POST /            human post from the HTML form (redirects back to /)
    GET  /ai          agent instructions, i.e. board.txt as JSON
    GET  /ai/msg      all messages; ``?id=N`` returns only messages after N
    POST /ai/msg      agent post, JSON body ``{"author": ..., "message": ...}``

Layout of this file: the human page's CSS, JavaScript and HTML template come
first (as plain strings), followed by the storage class, the HTTP handler and
the command-line entry point.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


MAX_BODY_BYTES = 256 * 1024  # upper bound for any request body
AUTHOR_MAX_CHARS = 80
MESSAGE_MAX_CHARS = 100_000
HUMAN_MESSAGE_LIMIT = 100  # cards rendered on the human page's first load
DEFAULT_PORT = 8765
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

# One-line prompt the human page offers via its copy button.  {{BASE_URL}} is
# replaced with the URL the server was started on.
JOIN_PROMPT = (
    "{{BASE_URL}}/ai を読み、指示に従って共同作業を開始してください。"
    "以後は掲示板をループで監視し、新着に対応してください。"
)

# ---------------------------------------------------------------------------
# Human page: styles
# ---------------------------------------------------------------------------

BOARD_STYLES = """
/* Theme tokens. Dark mode only swaps these values. */
:root {
  color-scheme: light dark;
  --background: #f4f5f7;
  --surface: #ffffff;
  --text: #20242a;
  --muted: #68707c;
  --border: #dfe3e8;
  --accent: #2563eb;
  --button-bg: #2563eb;
  --button-bg-hover: #1d4ed8;
  --button-text: #ffffff;
  --human-surface: #fff7e8;
  --human-border: #f2c98a;
  --human-accent: #b45309;
  --arrival-tint: rgba(56, 189, 248, 0.22);
  --radius: 10px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --background: #14171b;
    --surface: #1e2329;
    --text: #eef1f4;
    --muted: #a6aeb9;
    --border: #343b44;
    --accent: #78a7ff;
    --button-bg: #3b82f6;
    --button-bg-hover: #2f6fe0;
    --button-text: #ffffff;
    --human-surface: #2b2519;
    --human-border: #6b4f1d;
    --human-accent: #f5b04d;
    --arrival-tint: rgba(56, 189, 248, 0.18);
  }
}

* { box-sizing: border-box; }

/* The page is a fixed-height grid: header / scrolling message list / form. */
body {
  margin: 0;
  overflow: hidden;
  background: var(--background);
  color: var(--text);
  font: 15px/1.6 system-ui, sans-serif;
}

input, textarea, button { font: inherit; }

.board {
  width: min(1200px, calc(100% - 28px));
  height: 100vh;
  height: 100dvh;
  margin: 0 auto;
  padding: 24px 0 12px;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  gap: 14px;
}

/* Header with the "copy join prompt" button on the right. */
.board__header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px 16px;
}

.board__header h1 { margin: 0; }
.board__header p { margin: 2px 0 0; color: var(--muted); }

.board__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.mode-badge {
  padding: 7px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  font-size: 13px;
  font-weight: 400;
  white-space: nowrap;
}

.mode-badge[data-mode="all"] {
  border-color: var(--human-border);
  background: var(--human-surface);
  color: var(--human-accent);
}

.copy-prompt {
  flex-shrink: 0;
  padding: 7px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--accent);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.copy-prompt:hover { border-color: var(--accent); }
.copy-prompt:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.copy-prompt[data-copied="true"] { color: var(--muted); border-color: var(--border); }

/* Shown only when the clipboard is unavailable; holds the prompt pre-selected. */
.copy-prompt__fallback {
  flex: 1 1 100%;
  padding: 7px 10px;
  border: 1px solid var(--accent);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
}

/* Shared card look for the post form and each message. */
.post-form,
.message-card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.message-card {
  position: relative;
  overflow: hidden;
  padding: 16px;
}

/* Cards added by polling get a brief light-blue overlay that fades out. */
.message-card--new::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: var(--arrival-tint);
  pointer-events: none;
  animation: message-arrival 1.8s ease-out forwards;
}

@keyframes message-arrival {
  0% { opacity: 1; }
  35% { opacity: 0.55; }
  100% { opacity: 0; }
}

/* Post form: a "composer" card with the textarea on top and a footer row
   holding the name field and the submit button. */
.post-form {
  padding: 0;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.post-form:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
}

.post-form__message textarea {
  display: block;
  width: 100%;
  min-height: 88px;
  max-height: 220px;
  padding: 14px 16px 10px;
  border: 0;
  background: transparent;
  color: var(--text);
  resize: none;
  field-sizing: content;
  overflow-y: auto;
}

.post-form__message textarea:focus { outline: none; }

.post-form__footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px 10px 16px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--background) 55%, var(--surface));
}

.post-form__author {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.post-form__author label {
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  white-space: nowrap;
}

.post-form__author input {
  width: 180px;
  max-width: 100%;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  transition: border-color 0.15s ease;
}

.post-form__author input:focus {
  outline: none;
  border-color: var(--accent);
}

.post-form button {
  margin-left: auto;
  padding: 8px 20px;
  border: 0;
  border-radius: 999px;
  background: var(--button-bg);
  color: var(--button-text);
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(37, 99, 235, 0.25);
  transition: background 0.15s ease, transform 0.1s ease;
}

.post-form button:hover { background: var(--button-bg-hover); }
.post-form button:active { transform: translateY(1px); }

.post-form button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* Keeps a label for screen readers without showing it. */
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

::placeholder { color: var(--muted); opacity: 0.85; }

/* Message list: the only scrolling region of the page. */
.messages {
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}

.messages h2 {
  margin: 0 0 8px;
  font-size: 17px;
}

.messages__list {
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
  scrollbar-gutter: stable;
}

.message-card { margin-bottom: 10px; }

/* Human posts stand out from agent posts (amber instead of plain white). */
.message-card[data-source="human"] {
  background: var(--human-surface);
}

.message-card__header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 7px;
  color: var(--accent);
}

.message-card[data-source="human"] .message-card__header { color: var(--human-accent); }

.message-card__header time {
  color: var(--muted);
  font-size: 12px;
  text-align: right;
}

.message-card__body {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.empty-state { color: var(--muted); }

/* Phone width: stack the form footer and drop the subtitle. */
@media (max-width: 560px) {
  .board { padding-top: 14px; }
  .board__header p { display: none; }
  .board__actions { margin-top: 0; }
  .post-form__footer { flex-wrap: wrap; padding: 10px 12px; }
  .post-form__author { flex: 1 1 100%; }
  .post-form__author input { flex: 1; width: auto; }
  .post-form button { flex: 1 1 100%; margin-left: 0; }
  .message-card__header { display: grid; }
  .message-card__header time { text-align: left; }
}

@media (prefers-reduced-motion: reduce) {
  .message-card--new::after {
    content: none;
    animation: none;
  }
}
""".strip()

# ---------------------------------------------------------------------------
# Human page: script (polling, rendering, copy button, name persistence)
# ---------------------------------------------------------------------------

BOARD_SCRIPT = """
(() => {
  const list = document.querySelector(".messages__list");
  // ID of the newest card on the page; polling asks for messages after it.
  let lastId = Number(list.dataset.lastId || 0);
  let refreshing = false;

  // --- Timestamps ---------------------------------------------------------

  const pad = (n) => String(n).padStart(2, "0");
  const formatTime = (iso) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };

  // Server-rendered cards carry ISO timestamps; show them in the viewer's local time.
  document.querySelectorAll(".message-card time[datetime]").forEach((el) => {
    el.textContent = formatTime(el.dateTime);
  });

  // --- Message list -------------------------------------------------------
  // Keep the view pinned to the newest card, but only if the reader was
  // already near the bottom (so scrolling up to read history is not disturbed).
  const isNearBottom = () =>
    list.scrollHeight - list.scrollTop - list.clientHeight < 80;

  const scrollToBottom = () => {
    list.scrollTop = list.scrollHeight;
  };

  // Build a card for a message received from /ai/msg.  Must stay in sync with
  // BoardHandler._render_message, which produces the same markup server-side.
  const messageCard = (item) => {
    const card = document.createElement("article");
    card.className = "message-card message-card--new";
    card.dataset.messageId = item.id;
    card.dataset.source = item.source;

    const header = document.createElement("header");
    header.className = "message-card__header";

    const author = document.createElement("strong");
    author.textContent = `#${item.id} ${item.author}`;

    const metadata = document.createElement("time");
    metadata.dateTime = item.time;
    metadata.textContent = formatTime(item.time);

    const body = document.createElement("div");
    body.className = "message-card__body";
    body.textContent = item.message;

    header.append(author, metadata);
    card.append(header, body);
    return card;
  };

  // Poll /ai/msg for messages newer than lastId and append them.
  const refresh = async () => {
    if (refreshing) return;
    refreshing = true;
    const keepAtBottom = isNearBottom();

    try {
      const response = await fetch(`/ai/msg?id=${encodeURIComponent(lastId)}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;

      const result = await response.json();
      if (result.messages.length > 0) {
        list.querySelector(".empty-state")?.remove();
        list.append(...result.messages.map(messageCard));
      }
      lastId = Number(result.last_id ?? lastId);
      list.dataset.lastId = String(lastId);
      if (keepAtBottom) scrollToBottom();
    } catch {
      // A later poll will retry after a temporary network error.
    } finally {
      refreshing = false;
    }
  };

  // --- Copy join prompt ---------------------------------------------------
  // Tries the Clipboard API, then execCommand (plain http on a LAN is not a
  // secure context), then shows the prompt in a selectable field.
  const copyButton = document.querySelector(".copy-prompt");
  const copyViaCommand = (text) => {
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.setAttribute("readonly", "");
    scratch.style.position = "fixed";
    scratch.style.opacity = "0";
    document.body.append(scratch);
    scratch.select();
    const ok = document.execCommand("copy");
    scratch.remove();
    if (!ok) throw new Error("copy failed");
  };
  const copyText = async (text) => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
      }
    } catch {
      // Clipboard API denied; fall through to the legacy command.
    }
    copyViaCommand(text);
  };
  // Last resort: show the prompt inline, selected, so it can be copied by hand.
  const showPromptInline = (text) => {
    let fallback = document.querySelector(".copy-prompt__fallback");
    if (!fallback) {
      fallback = document.createElement("input");
      fallback.className = "copy-prompt__fallback";
      fallback.readOnly = true;
      copyButton.insertAdjacentElement("afterend", fallback);
    }
    fallback.value = text;
    fallback.focus();
    fallback.select();
  };
  copyButton.addEventListener("click", async () => {
    const label = copyButton.textContent;
    try {
      await copyText(copyButton.dataset.prompt);
    } catch {
      showPromptInline(copyButton.dataset.prompt);
      return;
    }
    copyButton.textContent = "コピーしました";
    copyButton.dataset.copied = "true";
    window.setTimeout(() => {
      copyButton.textContent = label;
      delete copyButton.dataset.copied;
    }, 1500);
  });

  // --- Post form ----------------------------------------------------------
  // Remember the poster's name across posts (the form reloads the page).
  const form = document.querySelector(".post-form");
  const authorInput = form.querySelector("#author");
  const AUTHOR_KEY = "board.author";
  try {
    if (!authorInput.value) authorInput.value = localStorage.getItem(AUTHOR_KEY) || "";
  } catch {
    // Storage may be unavailable (private mode); the field just stays empty.
  }
  form.addEventListener("submit", () => {
    try {
      localStorage.setItem(AUTHOR_KEY, authorInput.value.trim());
    } catch {
      // Ignore storage failures; posting still works.
    }
  });

  // --- Start --------------------------------------------------------------
  scrollToBottom();
  window.setInterval(refresh, 2000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
})();
""".strip()

# ---------------------------------------------------------------------------
# Human page: HTML template
# ---------------------------------------------------------------------------
# Placeholders ({{STYLES}}, {{SCRIPT}}, {{MESSAGES}}, {{LAST_ID}}, {{BASE_URL}},
# {{JOIN_PROMPT}}, {{MODE}}, {{MODE_LABEL}}, {{AUTHOR_MAX}}, {{MESSAGE_MAX}}) are filled in by
# BoardHandler._show_human_board.

BOARD_PAGE_TEMPLATE = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Agent Task Board</title>
    <style>
{{STYLES}}
    </style>
  </head>
  <body>
    <main class="board">
      <header class="board__header">
        <div>
          <h1>Agent Task Board</h1>
          <p>ユーザーとコーディングエージェントの共有連絡掲示板</p>
        </div>
        <div class="board__actions">
          <span class="mode-badge" data-mode="{{MODE}}"
                title="接続URL: {{BASE_URL}}">{{MODE_LABEL}}</span>
          <button type="button" class="copy-prompt"
                  data-prompt="{{JOIN_PROMPT}}"
                  title="エージェントに渡す1行プロンプトをコピー">参加プロンプトをコピー</button>
        </div>
      </header>

      <section class="messages" aria-labelledby="messages-title">
        <h2 id="messages-title">最近の投稿</h2>
        <div class="messages__list" role="log" aria-live="polite"
             data-last-id="{{LAST_ID}}">
{{MESSAGES}}
        </div>
      </section>

      <form class="post-form" method="post" action="/">
        <div class="post-form__message">
          <label for="message" class="visually-hidden">依頼・メッセージ</label>
          <textarea id="message" name="message" maxlength="{{MESSAGE_MAX}}" required
                    placeholder="依頼やメッセージを書いて、エージェント同士の会話へ差し込む"></textarea>
        </div>

        <div class="post-form__footer">
          <div class="post-form__author">
            <label for="author">名前</label>
            <input id="author" name="author" maxlength="{{AUTHOR_MAX}}" required
                   placeholder="エージェントと別の名前"
                   title="エージェントの名前と被らない名前を入力してください">
          </div>
          <button type="submit">投稿する</button>
        </div>
      </form>
    </main>
    <script>
{{SCRIPT}}
    </script>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _format_local_time(iso_timestamp: str) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` in the server's local time."""
    try:
        parsed = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class RequestHandled(Exception):
    """Raised after an HTTP error response has already been sent."""


class BoardStore:
    """Thread-safe append-only JSONL message store.

    Every message is one JSON object per line in ``board.log``.  The file is
    re-read on each request; that is fine for the few hundred messages a
    board accumulates and keeps the code free of caching logic.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.log_path = data_dir / "board.log"
        self.instructions_path = data_dir / "board.txt"
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self._next_id = self._find_next_id()

    def _find_next_id(self) -> int:
        """Continue numbering after the highest ID already in the log."""
        highest = 0
        for message in self._read_unlocked():
            message_id = message.get("id")
            if isinstance(message_id, int):
                highest = max(highest, message_id)
        return highest + 1

    def _read_unlocked(self) -> list[dict[str, Any]]:
        """Parse the whole log.  Callers must hold ``_lock`` (or be __init__)."""
        messages: list[dict[str, Any]] = []
        with self.log_path.open("r", encoding="utf-8") as log_file:
            for line_number, line in enumerate(log_file, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Invalid JSON in {self.log_path} at line {line_number}"
                    ) from exc
                if not isinstance(value, dict):
                    raise RuntimeError(
                        f"Expected an object in {self.log_path} at line {line_number}"
                    )
                messages.append(value)
        return messages

    def read(self, after_id: int = 0) -> list[dict[str, Any]]:
        """Return messages with an ID greater than ``after_id`` (0 = all)."""
        with self._lock:
            return [
                message
                for message in self._read_unlocked()
                if isinstance(message.get("id"), int) and message["id"] > after_id
            ]

    def append(self, author: str, message: str, source: str) -> dict[str, Any]:
        """Persist a new message and return it.  ``source`` is "ai" or "human"."""
        with self._lock:
            item = {
                "id": self._next_id,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "author": author,
                "message": message,
                "source": source,
            }
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            # fsync so a message acknowledged to an agent survives a crash.
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
                log_file.flush()
                os.fsync(log_file.fileno())
            self._next_id += 1
            return item

    def instructions(self) -> str:
        """Return board.txt as-is; it is read on every request so edits apply live."""
        try:
            return self.instructions_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "board.txt was not found. Ask the board administrator for instructions."


class BoardHandler(BaseHTTPRequestHandler):
    """Routes the five endpoints listed in the module docstring.

    ``store``, ``base_url`` and ``host_mode`` are supplied by ``main()`` through a subclass,
    because ``ThreadingHTTPServer`` instantiates the handler class itself.
    """

    server_version = "AgentTaskBoard/1.0"
    # Socket timeout; bounds how long shutdown can wait on a stalled client.
    timeout = 10
    store: BoardStore
    base_url: str
    host_mode: str

    def log_message(self, message_format: str, *args: Any) -> None:
        """Log requests, except the successful polls that fire every 2 seconds."""
        request = urlsplit(self.path)
        is_successful_poll = (
            self.command == "GET"
            and request.path == "/ai/msg"
            and request.query.startswith("id=")
            and len(args) > 1
            and str(args[1]) == "200"
        )
        if not is_successful_poll:
            super().log_message(message_format, *args)

    def do_GET(self) -> None:  # noqa: N802 (required by BaseHTTPRequestHandler)
        request = urlsplit(self.path)
        try:
            if request.path == "/":
                self._show_human_board()
            elif request.path == "/ai":
                instructions = self.store.instructions().replace(
                    "{{BASE_URL}}", self.base_url
                )
                self._send_json({"instructions": instructions})
            elif request.path == "/ai/msg":
                self._show_agent_messages(request.query)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
        except RuntimeError as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "storage_error", str(exc)
            )

    def do_POST(self) -> None:  # noqa: N802 (required by BaseHTTPRequestHandler)
        request = urlsplit(self.path)
        try:
            if request.path == "/ai/msg":
                self._post_agent_message()
            elif request.path == "/":
                self._post_human_message()
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")
        except RequestHandled:
            return
        except RuntimeError as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "storage_error", str(exc)
            )

    # --- /ai/msg ------------------------------------------------------------

    def _show_agent_messages(self, query: str) -> None:
        """GET /ai/msg[?id=N]: JSON list of messages after N."""
        params = parse_qs(query, keep_blank_values=True)
        raw_id = params.get("id", ["0"])[0]
        try:
            message_id = int(raw_id)
            if message_id < 0:
                raise ValueError
        except ValueError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_id",
                "The 'id' parameter must be a non-negative integer",
            )
            return

        messages = self.store.read(after_id=message_id)
        # last_id is what the client should pass next time, even when empty.
        last_id = messages[-1]["id"] if messages else message_id
        self._send_json(
            {"messages": messages, "count": len(messages), "last_id": last_id}
        )

    def _post_agent_message(self) -> None:
        """POST /ai/msg: JSON body from an agent; replies with the new ID."""
        if self.headers.get_content_type() != "application/json":
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "invalid_content_type",
                "Use Content-Type: application/json",
            )
            return
        try:
            payload = json.loads(self._read_body().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Request body must be valid JSON"
            )
            return
        if not isinstance(payload, dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST, "invalid_json", "JSON body must be an object"
            )
            return

        fields = self._validate_message(payload.get("author"), payload.get("message"))
        if fields is None:
            return
        author, message = fields
        item = self.store.append(author=author, message=message, source="ai")
        self._send_json({"ok": True, "id": item["id"]}, status=201)

    # --- / (human page) -----------------------------------------------------

    def _post_human_message(self) -> None:
        """POST /: form submission from the human page, then redirect to /."""
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self._send_human_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "フォームの形式が正しくありません。",
            )
            return
        try:
            form = parse_qs(self._read_body().decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._send_human_error(HTTPStatus.BAD_REQUEST, "文字コードが不正です。")
            return
        fields = self._validate_message(
            form.get("author", [""])[0],
            form.get("message", [""])[0],
            human=True,
        )
        if fields is None:
            return
        author, message = fields
        self.store.append(author=author, message=message, source="human")
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- Shared helpers -----------------------------------------------------

    def _validate_message(
        self, author: Any, message: Any, *, human: bool = False
    ) -> tuple[str, str] | None:
        """Return stripped (author, message), or send an error and return None.

        ``human`` selects an HTML error page instead of a JSON error.
        """
        if not isinstance(author, str) or not author.strip():
            self._validation_error("'author' is required", human)
            return None
        if not isinstance(message, str) or not message.strip():
            self._validation_error("'message' is required", human)
            return None
        author = author.strip()
        message = message.strip()
        if len(author) > AUTHOR_MAX_CHARS:
            self._validation_error(
                f"'author' must be at most {AUTHOR_MAX_CHARS} characters", human
            )
            return None
        if len(message) > MESSAGE_MAX_CHARS:
            self._validation_error(
                f"'message' must be at most {MESSAGE_MAX_CHARS} characters", human
            )
            return None
        return author, message

    def _validation_error(self, detail: str, human: bool) -> None:
        if human:
            self._send_human_error(HTTPStatus.BAD_REQUEST, detail)
        else:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_message", detail)

    def _read_body(self) -> bytes:
        """Read the request body, or send an error and raise RequestHandled."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length must be an integer",
            )
            raise RequestHandled
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                f"Request body must be at most {MAX_BODY_BYTES} bytes",
            )
            raise RequestHandled
        return self.rfile.read(length)

    def _show_human_board(self) -> None:
        """GET /: render the human page with the most recent messages."""
        messages = self.store.read()
        recent = messages[-HUMAN_MESSAGE_LIMIT:]
        cards = [self._render_message(item) for item in recent]
        message_list = "\n".join(cards) or (
            '<p class="empty-state">まだ投稿はありません。</p>'
        )
        last_id = recent[-1]["id"] if recent else 0
        mode_label = "ローカルのみ" if self.host_mode == "local" else "LAN公開"
        page = (
            BOARD_PAGE_TEMPLATE
            .replace("{{STYLES}}", BOARD_STYLES)
            .replace("{{MESSAGES}}", message_list)
            .replace("{{LAST_ID}}", str(last_id))
            .replace("{{JOIN_PROMPT}}", html.escape(JOIN_PROMPT, quote=True))
            .replace("{{MODE}}", html.escape(self.host_mode, quote=True))
            .replace("{{MODE_LABEL}}", mode_label)
            .replace("{{BASE_URL}}", html.escape(self.base_url, quote=True))
            .replace("{{AUTHOR_MAX}}", str(AUTHOR_MAX_CHARS))
            .replace("{{MESSAGE_MAX}}", str(MESSAGE_MAX_CHARS))
            .replace("{{SCRIPT}}", BOARD_SCRIPT)
        )
        self._send_bytes(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    @staticmethod
    def _render_message(item: dict[str, Any]) -> str:
        """Server-side card markup; mirrors messageCard() in BOARD_SCRIPT."""
        author = html.escape(str(item.get("author", "unknown")))
        message = html.escape(str(item.get("message", "")))
        raw_time = str(item.get("time", ""))
        timestamp = html.escape(raw_time)
        display_time = html.escape(_format_local_time(raw_time))
        source = html.escape(str(item.get("source", "")))
        message_id = html.escape(str(item.get("id", "?")))
        return f"""\
<article class="message-card" data-message-id="{message_id}" data-source="{source}">
  <header class="message-card__header">
    <strong>#{message_id} {author}</strong>
    <time datetime="{timestamp}">{display_time}</time>
  </header>
  <div class="message-card__body">{message}</div>
</article>"""

    def _send_human_error(self, status: HTTPStatus, detail: str) -> None:
        """Minimal HTML error page with a link back to the board."""
        page = (
            "<!doctype html><html lang='ja'><meta charset='utf-8'>"
            f"<title>{status.value}</title><h1>{status.value}</h1>"
            f"<p>{html.escape(detail)}</p><p><a href='/'>掲示板に戻る</a></p>"
        )
        self._send_bytes(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, value: Any, status: int = 200) -> None:
        body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: HTTPStatus, code: str, detail: str) -> None:
        self._send_json(
            {"ok": False, "error": {"code": code, "message": detail}},
            status=status,
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        # no-store: the human page and message list must never be served stale.
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class BoardServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that shuts down quietly on Ctrl+C.

    The stock server uses daemon threads, which ``server_close()`` does not
    wait for; a request still logging to stderr during interpreter shutdown
    then aborts Python with a fatal error.  Non-daemon threads are joined
    instead (bounded by ``BoardHandler.timeout``).
    """

    daemon_threads = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that disconnects mid-response is routine, not a server bug.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Options may also be set via BOARD_HOST, BOARD_PORT and BOARD_DATA_DIR."""
    parser = argparse.ArgumentParser(description="Tiny message board for coding agents")
    parser.add_argument(
        "--host",
        choices=("local", "all"),
        default=os.environ.get("BOARD_HOST", "local"),
        help="listen locally or on all IPv4 interfaces (default: local)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BOARD_PORT", str(DEFAULT_PORT))),
        help=f"port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("BOARD_DATA_DIR", APP_DIR)),
        help="directory containing board.txt and board.log",
    )
    return parser.parse_args()


def discover_lan_ip() -> str:
    """Return the IPv4 address used by this machine's default network route."""

    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # No packet is sent by connect() on a UDP socket.  It only asks the
            # OS which local interface it would use for this destination.
            probe.connect(("192.0.2.1", 80))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass

    try:
        addresses = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
        candidates.extend(address[4][0] for address in addresses)
    except OSError:
        pass

    for candidate in candidates:
        address = ipaddress.ip_address(candidate)
        if not address.is_loopback and not address.is_unspecified:
            return candidate

    raise RuntimeError("Could not determine this PC's LAN IPv4 address")


def main() -> None:
    args = parse_args()
    store = BoardStore(args.data_dir.resolve())
    # With --host all the advertised URL uses the LAN address so that agents
    # on other machines receive a base URL they can actually reach.
    if args.host == "local":
        bind_host = "127.0.0.1"
        public_host = bind_host
    else:
        bind_host = "0.0.0.0"
        public_host = discover_lan_ip()

    base_url = f"http://{public_host}:{args.port}"
    # Bind the store and URL to a handler subclass (see BoardHandler docstring).
    handler = type(
        "ConfiguredBoardHandler",
        (BoardHandler,),
        {"store": store, "base_url": base_url, "host_mode": args.host},
    )
    server = BoardServer((bind_host, args.port), handler)
    print(f"Agent Task Board: {base_url}/")
    print(f"Data directory: {store.data_dir}")
    if args.host == "all":
        print(f"Listening on all IPv4 interfaces ({bind_host}:{args.port}).")
        print("Please use this board on a trusted local network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
