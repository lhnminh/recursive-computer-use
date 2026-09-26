"""Simple local web front end for the recursive computer-use harness.

It uses only the Python standard library, so there is no extra front-end
dependency. It serves one chat page and a
JSON ``/api/run`` endpoint that drives the same policy-enforced ``agent.run``
path used by the CLI, through :mod:`recursive_computer_use.chat_runtime`.

Safety properties preserved from the old UI:

- The server binds to the loopback interface only, so another machine cannot
  remotely trigger mouse or keyboard control.
- A process-wide lock allows only one desktop task at a time, even with several
  browser tabs open.
- MongoDB credentials stay in the environment and never enter the page.
- Task execution goes through ``chat_runtime.execute_task`` unchanged, keeping
  sanitized telemetry, the review boundary, and verifier-only promotion.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from dotenv import load_dotenv

from . import auth
from .chat_runtime import (
    ChatOptions,
    DEFAULT_MODEL,
    DEFAULT_TASK_KEY,
    execute_task,
    safe_error,
)


HOST = "127.0.0.1"
PORT = int(os.environ.get("RCU_WEB_PORT", "8600"))

# The local codex-as-api proxy the agent uses for Codex OAuth (see auth.py).
_PROXY_HOST = "127.0.0.1"
_PROXY_PORT = 18080

# One desktop task at a time, shared across all connections and tabs.
_DESKTOP_LOCK = threading.Lock()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Recursive Computer Use</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin: 0; background: #0f1115; color: #e6e6e6;
    display: flex; justify-content: center;
  }
  main { width: 100%; max-width: 720px; padding: 24px 16px 120px; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .caption { color: #9aa0a6; font-size: 0.85rem; margin: 0 0 16px; }
  .controls {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px;
    background: #171a21; border: 1px solid #262b35; border-radius: 10px;
    padding: 12px; margin-bottom: 16px;
  }
  .controls label { font-size: 0.75rem; color: #9aa0a6; display: block; margin-bottom: 2px; }
  .controls input[type=text] {
    width: 100%; padding: 6px 8px; border-radius: 6px;
    border: 1px solid #2c313c; background: #0f1115; color: #e6e6e6; font-size: 0.85rem;
  }
  .toggles { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 14px; }
  .toggles label { display: flex; align-items: center; gap: 6px; color: #cfd3d8; font-size: 0.8rem; }
  #log { display: flex; flex-direction: column; gap: 10px; }
  .msg { padding: 10px 12px; border-radius: 10px; white-space: pre-wrap; line-height: 1.4; font-size: 0.92rem; }
  .msg.user { background: #1f6feb; color: #fff; align-self: flex-end; max-width: 85%; }
  .msg.assistant { background: #171a21; border: 1px solid #262b35; align-self: flex-start; max-width: 95%; }
  .msg.error { background: #3d1418; border: 1px solid #7d2530; color: #ffb3ba; align-self: flex-start; max-width: 95%; }
  .msg.status { background: transparent; color: #9aa0a6; font-size: 0.8rem; align-self: center; }
  .composer {
    position: fixed; bottom: 0; left: 0; right: 0; display: flex; justify-content: center;
    background: linear-gradient(transparent, #0f1115 30%); padding: 16px;
  }
  .composer-inner { width: 100%; max-width: 720px; display: flex; gap: 8px; }
  #prompt {
    flex: 1; padding: 12px; border-radius: 10px; border: 1px solid #2c313c;
    background: #171a21; color: #e6e6e6; font-size: 0.95rem; resize: none;
  }
  #prompt:disabled { opacity: 0.5; }
  button.send {
    padding: 0 18px; border: none; border-radius: 10px; background: #1f6feb;
    color: #fff; font-size: 0.95rem; cursor: pointer;
  }
  button.send:disabled { opacity: 0.5; cursor: not-allowed; }
  .warn { color: #f0b429; font-size: 0.8rem; margin-top: 6px; }
</style>
</head>
<body>
<main>
  <h1>Recursive Computer Use</h1>
  <p class="caption">Local chat for the policy-enforced desktop agent. Submitting a task can move your mouse and type on your keyboard.</p>

  <div class="controls">
    <div>
      <label for="model">Model</label>
      <input id="model" type="text" value="__MODEL__" />
    </div>
    <div>
      <label for="taskKey">Task family</label>
      <input id="taskKey" type="text" value="__TASK_KEY__" />
    </div>
    <div style="grid-column: 1 / -1;">
      <label for="verifier">Local verifier URL (optional)</label>
      <input id="verifier" type="text" placeholder="http://127.0.0.1:8765/api/result" />
    </div>
    <div class="toggles">
      <label><input id="armed" type="checkbox" /> Arm computer control</label>
      <label><input id="stopIrreversible" type="checkbox" checked /> Stop before irreversible actions</label>
      <label><input id="logActions" type="checkbox" __MONGO_CHECKED__ /> MongoDB telemetry</label>
      <label><input id="evolve" type="checkbox" __MONGO_CHECKED__ /> Self-improvement</label>
    </div>
  </div>
  <p id="mongoNote" class="caption">__MONGO_NOTE__</p>

  <div id="log">
    <div class="msg assistant">Tell me what to do on this computer. I will inspect the screen, operate the local mouse and keyboard, and report the result.</div>
  </div>
</main>

<div class="composer">
  <div class="composer-inner">
    <textarea id="prompt" rows="1" placeholder="Arm computer control, then tell the agent what to do" disabled></textarea>
    <button class="send" id="send" disabled>Send</button>
  </div>
</div>

<script>
  const log = document.getElementById("log");
  const promptEl = document.getElementById("prompt");
  const sendEl = document.getElementById("send");
  const armedEl = document.getElementById("armed");
  let busy = false;

  function addMsg(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    log.appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "end" });
    return div;
  }

  function refreshEnabled() {
    const on = armedEl.checked && !busy;
    promptEl.disabled = !on;
    sendEl.disabled = !on;
  }
  armedEl.addEventListener("change", refreshEnabled);

  async function submit() {
    const prompt = promptEl.value.trim();
    if (!prompt || busy) return;
    busy = true; refreshEnabled();
    addMsg("user", prompt);
    promptEl.value = "";
    const status = addMsg("status", "Agent is controlling the local desktop. Move the pointer to a screen corner to trigger the pyautogui fail-safe.");

    try {
      const resp = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          model: document.getElementById("model").value,
          task_key: document.getElementById("taskKey").value,
          verifier_url: document.getElementById("verifier").value,
          stop_before_irreversible: document.getElementById("stopIrreversible").checked,
          log_actions: document.getElementById("logActions").checked,
          evolve: document.getElementById("evolve").checked,
        }),
      });
      const data = await resp.json();
      status.remove();
      if (data.ok) {
        addMsg("assistant", data.answer);
      } else {
        addMsg("error", data.error || "Task failed.");
      }
    } catch (err) {
      status.remove();
      addMsg("error", "Could not reach the local server: " + err);
    } finally {
      busy = false; refreshEnabled();
    }
  }

  sendEl.addEventListener("click", submit);
  promptEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
</script>
</body>
</html>
"""


def _render_page() -> str:
    mongo_configured = bool(os.environ.get("MONGODB_URI", "").strip())
    note = (
        "MongoDB configured through .env."
        if mongo_configured
        else "MongoDB is not configured. Tasks can still run without learning."
    )
    return (
        PAGE.replace("__MODEL__", html.escape(os.environ.get("RCU_MODEL", DEFAULT_MODEL)))
        .replace("__TASK_KEY__", DEFAULT_TASK_KEY)
        .replace("__MONGO_CHECKED__", "checked" if mongo_configured else "")
        .replace("__MONGO_NOTE__", note)
    )


def _run_task(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        return {"ok": False, "error": "Enter a task before starting computer control."}

    if not _DESKTOP_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "Another task is controlling this desktop. Wait for it to finish and try again.",
        }
    try:
        options = ChatOptions(
            model=str(payload.get("model") or DEFAULT_MODEL),
            task_key=str(payload.get("task_key") or DEFAULT_TASK_KEY),
            verifier_url=(str(payload.get("verifier_url")).strip() or None)
            if payload.get("verifier_url")
            else None,
            mongodb_db=os.environ.get("MONGODB_DB") or None,
            log_actions=bool(payload.get("log_actions", False)),
            evolve=bool(payload.get("evolve", False)),
            verbose=False,
            stop_before_irreversible=bool(payload.get("stop_before_irreversible", True)),
        )
        answer = execute_task(prompt, options).strip()
        return {"ok": True, "answer": answer or "The agent finished without a text response."}
    except Exception as exc:  # The server must survive a failed desktop run.
        return {"ok": False, "error": f"Task failed: {safe_error(exc)}"}
    finally:
        _DESKTOP_LOCK.release()


class _Handler(BaseHTTPRequestHandler):
    server_version = "RCUWebUI/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path in ("/", "/index.html"):
            self._send(200, _render_page().encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != "/api/run":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
        except (ValueError, json.JSONDecodeError):
            self._send(
                400,
                json.dumps({"ok": False, "error": "Invalid request body."}).encode("utf-8"),
                "application/json",
            )
            return
        result = _run_task(payload)
        self._send(200, json.dumps(result).encode("utf-8"), "application/json")

    def log_message(self, *args: Any) -> None:  # Quieter default logging.
        return


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if something is already listening on host:port."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _needs_codex_proxy() -> bool:
    """The proxy is needed only when credentials resolve to it (Codex OAuth)."""

    try:
        config = auth.resolve()
    except Exception:
        # No credentials at all; let the agent surface the error on first task.
        return False
    return config.base_url == auth.CODEX_PROXY_BASE_URL


def _start_codex_proxy() -> subprocess.Popen | None:
    """Spawn ``npx codex-as-api`` and wait for it to listen. Return the process.

    Returns ``None`` if the proxy is already running or ``npx`` is unavailable.
    """

    if _port_open(_PROXY_HOST, _PROXY_PORT):
        print(f"Codex proxy already running on {_PROXY_HOST}:{_PROXY_PORT}.")
        return None

    npx = shutil.which("npx")
    if npx is None:
        print(
            "Codex OAuth is configured but 'npx' was not found on PATH.\n"
            "Install Node.js, or start the proxy manually with 'npx codex-as-api',\n"
            "or set OPENAI_API_KEY in .env to skip the proxy."
        )
        return None

    print("Starting Codex proxy: npx codex-as-api ...")
    process = subprocess.Popen([npx, "codex-as-api"])

    # Wait up to ~30s for the proxy port to come up.
    for _ in range(60):
        if process.poll() is not None:
            print("Codex proxy exited before it became ready.")
            return None
        if _port_open(_PROXY_HOST, _PROXY_PORT):
            print(f"Codex proxy ready on {_PROXY_HOST}:{_PROXY_PORT}.")
            return process
        time.sleep(0.5)

    print("Timed out waiting for the Codex proxy to start; continuing anyway.")
    return process


def _stop_codex_proxy(process: subprocess.Popen | None) -> None:
    """Terminate a proxy we started, giving it a moment to exit cleanly."""

    if process is None or process.poll() is not None:
        return
    print("Stopping Codex proxy.")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def launch() -> None:
    """Serve the local web UI, auto-starting the Codex proxy when required.

    When credentials resolve to the local Codex proxy (Codex OAuth) and the
    proxy is not already listening, this launches ``npx codex-as-api`` first
    and shuts it down on exit. With an ``OPENAI_API_KEY`` no proxy is started.
    """

    load_dotenv()

    proxy: subprocess.Popen | None = None
    if _needs_codex_proxy():
        proxy = _start_codex_proxy()

    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Recursive Computer Use web UI on {url}")
    print("Open it in a browser, arm computer control, and enter a task.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    finally:
        server.server_close()
        _stop_codex_proxy(proxy)


if __name__ == "__main__":
    launch()
