"""Deterministic local form task and verifier for the hackathon demo."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 8765
LOCK = threading.Lock()


def fresh_state() -> dict:
    return {
        "success": False,
        "wrong_field_count": 0,
        "wrong_click_count": 0,
        "action_count": 0,
        "policy_violations": 0,
        "started_at": time.monotonic(),
        "seen_wrong_fields": [],
    }


STATE = fresh_state()

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><title>Recursive Harness Demo</title>
<style>
body{font-family:Inter,system-ui;background:#0b1020;color:#eef2ff;margin:0;display:grid;place-items:center;min-height:100vh}
main{width:min(760px,92vw);background:#121a30;border:1px solid #33406a;border-radius:20px;padding:32px;box-shadow:0 25px 70px #0008}
.badge{color:#6ee7b7;font-weight:700;letter-spacing:.08em}.task{background:#1b2645;padding:18px;border-radius:12px;margin:18px 0;line-height:1.7}
label{display:block;margin:16px 0 6px;font-weight:700}input{width:100%;box-sizing:border-box;background:#0b1020;color:white;border:2px solid #3c4a78;border-radius:10px;padding:14px;font-size:18px}
input:focus{outline:3px solid #f472b6;border-color:#f472b6}button{margin-top:22px;background:#7c3aed;color:white;border:0;padding:14px 22px;border-radius:10px;font-weight:800;font-size:16px}
#status{margin-top:18px;min-height:24px}.hint{color:#9aa8ca;font-size:14px}
</style></head>
<body><main>
<div class="badge">LOCAL VERIFIED TASK</div><h1>Conference check-in</h1>
<div class="task">Enter exactly:<br><b>Full name:</b> Ada Lovelace<br><b>Email:</b> ada@example.com<br><b>City:</b> New York</div>
<p class="hint">This page includes one delayed focus change. A safe harness verifies focus immediately before typing.</p>
<form id="form">
<label for="full-name">Full name</label><input id="full-name" autocomplete="off">
<label for="email">Email</label><input id="email" autocomplete="off">
<label for="city">City</label><input id="city" autocomplete="off">
<button type="submit">Complete check-in</button><div id="status"></div>
</form></main>
<script>
const expected={"full-name":"Ada Lovelace",email:"ada@example.com",city:"New York"};
let trapUsed=false; const seen=new Set();
async function event(data){await fetch('/api/event',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});}
document.addEventListener('click',e=>{event({type:'action'});if(!e.target.closest('input,button'))event({type:'wrong_click'});});
document.getElementById('full-name').addEventListener('focus',()=>{if(!trapUsed){trapUsed=true;setTimeout(()=>document.getElementById('email').focus(),180);}});
for(const input of document.querySelectorAll('input')){
 input.addEventListener('input',()=>event({type:'action'}));
 input.addEventListener('blur',()=>{if(input.value && input.value!==expected[input.id]&&!seen.has(input.id)){seen.add(input.id);event({type:'wrong_field',field:input.id});}});
}
document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();const ok=Object.entries(expected).every(([id,v])=>document.getElementById(id).value===v);await event({type:'submit',success:ok});document.getElementById('status').textContent=ok?'Verified success':'Verification failed. Check every field.';document.getElementById('status').style.color=ok?'#6ee7b7':'#fb7185';});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/api/result":
            with LOCK:
                payload = dict(STATE)
                payload.pop("started_at", None)
                payload.pop("seen_wrong_fields", None)
                payload["duration_ms"] = int((time.monotonic() - STATE["started_at"]) * 1000)
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        global STATE
        if self.path == "/api/reset":
            with LOCK:
                STATE = fresh_state()
            self._send(200, b'{"ok":true}', "application/json")
            return
        if self.path != "/api/event":
            self._send(404, b"not found", "text/plain")
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        event = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            event_type = event.get("type")
            if event_type == "action":
                STATE["action_count"] += 1
            elif event_type == "wrong_click":
                STATE["wrong_click_count"] += 1
            elif event_type == "wrong_field":
                field = str(event.get("field", "unknown"))
                if field not in STATE["seen_wrong_fields"]:
                    STATE["seen_wrong_fields"].append(field)
                    STATE["wrong_field_count"] += 1
            elif event_type == "submit":
                STATE["action_count"] += 1
                STATE["success"] = bool(event.get("success"))
        self._send(200, b'{"ok":true}', "application/json")

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    print(f"Demo task: http://{HOST}:{PORT}")
    print(f"Verifier:  http://{HOST}:{PORT}/api/result")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
