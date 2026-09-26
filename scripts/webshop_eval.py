"""WebShop eval: a learned API recipe vs. a model browsing page by page.

WebShop (princeton-nlp/WebShop) runs locally at http://127.0.0.1:3000 and
scores every purchase from 0 to 1 against the task's hidden goal. We never
grade ourselves.

    # 1. learn once from one recorded purchase (headless browser, no desktop)
    uv run python scripts/webshop_eval.py --learn
    # 2. evaluate on held-out tasks
    uv run python scripts/webshop_eval.py --arms recipe,baseline

Standard WebShop split (seeded goal order, see baseline_models/env.py):
test = fixed_0..fixed_499, dev = 500..1499, train = 1500+. The recipe is
learned from ONE training task (default fixed_1500) and evaluated on test.

Arms:
  recipe   : fill_params (1 model call) + replay the learned recipe over HTTP;
             its choose steps pick the product and options (1 small call each).
  baseline : a model sees each page as text plus numbered actions and picks
             search/click until it buys (one call per page), no memory.

Recordings, the learned recipe and results go to .recordings/ (gitignored).
Needs the Codex proxy (npx codex-as-api) for model calls.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener

from recursive_computer_use.network.learner import (
    DEFAULT_MODEL,
    _complete,
    _parse_json,
    default_client,
    fill_params,
    learn_recipe,
    llm_chooser,
)
from recursive_computer_use.network.recipe import Extract, Recipe
from recursive_computer_use.network.runner import run_recipe

BASE = "http://127.0.0.1:3000"
SITE = "127.0.0.1:3000"
OUT = Path(".recordings")
RECIPE_PATH = OUT / "webshop_recipe.json"
DEMO_HAR = OUT / "webshop_demo.har"
REWARD_RE = r'id="reward">[^<]*<pre>\s*([0-9.eE+-]+)\s*</pre>'
# A replay scoring at least this counts as a verified success for the recipe's
# lifecycle (promote / fail streak). The WebShop score itself is kept as is.
RECIPE_OK_REWARD = 0.5
MAX_BASELINE_STEPS = 15
PAGE_CHARS = 6000

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


Event = Callable[[dict[str, Any]], None]


def _emit(on_event: Event | None, kind: str, text: str, **extra: Any) -> None:
    """Report progress: to *on_event* (the live demo page) or to stdout."""
    if on_event is None:
        print(f"  [{kind}] {text}", flush=True)
    else:
        on_event({"kind": kind, "text": text, "t": time.time(), **extra})


def page_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", markup))).strip()


def instruction(session: str) -> str:
    with build_opener().open(f"{BASE}/{session}", timeout=30) as r:
        text = page_text(r.read().decode("utf-8", "replace"))
    m = re.search(r"Instruction:\s*(.*?)\s*Search\b", text)
    return m.group(1) if m else text[:300]


def task_text(session: str) -> str:
    return f"WebShop session id: {session}. Instruction: {instruction(session)}"


# -- learn ---------------------------------------------------------------------


def learn(learn_task: int = 1500, *, store: Any = None, on_event: Event | None = None) -> Recipe:
    """Record one purchase headless and learn a recipe from it.

    The recipe is cached in .recordings/ and, with *store* (a
    ``recipes.RecipeStore``), saved to MongoDB so every agent on the same
    database can find and reuse it.
    """
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    session = f"fixed_{learn_task}"
    task = task_text(session)
    _emit(on_event, "task", task)
    _emit(on_event, "record", "Recording one purchase in a headless browser (network traffic -> HAR)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(record_har_path=str(DEMO_HAR), record_har_content="embed")
        page = ctx.new_page()
        page.goto(f"{BASE}/{session}")
        words = re.sub(r"[^a-z0-9 ]", " ", task.split("Instruction:", 1)[1].lower()).split()
        page.fill("#search_input", " ".join(w for w in words if len(w) > 2)[:60])
        page.click("button[type=submit]")
        page.wait_for_url("**/search_results/**")
        page.click("a[href*='item_page']")
        page.wait_for_url("**/item_page/**")
        option = page.locator("input[type=radio]")
        if option.count():  # the radio is hidden; the page's JS navigates to data-url
            page.goto(urljoin(BASE, option.first.get_attribute("data-url")))
        page.click("button.purchase")
        page.wait_for_url("**/done/**")
        reward = page.inner_text("#reward")
        ctx.close()
        browser.close()
    _emit(on_event, "record", "Demo purchase done: " + _WS.sub(" ", reward))
    _emit(on_event, "learn", "Model reads the redacted traffic and writes an API recipe...")
    t = time.time()
    recipe = learn_recipe(DEMO_HAR, task, site=SITE, task_key="webshop")
    _emit(on_event, "learn", f"Recipe learned in {time.time() - t:.1f}s", recipe=recipe.to_dict())
    for step in recipe.steps:
        chooses = ", ".join(f"choose {c.var}" for c in step.choose)
        _emit(on_event, "step", f"{step.method} {step.url.split(SITE, 1)[-1]}" + (f"  ({chooses})" if chooses else ""))
    RECIPE_PATH.write_text(json.dumps(recipe.to_dict(), indent=1))
    if store is not None:
        recipe.id = store.save_candidate(recipe)
        _emit(on_event, "atlas", f"Saved to MongoDB as {recipe.name} v{recipe.version} ({recipe.status})",
              recipe_id=str(recipe.id))
    return recipe


# -- arms ----------------------------------------------------------------------


def run_recipe_arm(
    recipe: Recipe, session: str, client: Any, on_event: Event | None = None, *, quiet: bool = True
) -> dict[str, Any]:
    stats = {"llm_calls": 0}
    t = time.time()
    r = copy.deepcopy(recipe)
    r.steps[-1].extract.append(Extract(var="__reward", source="regex", path=REWARD_RE))  # grader side
    emit = on_event if (on_event or not quiet) else (lambda _e: None)
    base_chooser = llm_chooser(client=client, stats=stats)

    def chooser(task_: str, ch: Any, candidates: list[dict[str, Any]]) -> Any:
        pick = base_chooser(task_, ch, candidates)
        if ch.mode == "one" and isinstance(pick, int) and 0 <= pick < len(candidates):
            _emit(emit, "choose", f"Picked {candidates[pick]['value']}: {candidates[pick]['context'][:90]}")
        elif ch.mode == "per_name":
            _emit(emit, "choose", f"Options: {json.dumps(pick) if pick else 'none'}")
        return pick

    def on_step(step: Any, chosen: dict[str, Any]) -> None:
        _emit(emit, "http", f"{step.id}: HTTP {step.status} in {step.ms} ms")

    try:
        task = task_text(session)
        _emit(emit, "task", task)
        params = fill_params(r, task, client=client)
        stats["llm_calls"] += 1
        _emit(emit, "params", "Params: " + ", ".join(f"{k}={v!r}" for k, v in params.items()))
        result = run_recipe(r, params, session_cookies={}, chooser=chooser, task=task, on_step=on_step)
        reward = float(result.vars.get("__reward", 0.0)) if result.ok or "__reward" in result.vars else 0.0
        error = None if "__reward" in result.vars else (result.error or "no reward")
    except Exception as exc:  # noqa: BLE001 - an eval row, not a crash
        reward, error = 0.0, repr(exc)[:200]
    row = {"arm": "recipe", "session": session, "reward": reward, "seconds": time.time() - t,
           "llm_calls": stats["llm_calls"], "error": error}
    _emit(emit, "done", f"Score {reward:.2f} in {row['seconds']:.1f}s, {row['llm_calls']} model calls"
          + (f" ({error})" if error else ""), row=row)
    return row


def atlas_store() -> Any:
    """RecipeStore on the shared MongoDB database (MONGODB_URI / MONGODB_DB)."""
    import os

    from dotenv import load_dotenv
    from pymongo import MongoClient

    from recursive_computer_use.recipes import RecipeStore

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
    return RecipeStore(client[os.environ.get("MONGODB_DB", "recursive_computer_use")])


def run_from_atlas(store: Any, session: str, client: Any, on_event: Event | None = None,
                   *, quiet: bool = True) -> dict[str, Any]:
    """What any agent on the shared database does: find a recipe, use it, report back."""
    emit = on_event if (on_event or not quiet) else (lambda _e: None)
    t = time.time()
    need = instruction(session)
    found = store.find_for_task(need, site=SITE, limit=1)
    if not found:
        _emit(emit, "atlas", "No recipe for this task in MongoDB yet. Teach one first.")
        row = {"arm": "recipe", "session": session, "reward": 0.0, "seconds": time.time() - t,
               "llm_calls": 0, "error": "no recipe in Atlas"}
        _emit(emit, "done", "No recipe", row=row)
        return row
    recipe = found[0]
    doc = store.skills.find_one({"_id": recipe.id}, {"uses": 1, "wins": 1, "lift": 1}) or {}
    _emit(emit, "atlas", f"Found in MongoDB by meaning: {recipe.name} v{recipe.version} ({recipe.status}), "
          f"used {doc.get('uses', 0)}x, won {doc.get('wins', 0)}x", recipe_id=str(recipe.id))
    row = run_recipe_arm(recipe, session, client, emit)
    status = store.record_result(recipe.id, ok=row["reward"] >= RECIPE_OK_REWARD,
                                 run_ms=int(row["seconds"] * 1000), steps=[{}] * len(recipe.steps),
                                 task=need, llm_calls=row["llm_calls"])
    _emit(emit, "atlas", f"Result written to MongoDB: episode + uses/wins/lift; recipe is now {status}")
    return row


BASELINE_SYSTEM = """You shop on a web store to satisfy an instruction. Each turn you
see the page text and numbered actions. Reply with one JSON object:
{"action": "search", "query": "..."} or {"action": "click", "n": <number>}.
Buy (click "Buy Now") once you are on the best matching product with the
right options selected. Be efficient."""


def run_baseline_arm(session: str, client: Any, on_event: Event | None = None, *, quiet: bool = True) -> dict[str, Any]:
    emit = on_event if (on_event or not quiet) else (lambda _e: None)
    t = time.time()
    opener = build_opener(HTTPCookieProcessor())
    url, data = f"{BASE}/{session}", None
    history: list[str] = []
    calls, reward, error = 0, 0.0, None
    try:
        task = task_text(session)
        need = task.split("Instruction:", 1)[-1].strip()
        _emit(emit, "task", task)
        for _ in range(MAX_BASELINE_STEPS):
            with opener.open(Request(url, data=data), timeout=30) as resp:
                url, markup = resp.geturl(), resp.read().decode("utf-8", "replace")
            m = re.search(REWARD_RE, markup)
            if m:
                reward = float(m.group(1))
                break
            actions = _actions(markup, url)
            listing = "\n".join(f"{i}: {a['label']}" for i, a in enumerate(actions))
            prompt = {"instruction": task, "history": history[-6:], "page": page_text(markup)[:PAGE_CHARS],
                      "actions": listing, "can_search": 'name="search_query"' in markup}
            reply = _complete(client, DEFAULT_MODEL, [
                {"role": "system", "content": BASELINE_SYSTEM},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ])
            calls += 1
            try:
                act = _parse_json(reply)
            except ValueError:
                act = {"action": "click", "n": 0}
            can_search = 'name="search_query"' in markup
            if act.get("action") != "search" and can_search and not actions:
                act = {"action": "search", "query": act.get("query") or need}  # search is the only move here
            if act.get("action") == "search":
                q = str(act.get("query", ""))[:200]
                history.append(f"search[{q}]")
                _emit(emit, "action", f"search[{q}]")
                url, data = f"{BASE}/{session}", urlencode({"search_query": q}).encode()
                continue
            n = int(act.get("n", 0)) if str(act.get("n", "0")).isdigit() else 0
            a = actions[n] if 0 <= n < len(actions) else (actions[0] if actions else None)
            if a is None:
                error = "no actions"
                break
            history.append(f"click[{a['label']}]")
            _emit(emit, "action", f"click[{a['label']}]")
            url, data = a["url"], (b"" if a["method"] == "POST" else None)
        else:
            error = "step limit"
    except Exception as exc:  # noqa: BLE001
        error = repr(exc)[:200]
    row = {"arm": "baseline", "session": session, "reward": reward, "seconds": time.time() - t,
           "llm_calls": calls, "error": error, "trace": history}
    _emit(emit, "done", f"Score {reward:.2f} in {row['seconds']:.1f}s, {calls} model calls"
          + (f" ({error})" if error else ""), row=row)
    return row


def _actions(markup: str, url: str) -> list[dict[str, str]]:
    """Clickable things on a WebShop page: links, option radios, form buttons."""
    out: list[dict[str, str]] = []
    for href, label in re.findall(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup):
        if href.startswith("#") or (href.startswith("http") and not href.startswith(BASE)):
            continue
        out.append({"label": page_text(label)[:100] or href, "url": urljoin(url, html.unescape(href)), "method": "GET"})
    for name, value, data_url in re.findall(
        r'(?is)<input[^>]*type="radio"[^>]*name="([^"]+)"[^>]*value="([^"]+)"[^>]*data-url="([^"]+)"', markup
    ):
        out.append({"label": f"select {name}: {html.unescape(value)}", "url": urljoin(url, html.unescape(data_url)), "method": "GET"})
    for action, label in re.findall(r'(?is)<form[^>]*action="([^"]+)"[^>]*>.*?<button[^>]*>(.*?)</button>', markup):
        text = page_text(label)
        if text.lower() != "search":
            out.append({"label": text[:60], "url": urljoin(url, html.unescape(action)), "method": "POST"})
    return out


# -- main ----------------------------------------------------------------------


def parse_range(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def arm_session(arm: str, task_index: int) -> str:
    """Give each arm its own server-side session for the same fixed task."""
    if not arm.isidentifier() or task_index < 0:
        raise ValueError("arm must be an identifier and task index must be non-negative")
    return f"{arm}_fixed_{task_index}"


def summarize(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'arm':9} {'n':>3} {'avg reward':>10} {'success':>8} {'sec/task':>9} {'LLM calls/task':>15} {'errors':>6}")
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm]
        print(f"{arm:9} {len(rs):>3} {statistics.mean(r['reward'] for r in rs):>10.3f} "
              f"{sum(r['reward'] >= 1.0 for r in rs) / len(rs):>8.0%} "
              f"{statistics.mean(r['seconds'] for r in rs):>9.1f} "
              f"{statistics.mean(r['llm_calls'] for r in rs):>15.1f} {sum(bool(r['error']) for r in rs):>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--learn", action="store_true", help="record the demo and learn the recipe")
    ap.add_argument("--tasks", default="0-499", help="fixed_N task range; default is the standard test split")
    ap.add_argument("--learn-task", type=int, default=1500, help="training task used for the one demonstration")
    ap.add_argument("--arms", default="recipe,baseline")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--atlas", action="store_true",
                    help="save the learned recipe to MongoDB / find it there per task and write results back")
    args = ap.parse_args()
    store = atlas_store() if args.atlas else None
    if args.learn:
        learn(args.learn_task, store=store)
        return
    recipe = None if store else Recipe.from_dict(json.loads(RECIPE_PATH.read_text())).validate()
    client = default_client()
    task_indices = [i for i in parse_range(args.tasks) if i != args.learn_task]
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    if not arms or any(arm not in {"recipe", "baseline"} for arm in arms):
        ap.error("--arms must be a comma-separated subset of recipe,baseline")

    def run(job: tuple[str, str]) -> dict[str, Any]:
        arm, s = job
        if arm == "baseline":
            row = run_baseline_arm(s, client)
        elif store is not None:
            row = run_from_atlas(store, s, client)
        else:
            row = run_recipe_arm(recipe, s, client)
        print(f"{arm:8} {s:9} reward={row['reward']:.2f} {row['seconds']:5.1f}s calls={row['llm_calls']}"
              + (f" err={row['error']}" if row["error"] else ""), flush=True)
        return row

    rows = []
    for arm in arms:
        jobs = [(arm, arm_session(arm, i)) for i in task_indices]
        with ThreadPoolExecutor(args.workers) as pool:
            rows.extend(pool.map(run, jobs))
    OUT.mkdir(exist_ok=True)
    path = OUT / f"webshop_eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"model": DEFAULT_MODEL, "rows": rows}, indent=1))
    summarize(rows)
    print(f"\nrows saved to {path}")


if __name__ == "__main__":
    sys.exit(main())
