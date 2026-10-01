"""Simple local web front end for the recursive computer-use harness.

This provides a local chat surface. It uses only the Python standard
library so there is no extra front-end dependency. It serves one chat page and a
JSON ``/api/run`` endpoint that drives the same policy-enforced ``agent.run``
path used by the CLI, through :mod:`recursive_computer_use.chat_runtime`.

Safety properties preserved from the old UI:

- The server binds to the loopback interface only, so another machine cannot
  remotely trigger mouse or keyboard control.
- A process-wide lock allows only one desktop task at a time, even with several
  browser tabs open.
- MongoDB credentials stay in the environment and never enter the page.
- Task execution goes through ``chat_runtime.execute_task`` unchanged, keeping
  sanitized telemetry and verifier-only promotion.
"""

from __future__ import annotations

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


HOST = os.environ.get("RCU_WEB_HOST", "127.0.0.1")
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
<title>Self Improving Computer Use</title>
<style>
  :root { color-scheme: dark; --ink:#f4f1e9; --muted:#a9aaa5; --line:#343832; --panel:#171a16; --lime:#c8ef74; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; background:#10120f; color:var(--ink); font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  main { width:min(100%,960px); margin:auto; padding:30px 28px 150px; }
  header { display:flex; justify-content:space-between; align-items:center; margin-bottom:76px; }
  .brand { display:flex; align-items:center; gap:11px; font-size:14px; font-weight:650; letter-spacing:.01em; }
  .brand-mark { display:grid; place-items:center; width:30px; height:30px; color:#15180f; background:var(--lime); border-radius:9px; font-size:17px; }
  .local-pill { display:flex; align-items:center; gap:8px; color:#c2c5bc; font-size:12px; border:1px solid var(--line); border-radius:999px; padding:8px 12px; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--lime); box-shadow:0 0 12px #c8ef7480; }
  .eyebrow { color:var(--lime); font-size:11px; font-weight:700; letter-spacing:.14em; text-transform:uppercase; }
  h1 { max-width:680px; margin:14px 0 14px; font-size:clamp(42px,7vw,68px); line-height:.99; letter-spacing:-.055em; font-weight:570; }
  .intro { max-width:560px; margin:0; color:var(--muted); font-size:16px; line-height:1.6; }
  .workbench { margin-top:36px; padding:22px; border:1px solid var(--line); border-radius:16px; background:#151713; }
  .workbench-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }
  .workbench-title { display:flex; align-items:center; gap:9px; font-size:13px; font-weight:600; }
  .arm { display:flex; align-items:center; gap:8px; color:#b9bdb3; font-size:12px; cursor:pointer; }
  .arm input { accent-color:var(--lime); width:15px; height:15px; }
  #log { display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:88%; padding:11px 14px; border-radius:12px; white-space:pre-wrap; line-height:1.5; font-size:13px; }
  .msg.user { color:#15180f; background:var(--lime); align-self:flex-end; }
  .msg.assistant { color:#e4e5de; background:#20231e; border:1px solid #30342d; align-self:flex-start; }
  .msg.error { color:#ffc0b9; background:#321d1a; border:1px solid #68403a; align-self:flex-start; }
  .msg.status { max-width:none; color:#abb09f; font-size:12px; align-self:center; }
  .composer { display:flex; gap:10px; margin-top:16px; }
  #prompt { flex:1; min-height:52px; max-height:130px; padding:15px; resize:vertical; color:var(--ink); background:#10120f; border:1px solid #3b4037; border-radius:11px; font:inherit; font-size:13px; outline:none; }
  #prompt:focus { border-color:#8ba950; box-shadow:0 0 0 3px #c8ef741c; }
  #prompt:disabled { opacity:.45; }
  button.send { padding:0 20px; border:0; border-radius:11px; background:var(--lime); color:#171a12; font-weight:700; cursor:pointer; }
  button.send:disabled { opacity:.4; cursor:not-allowed; }
  .footnote { margin:13px 0 0; color:#858a80; font-size:11px; line-height:1.5; }
  @media(max-width:620px) { main{padding:20px 16px 130px} header{margin-bottom:54px}.workbench{padding:16px}.footnote{max-width:330px} }
</style>
</head>
<body>
<main>
  <header>
    <div class="brand"><span class="brand-mark">↗</span> Self Improving Computer Use</div>
    <div class="local-pill"><i class="dot"></i> Running locally</div>
  </header>
  <div class="eyebrow">A desktop agent you can watch</div>
  <h1>Give your computer<br>something to do.</h1>
  <p class="intro">The agent sees the screen, uses the mouse and keyboard, and works through the task in front of you.</p>

  <section class="workbench">
    <div class="workbench-top">
      <div class="workbench-title"><i class="dot"></i> Computer-use session</div>
      <label class="arm"><input id="armed" type="checkbox" /> Arm desktop</label>
    </div>
    <div id="log"><div class="msg assistant">Your session is ready. Arm the desktop and describe a task to begin.</div></div>
    <div class="composer"><textarea id="prompt" rows="1" placeholder="Arm desktop to describe a task…" disabled></textarea><button class="send" id="send" disabled>Run task ↗</button></div>
  </section>
  <p class="footnote">Arming enables real mouse and keyboard control. Keep the desktop visible and move the pointer to a screen corner to stop the agent.</p>
</main>

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
    return PAGE


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
            log_actions=bool(payload.get("log_actions", True)),
            evolve=bool(payload.get("evolve", True)),
            verbose=False,
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
