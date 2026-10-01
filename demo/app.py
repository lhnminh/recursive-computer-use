"""Deterministic local form task and verifier for the hackathon demo."""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


HOST = "127.0.0.1"
PORT = 8765
LOCK = threading.Lock()
DEFAULT_GUEST = {"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"}
SESSIONS: dict[str, str] = {}
REDESIGN_ON = False


def fresh_state(guest: dict[str, str] | None = None) -> dict:
    return {
        "success": False,
        "wrong_field_count": 0,
        "wrong_click_count": 0,
        "action_count": 0,
        "policy_violations": 0,
        "started_at": time.monotonic(),
        "seen_wrong_fields": [],
        "expected_guest": dict(guest or DEFAULT_GUEST),
    }


STATE = fresh_state()

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Recursive Harness Demo</title>
<style>
body{font-family:Inter,system-ui;background:#0b1020;color:#eef2ff;margin:0;display:grid;place-items:center;min-height:100vh}
main{width:min(760px,92vw);background:#121a30;border:1px solid #33406a;border-radius:8px;padding:32px}
.badge{color:#6ee7b7;font-weight:700}.task{background:#1b2645;padding:18px;border-radius:8px;margin:18px 0;line-height:1.7}
label{display:block;margin:16px 0 6px;font-weight:700}input{width:100%;box-sizing:border-box;background:#0b1020;color:white;border:2px solid #3c4a78;border-radius:6px;padding:14px;font-size:18px}
input:focus{outline:3px solid #f472b6;border-color:#f472b6}button{margin-top:16px;background:#7c3aed;color:white;border:0;padding:12px 18px;border-radius:6px;font-weight:700}
#status{margin-top:18px;min-height:24px}.hint{color:#9aa8ca;font-size:14px}
</style></head><body><main>
<div class="badge">LOCAL VERIFIED TASK</div><h1>Conference check-in</h1>
<div class="task">Enter exactly:<br><b>Full name:</b> <span id="expected-name"></span><br><b>Email:</b> <span id="expected-email"></span><br><b>City:</b> <span id="expected-city"></span></div>
<p class="hint">This page includes one delayed focus change. A safe harness verifies focus immediately before typing.</p>
<button id="redesign-switch" type="button"></button>
<form id="form">
<label for="full-name">Full name</label><input id="full-name" autocomplete="off">
<label for="email">Email</label><input id="email" autocomplete="off">
<label for="city">City</label><input id="city" autocomplete="off">
<button type="submit">Complete check-in</button><div id="status" aria-live="polite"></div>
</form></main>
<script>
let expected=__GUEST_JSON__;
let csrf="";
let redesigned=__REDESIGN__;
let trapUsed=false;const seen=new Set();
const fields={full_name:"full-name",email:"email",city:"city"};
function showExpected(){
 document.getElementById('expected-name').textContent=expected.full_name;
 document.getElementById('expected-email').textContent=expected.email;
 document.getElementById('expected-city').textContent=expected.city;
 document.getElementById('redesign-switch').textContent=redesigned?'Switch to original API':'Switch to redesigned API';
}
async function event(data){try{await fetch('/api/event',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});}catch(_){}}
document.addEventListener('click',e=>{event({type:'action'});if(!e.target.closest('input,button'))event({type:'wrong_click'});});
document.getElementById('full-name').addEventListener('focus',()=>{if(!trapUsed){trapUsed=true;setTimeout(()=>document.getElementById('email').focus(),180);}});
for(const input of document.querySelectorAll('input')){
 input.addEventListener('input',()=>event({type:'action'}));
 input.addEventListener('blur',()=>{if(input.value&&input.value!==expected[Object.keys(fields).find(k=>fields[k]===input.id)]&&!seen.has(input.id)){seen.add(input.id);event({type:'wrong_field',field:input.id});}});
}
document.getElementById('redesign-switch').addEventListener('click',async()=>{
 const response=await fetch('/api/redesign',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({on:!redesigned})});
 if(response.ok){const state=await response.json();redesigned=state.on;showExpected();}
});
document.getElementById('form').addEventListener('submit',async e=>{
 e.preventDefault();
 const values={full_name:document.getElementById('full-name').value,email:document.getElementById('email').value,city:document.getElementById('city').value};
 const url=redesigned?'/api/v2/check-in':'/api/checkin';
 const body=redesigned?{name:values.full_name,email:values.email,city:values.city}:values;
 const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
 const result=await response.json().catch(()=>({success:false}));
 const status=document.getElementById('status');
 status.textContent=result.success?'Verified success':(response.status===410?'This API version is retired. Reload the page.':'Verification failed. Check every field.');
 status.style.color=result.success?'#6ee7b7':'#fb7185';
});
async function start(){
 showExpected();
 const response=await fetch('/api/session');
 const session=await response.json();csrf=session.csrf;
}
start();
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", headers)

    def _read_json(self, max_bytes: int = 8192) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= max_bytes:
                return None
            payload = json.loads(self.rfile.read(length) or b"{}")
            return payload if isinstance(payload, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _session_id(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("sid")
        return morsel.value if morsel else None

    def _has_csrf(self) -> bool:
        sid = self._session_id()
        token = self.headers.get("X-CSRF-Token", "")
        with LOCK:
            expected = SESSIONS.get(sid or "")
        return bool(expected and secrets.compare_digest(token, expected))

    def _same_origin(self) -> bool:
        port = self.server.server_port
        allowed = {f"{HOST}:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{host}" for host in allowed}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            with LOCK:
                guest = json.dumps(STATE["expected_guest"], ensure_ascii=True).replace("<", "\\u003c")
                redesigned = "true" if REDESIGN_ON else "false"
            page = PAGE.replace("__GUEST_JSON__", guest).replace("__REDESIGN__", redesigned)
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/session":
            sid, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            with LOCK:
                SESSIONS[sid] = csrf
            self._json(
                200,
                {"csrf": csrf},
                {"Set-Cookie": f"sid={sid}; HttpOnly; SameSite=Strict; Path=/"},
            )
            return
        if path == "/api/result":
            with LOCK:
                payload = dict(STATE)
                payload.pop("started_at", None)
                payload.pop("seen_wrong_fields", None)
                payload.pop("expected_guest", None)
                payload["duration_ms"] = int((time.monotonic() - STATE["started_at"]) * 1000)
            self._json(200, payload)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        global STATE, REDESIGN_ON
        if not self._same_origin():
            self._send(403, b"forbidden", "text/plain")
            return
        path = urlparse(self.path).path
        if path == "/api/reset":
            payload = self._read_json()
            if payload is None:
                self._send(400, b"bad request", "text/plain")
                return
            guest = payload.get("guest", DEFAULT_GUEST)
            if not _valid_guest(guest):
                self._send(400, b"bad guest", "text/plain")
                return
            with LOCK:
                STATE = fresh_state(guest)
            self._json(200, {"ok": True, "guest": guest})
            return
        if path == "/api/redesign":
            payload = self._read_json()
            if payload is None or not isinstance(payload.get("on"), bool):
                self._send(400, b"bad request", "text/plain")
                return
            with LOCK:
                REDESIGN_ON = payload["on"]
            self._json(200, {"on": REDESIGN_ON})
            return
        if path == "/api/checkin" and REDESIGN_ON:
            self._send(410, b"endpoint retired", "text/plain")
            return
        if path == "/api/v2/check-in" and not REDESIGN_ON:
            self._send(410, b"redesigned endpoint unavailable", "text/plain")
            return
        if path in {"/api/checkin", "/api/v2/check-in"}:
            if not self._has_csrf():
                self._send(403, b"forbidden", "text/plain")
                return
            payload = self._read_json()
            if payload is None:
                self._send(400, b"bad request", "text/plain")
                return
            if path == "/api/checkin":
                guest = {key: payload.get(key) for key in ("full_name", "email", "city")}
            else:
                guest = {"full_name": payload.get("name"), "email": payload.get("email"), "city": payload.get("city")}
            with LOCK:
                ok = guest == STATE["expected_guest"]
                STATE["success"] = ok
            self._json(200, {"success": ok})
            return
        if path != "/api/event":
            self._send(404, b"not found", "text/plain")
            return
        event = self._read_json(max_bytes=4096)
        if event is None:
            self._send(400, b"bad request", "text/plain")
            return
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
        self._json(200, {"ok": True})

    def log_message(self, format: str, *args) -> None:
        return


def _valid_guest(guest: object) -> bool:
    return (
        isinstance(guest, dict)
        and set(guest) == {"full_name", "email", "city"}
        and all(isinstance(guest[key], str) and 0 < len(guest[key]) <= 200 for key in guest)
    )


if __name__ == "__main__":
    print(f"Demo task: http://{HOST}:{PORT}")
    print(f"Verifier:  http://{HOST}:{PORT}/api/result")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
