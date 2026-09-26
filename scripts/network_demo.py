"""Run the network-learning demo against a disposable Atlas database.

The two capture phases launch headed Chromium. By default a person performs
the task in each window; pass ``--agent`` only when computer use is intended.
The HAR stays under .recordings/ and rcu_demo is dropped during cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from recursive_computer_use.network.capture import record
from recursive_computer_use.network.learner import learn_recipe
from recursive_computer_use.network.runner import run_recipe
from recursive_computer_use.recipes import RecipeStore
from recursive_computer_use.schema import ensure_schema
from recursive_computer_use.store import DEFAULT_URI


DB_NAME = "rcu_demo"
TASK_KEY = "local-checkin"
SITE = "127.0.0.1:8765"
BASE = f"http://{SITE}"


def _api(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read(64 * 1024))


def _params(recipe: Any, guest: dict[str, str]) -> dict[str, str]:
    aliases = {
        "name": "full_name",
        "guest_name": "full_name",
        "full_name": "full_name",
        "email": "email",
        "city": "city",
    }
    values = {}
    for param in recipe.params:
        key = aliases.get(param.name.lower())
        if key is None:
            raise ValueError(f"demo cannot map learned param {param.name!r}")
        values[param.name] = guest[key]
    return values


def _timed(label: str, result: Any) -> None:
    print(f"  {label}: {'PASS' if result.ok else 'FAIL'} in {result.duration_ms} ms")
    if result.error:
        print(f"    {result.error}")


def _capture(task: str, *, agent: bool, suffix: str) -> Any:
    print(f"\n{task}")
    print("  A headed Chromium window will open. Complete the task there.")
    return record(
        f"{BASE}/",
        task=task,
        har_path=ROOT / ".recordings" / f"network-demo-{suffix}-{uuid4().hex}.har",
        agent_prompt=task if agent else None,
        timeout_s=300,
    )


def run(agent: bool = False) -> None:
    load_dotenv(ROOT / ".env")
    os.environ["MONGODB_DB"] = DB_NAME  # capture metadata is disposable too
    client = MongoClient(os.environ.get("MONGODB_URI", DEFAULT_URI), serverSelectionTimeoutMS=5000)
    server = None
    try:
        client.admin.command("ping")
        client.drop_database(DB_NAME)
        db = client[DB_NAME]
        ensure_schema(db)

        from http.server import ThreadingHTTPServer

        from demo import app

        app.STATE = app.fresh_state()
        app.REDESIGN_ON = False
        server = ThreadingHTTPServer((app.HOST, app.PORT), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        store = RecipeStore(db)

        print("1. Reset and learn one recorded flow")
        first = {"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"}
        _api("/api/reset", {"guest": first})
        first_task = f"Check in {first['full_name']}, email {first['email']}, city {first['city']}."
        capture = _capture(first_task, agent=agent, suffix="v1")
        if not capture.ok:
            raise RuntimeError("first recording did not pass the verifier")
        recipe = learn_recipe(capture.har_path, first_task, site=SITE, task_key=TASK_KEY)
        recipe_id = store.save_candidate(recipe, recording_id=capture.recording_id)
        print(f"  Learned candidate {recipe.name} v{recipe.version}.")

        print("\n2. Replay for three new guests")
        guests = [
            {"full_name": "Grace Hopper", "email": "grace@example.net", "city": "Arlington"},
            {"full_name": "Katherine Johnson", "email": "kj@example.org", "city": "Hampton"},
            {"full_name": "Alan Turing", "email": "alan@example.com", "city": "Manchester"},
        ]
        for guest in guests:
            _api("/api/reset", {"guest": guest})
            started = time.monotonic()
            result = run_recipe(recipe, _params(recipe, guest))
            wall_ms = int((time.monotonic() - started) * 1000)
            _timed(guest["full_name"], result)
            print(f"    wall time {wall_ms} ms; API steps {len(result.steps)}; model calls 0")
            store.record_result(
                recipe_id,
                ok=result.ok,
                run_ms=result.duration_ms,
                steps=[{"id": step.id, "status": step.status, "ms": step.ms} for step in result.steps],
                task=f"Check in {guest['full_name']}",
                llm_calls=0,
            )
            if not result.ok:
                raise RuntimeError(f"learned recipe failed for {guest['full_name']}")

        print("\n3. Flip redesign and observe the old recipe fail")
        _api("/api/redesign", {"on": True})
        redesign_guest = {"full_name": "Dorothy Vaughan", "email": "dorothy@example.org", "city": "Newport News"}
        _api("/api/reset", {"guest": redesign_guest})
        started = time.monotonic()
        failed = run_recipe(recipe, _params(recipe, redesign_guest))
        print(f"  Old recipe: {'unexpected PASS' if failed.ok else 'expected FAIL'} in {int((time.monotonic()-started)*1000)} ms")
        store.record_result(
            recipe_id,
            ok=failed.ok,
            run_ms=failed.duration_ms,
            steps=[{"id": step.id, "status": step.status, "ms": step.ms} for step in failed.steps],
            task="Check in after API redesign",
            llm_calls=0,
        )
        if failed.ok:
            raise AssertionError("old recipe unexpectedly survived redesign")

        print("\n4. Record the redesigned task, supersede, and verify v2")
        new_task = f"Check in {redesign_guest['full_name']}, email {redesign_guest['email']}, city {redesign_guest['city']}."
        capture_v2 = _capture(new_task, agent=agent, suffix="v2")
        if not capture_v2.ok:
            raise RuntimeError("redesigned recording did not pass the verifier")
        recipe_v2 = learn_recipe(capture_v2.har_path, new_task, site=SITE, task_key=TASK_KEY)
        recipe_v2_id = store.supersede(recipe_id, recipe_v2)
        final_guest = {"full_name": "Mary Jackson", "email": "mary@example.net", "city": "Hampton"}
        _api("/api/reset", {"guest": final_guest})
        started = time.monotonic()
        final = run_recipe(recipe_v2, _params(recipe_v2, final_guest))
        _timed(f"v{recipe_v2.version} {final_guest['full_name']}", final)
        print(f"    wall time {int((time.monotonic()-started)*1000)} ms; model calls 0")
        store.record_result(
            recipe_v2_id,
            ok=final.ok,
            run_ms=final.duration_ms,
            steps=[{"id": step.id, "status": step.status, "ms": step.ms} for step in final.steps],
            task=f"Check in {final_guest['full_name']}",
            llm_calls=0,
        )
        if not final.ok:
            raise RuntimeError("version 2 failed its verifier")
        print("\nDemo complete: API replay survived new params and healed to version 2.")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        client.drop_database(DB_NAME)
        client.close()
        print(f"Cleaned up scratch database {DB_NAME}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", action="store_true", help="Opt in to computer use for both capture phases.")
    args = parser.parse_args()
    try:
        run(agent=args.agent)
    except (HTTPError, OSError, RuntimeError, TimeoutError, AssertionError, ValueError) as exc:
        print(f"Network demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
