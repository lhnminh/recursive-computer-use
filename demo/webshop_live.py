"""Live demo: learn a WebShop task once, store it in MongoDB, reuse it from any agent.

    uv run python demo/webshop_live.py        # then open http://127.0.0.1:8790

Needs WebShop on 127.0.0.1:3000, the Codex proxy on 127.0.0.1:18080 and
MONGODB_URI in .env. Standard library only; binds to loopback.

The page shows three lanes, live:

- Teach: one headless recorded purchase -> learned API recipe -> saved to
  MongoDB (``skills``, ``kind: "api_recipe"``).
- Agent from MongoDB: holds no local recipe. It finds one in MongoDB by
  meaning (Voyage + $rankFusion), replays it, and writes the verified result
  back (episode, uses/wins/lift, status). Any other agent on the same
  database does exactly this.
- Browsing agent: no memory; clicks through pages with one model call each.

A change stream on ``skills`` feeds the "MongoDB live" panel: the same event
every agent on the database receives when a recipe is learned or updated.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import create_connection
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts import webshop_eval as ws  # noqa: E402
from recursive_computer_use.network.learner import default_client  # noqa: E402
from recursive_computer_use.network.recipe import KIND  # noqa: E402

HOST, PORT = "127.0.0.1", 8790
TASKS = range(0, 13)  # official small setup: fixed_0 .. fixed_12

_subscribers: list[queue.Queue] = []
_history: list[dict[str, Any]] = []
_lock = threading.Lock()
_busy = {"teach": False, "race": False}
_store = None
_client = None
_instructions: dict[int, str] = {}


def publish(lane: str, event: dict[str, Any]) -> None:
    event = {"lane": lane, "t": time.time(), **event}
    with _lock:
        _history.append(event)
        del _history[:-300]
        for q in list(_subscribers):
            q.put(event)


def store():
    global _store
    if _store is None:
        _store = ws.atlas_store()
    return _store


def client():
    global _client
    if _client is None:
        _client = default_client()
    return _client


def port_open(port: int) -> bool:
    try:
        with create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def recipes() -> list[dict[str, Any]]:
    docs = store().skills.find(
        {"kind": KIND, "scope.site": ws.SITE},
        {"name": 1, "version": 1, "status": 1, "uses": 1, "wins": 1, "lift": 1, "updated_at": 1, "description": 1},
    ).sort([("updated_at", -1)]).limit(20)
    return [
        {**{k: v for k, v in d.items() if k not in {"_id", "updated_at"}}, "id": str(d["_id"]),
         "updated": d.get("updated_at").isoformat() if d.get("updated_at") else None}
        for d in docs
    ]


def status() -> dict[str, Any]:
    out = {"webshop": port_open(3000), "proxy": port_open(18080), "mongodb": False, "recipes": []}
    try:
        out["recipes"] = recipes()
        out["mongodb"] = True
    except Exception as exc:  # noqa: BLE001
        out["mongodb_error"] = type(exc).__name__
    return out


def instructions() -> dict[int, str]:
    for n in TASKS:
        if n not in _instructions:
            try:
                _instructions[n] = ws.instruction(f"fixed_{n}")
            except Exception:  # noqa: BLE001
                _instructions[n] = "(WebShop not reachable)"
    return _instructions


# -- jobs ----------------------------------------------------------------------


def _job(kind: str, target, *args) -> bool:
    with _lock:
        if _busy[kind]:
            return False
        _busy[kind] = True

    def run() -> None:
        try:
            target(*args)
        except Exception as exc:  # noqa: BLE001
            publish(kind if kind == "teach" else "recipe", {"kind": "error", "text": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            with _lock:
                _busy[kind] = False
            publish("system", {"kind": "idle", "job": kind})

    threading.Thread(target=run, daemon=True).start()
    return True


def teach(n: int, attempts: int = 2) -> None:
    """Learn from one recording, then verify on the demo task before other agents rely on it."""
    publish("teach", {"kind": "start", "text": f"Teach from task fixed_{n}"})
    for attempt in range(1, attempts + 1):
        recipe = ws.learn(n, store=store(), on_event=lambda e: publish("teach", e))
        publish("teach", {"kind": "verify", "text": "Verifying: replay the new recipe on its demo task (fresh session)"})
        row = ws.run_recipe_arm(recipe, ws.arm_session(f"verify{int(time.time()) % 1_000_000}", n), client(),
                                lambda e: publish("teach", {**e, "kind": "verify"}) if e["kind"] == "http" else None)
        status = store().record_result(recipe.id, ok=row["reward"] >= ws.RECIPE_OK_REWARD,
                                       run_ms=int(row["seconds"] * 1000), steps=[{}] * len(recipe.steps),
                                       task=ws.instruction(f"fixed_{n}"), llm_calls=row["llm_calls"])
        publish("teach", {"kind": "atlas", "text": f"Verified score {row['reward']:.2f}; recipe v{recipe.version} is now {status} in MongoDB"})
        if status == "active":
            return
        if attempt < attempts:
            publish("teach", {"kind": "learn", "text": "Verification failed, so the recipe retired. Learning again..."})


def race(n: int) -> None:
    run_id = f"{int(time.time()) % 1_000_000}"
    lanes = []
    lanes.append(threading.Thread(target=lambda: (
        publish("recipe", {"kind": "start", "text": f"Task fixed_{n}"}),
        ws.run_from_atlas(store(), ws.arm_session(f"recipe{run_id}", n), client(), lambda e: publish("recipe", e)),
    ), daemon=True))
    lanes.append(threading.Thread(target=lambda: (
        publish("browse", {"kind": "start", "text": f"Task fixed_{n}"}),
        ws.run_baseline_arm(ws.arm_session(f"browse{run_id}", n), client(), lambda e: publish("browse", e)),
    ), daemon=True))
    for t in lanes:
        t.start()
    for t in lanes:
        t.join()


def forget() -> int:
    """Retire every live WebShop recipe (history is kept), to demo teaching from scratch."""
    now = ws.datetime.now(ws.datetime.now().astimezone().tzinfo)
    res = store().skills.update_many(
        {"kind": KIND, "scope.site": ws.SITE, "status": {"$in": ["candidate", "active"]}},
        {"$set": {"status": "retired", "retired_at": now, "updated_at": now, "retirement_reason": "demo reset"}},
    )
    return res.modified_count


def watch_mongodb() -> None:
    """Stream recipe changes from MongoDB, as every agent on the database sees them."""
    while True:
        try:
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]},
                                    "fullDocument.kind": KIND}}]
            with store().skills.watch(pipeline, full_document="updateLookup") as stream:
                publish("system", {"kind": "watching", "text": "Change stream open on skills"})
                for change in stream:
                    d = change.get("fullDocument") or {}
                    fields = (change.get("updateDescription") or {}).get("updatedFields", {})
                    interesting = [f for f in ("status", "uses", "wins", "lift", "fail_streak") if f in fields]
                    if change["operationType"] == "update" and not interesting:
                        continue  # timestamps only
                    what = "new recipe" if change["operationType"] == "insert" else "updated " + ", ".join(interesting)
                    publish("atlas", {"kind": "change", "text": f"{d.get('name')} v{d.get('version')}: {what} "
                                      f"(status {d.get('status')}, uses {d.get('uses', 0)}, wins {d.get('wins', 0)})"})
        except Exception as exc:  # noqa: BLE001
            publish("system", {"kind": "error", "text": f"change stream: {type(exc).__name__}; retrying"})
            time.sleep(5)


# -- http ------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        allowed = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in allowed and (origin is None or origin in {f"http://{a}" for a in allowed})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._json(200, status())
        elif self.path == "/api/tasks":
            self._json(200, instructions())
        elif self.path == "/events":
            self._events()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._local():
            self._json(403, {"error": "forbidden"})
            return
        try:
            n = int(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2) or b"{}").get("task", 0))
        except (ValueError, AttributeError):
            self._json(400, {"error": "bad request"})
            return
        if n not in TASKS and self.path in {"/api/teach", "/api/race"}:
            self._json(400, {"error": "task out of range"})
            return
        if self.path == "/api/teach":
            self._json(202 if _job("teach", teach, n) else 409, {"ok": True})
        elif self.path == "/api/race":
            self._json(202 if _job("race", race, n) else 409, {"ok": True})
        elif self.path == "/api/forget":
            self._json(200, {"retired": forget()})
        else:
            self._json(404, {"error": "not found"})

    def _events(self) -> None:
        q: queue.Queue = queue.Queue()
        with _lock:
            backlog = list(_history)
            _subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for e in backlog:
                self.wfile.write(f"data: {json.dumps(e, default=str)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    e = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(e, default=str)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    def log_message(self, *args: Any) -> None:
        return


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Learn once, reuse everywhere</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--amber:#d29922;--violet:#bc8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}h1 small{color:var(--dim);font-weight:400}
.pill{padding:3px 10px;border-radius:99px;border:1px solid var(--line);font-size:12px;color:var(--dim)}
.pill.ok{color:var(--green);border-color:#238636}.pill.bad{color:var(--red);border-color:#da3633}
main{padding:16px 24px;display:grid;gap:16px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
select,button{background:#21262d;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font:inherit}
select{max-width:520px}button{cursor:pointer}button.primary{background:#238636;border-color:#2ea043}button:disabled{opacity:.5;cursor:default}
.lanes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
@media (max-width:1000px){.lanes{grid-template-columns:1fr}}
.lane{background:var(--panel);border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;min-height:380px}
.lane h2{margin:0;padding:12px 14px;font-size:14px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.lane h2 span.sub{color:var(--dim);font-weight:400;font-size:12px}
.stats{display:flex;gap:14px;padding:10px 14px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
.stat b{font-size:20px;display:block}.stat{color:var(--dim);font-size:12px}
.score b{color:var(--dim)}.score.win b{color:var(--green)}.score.lose b{color:var(--red)}
ol{list-style:none;margin:0;padding:8px 14px 14px;overflow:auto;max-height:420px;display:flex;flex-direction:column;gap:6px}
li{padding:6px 8px;border-radius:6px;background:#0d1117;border-left:3px solid var(--line);word-break:break-word}
li .k{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim);margin-right:6px}
li.atlas{border-color:var(--violet)}li.http{border-color:var(--blue)}li.choose,li.params{border-color:var(--amber)}
li.done,li.verify{border-color:var(--green)}li.error{border-color:var(--red)}li.action{border-color:var(--blue)}
.bottom{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px}@media (max-width:1000px){.bottom{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px}.card h3{margin:0;padding:12px 14px;font-size:14px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--dim);font-weight:500}.st-active{color:var(--green)}.st-candidate{color:var(--amber)}.st-retired{color:var(--dim)}
#feed{max-height:260px}
</style></head><body>
<header><h1>Learn once, reuse everywhere <small>· WebShop · MongoDB Atlas</small></h1>
<span id="p-webshop" class="pill">WebShop</span><span id="p-proxy" class="pill">Model proxy</span><span id="p-mongodb" class="pill">MongoDB</span></header>
<main>
<div class="controls">
  <label>Task <select id="task"></select></label>
  <button id="race" class="primary">Race on this task</button>
  <span style="flex:1"></span>
  <label>Teach from <select id="teachTask"></select></label>
  <button id="teach">Teach (1 recording → MongoDB)</button>
  <button id="forget" title="Mark all WebShop recipes retired (history kept)">Forget recipes</button>
</div>
<div class="lanes">
  <section class="lane" id="lane-teach"><h2>1 · Teach once <span class="sub">record → learn → save to MongoDB</span></h2>
    <div class="stats"><div class="stat"><b id="teach-time">0.0s</b>elapsed</div></div><ol id="teach-log"></ol></section>
  <section class="lane" id="lane-recipe"><h2>2 · Agent using MongoDB <span class="sub">finds recipe, replays API calls</span></h2>
    <div class="stats"><div class="stat"><b id="recipe-time">0.0s</b>elapsed</div><div class="stat"><b id="recipe-calls">0</b>model calls</div><div class="stat score" id="recipe-score"><b>–</b>score</div></div><ol id="recipe-log"></ol></section>
  <section class="lane" id="lane-browse"><h2>3 · Browsing agent <span class="sub">no memory, clicks page by page</span></h2>
    <div class="stats"><div class="stat"><b id="browse-time">0.0s</b>elapsed</div><div class="stat"><b id="browse-calls">0</b>model calls</div><div class="stat score" id="browse-score"><b>–</b>score</div></div><ol id="browse-log"></ol></section>
</div>
<div class="bottom">
  <div class="card"><h3>Recipes in MongoDB (shared by every agent)</h3><table><thead><tr><th>Recipe</th><th>v</th><th>Status</th><th>Uses</th><th>Wins</th><th>Lift</th></tr></thead><tbody id="recipes"></tbody></table></div>
  <div class="card"><h3>MongoDB live · change stream on <code>skills</code></h3><ol id="feed"></ol></div>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
const timers={}, calls={recipe:0,browse:0};
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function add(list,kind,text){const li=document.createElement('li');li.className=kind;li.innerHTML=`<span class="k">${esc(kind)}</span>${esc(text)}`;$(list).appendChild(li);$(list).scrollTop=1e9}
function startTimer(l){const t0=Date.now();clearInterval(timers[l]);timers[l]=setInterval(()=>{$(l+'-time').textContent=((Date.now()-t0)/1000).toFixed(1)+'s'},100)}
function stopTimer(l,sec){clearInterval(timers[l]);if(sec!=null)$(l+'-time').textContent=sec.toFixed(1)+'s'}
async function refresh(){try{const s=await (await fetch('/api/status')).json();
 for(const k of ['webshop','proxy','mongodb']){$('p-'+k).className='pill '+(s[k]?'ok':'bad')}
 $('recipes').innerHTML=s.recipes.length?s.recipes.map(r=>`<tr><td title="${esc(r.description||'')}">${esc(r.name)}</td><td>${r.version}</td><td class="st-${r.status}">${r.status}</td><td>${r.uses||0}</td><td>${r.wins||0}</td><td>${r.lift==null?'–':Number(r.lift).toFixed(2)}</td></tr>`).join(''):'<tr><td colspan="6" style="color:var(--dim)">No recipes yet. Teach one.</td></tr>';
}catch(e){}}
async function loadTasks(){const t=await (await fetch('/api/tasks')).json();
 const opts=Object.entries(t).map(([n,txt])=>`<option value="${n}">fixed_${n} · ${esc(txt).slice(0,90)}</option>`).join('');
 $('task').innerHTML=opts;$('teachTask').innerHTML=opts;$('teachTask').value='12';$('task').value='3'}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});if(r.status===409)alert('Already running');return r}
$('race').onclick=()=>{for(const l of ['recipe','browse']){$(l+'-log').innerHTML='';calls[l]=0;$(l+'-calls').textContent='0';$(l+'-score').className='stat score';$(l+'-score').querySelector('b').textContent='–';startTimer(l)}post('/api/race',{task:+$('task').value})};
$('teach').onclick=()=>{$('teach-log').innerHTML='';startTimer('teach');post('/api/teach',{task:+$('teachTask').value})};
$('forget').onclick=async()=>{const r=await (await post('/api/forget')).json();add('feed','atlas',`Retired ${r.retired} recipe(s) for the demo`);refresh()};
const es=new EventSource('/events');
es.onmessage=m=>{const e=JSON.parse(m.data);
 if(e.lane==='system'){if(e.kind==='idle'&&e.job==='teach')stopTimer('teach');return}
 if(e.lane==='atlas'){add('feed','atlas',e.text);refresh();return}
 const l=e.lane; if(!['teach','recipe','browse'].includes(l))return;
 add(l+'-log',e.kind,e.text);
 if(e.kind==='atlas')add('feed','atlas',`[${l}] ${e.text}`);
 if(l!=='teach'&&(e.kind==='params'||e.kind==='choose'||e.kind==='action')){calls[l]++;$(l+'-calls').textContent=calls[l]}
 if(e.kind==='done'&&e.row){stopTimer(l,e.row.seconds);$(l+'-calls').textContent=e.row.llm_calls;const s=$(l+'-score');s.querySelector('b').textContent=e.row.reward.toFixed(2);s.className='stat score '+(e.row.reward>=0.99?'win':'lose');refresh()}
 if(e.kind==='atlas'&&l==='teach'){refresh()}};
loadTasks();refresh();setInterval(refresh,10000);
</script></body></html>"""


def main() -> None:
    threading.Thread(target=watch_mongodb, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Live demo on http://{HOST}:{PORT}  (WebShop :3000, proxy :18080, MongoDB from .env)")
    server.serve_forever()


if __name__ == "__main__":
    main()
