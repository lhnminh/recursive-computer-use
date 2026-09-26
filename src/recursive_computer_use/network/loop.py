"""Recipe-first task execution with recorded computer-use fallback and healing."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pymongo import MongoClient

from ..recipes import RecipeStore
from .learner import fill_params, learn_recipe
from .runner import run_recipe


def do_task(
    task: str,
    *,
    site: str,
    task_key: str,
    store: Any = None,
    capture_fn: Callable[..., Any] | None = None,
    learn_fn: Callable[..., Any] = learn_recipe,
    fill_fn: Callable[..., Any] = fill_params,
    runner_fn: Callable[..., Any] = run_recipe,
) -> dict[str, Any]:
    """Try a stored recipe, then teach and replay once when needed.

    Optional collaborators make the orchestration testable without Atlas,
    browser launch, or model calls. Runtime fallback uses a local headless
    Playwright browser; pass ``capture_fn=network.capture.record`` to record a
    headed human/computer-use session instead.
    """
    site_host = _normalize_site(site)
    url = site if urlsplit(site).scheme else f"http://{site_host}/"
    store = store or _default_store()
    timings: dict[str, int] = {}
    events: list[dict[str, Any]] = []

    started = time.monotonic()
    recipes = store.find_for_task(task, site=site_host, limit=3)
    timings["lookup_ms"] = _elapsed(started)
    current = recipes[0] if recipes else None
    old_id = getattr(current, "id", None) if current else None
    if current:
        started = time.monotonic()
        try:
            params = fill_fn(current, task)
            filled = True
        except Exception as exc:
            params = {}
            filled = False
            events.append({"event": "fill_params_failed", "error": str(exc)})
        timings["fill_params_ms"] = _elapsed(started)
        if filled:
            started = time.monotonic()
            replay = runner_fn(current, params)
            timings["recipe_run_ms"] = _elapsed(started)
            run_result = _as_dict(replay)
            events.append({"event": "recipe_replay", "recipe_id": str(old_id), **run_result})
            started = time.monotonic()
            status = store.record_result(
                old_id,
                ok=bool(run_result.get("ok")),
                run_ms=int(run_result.get("duration_ms", timings["recipe_run_ms"])),
                steps=run_result.get("steps", ()),
                task=task,
                llm_calls=1,
            )
            timings["record_result_ms"] = _elapsed(started)
            events[-1]["status"] = status
            if run_result.get("ok"):
                return _response(True, "recipe", events, timings, old_id)
        else:
            run_result = {"ok": False, "error": "parameter extraction failed", "steps": []}

    # No usable recipe or replay failure: perform the task once while recording.
    if capture_fn is None:
        from .browser_agent import record_headless

        capture_fn = record_headless
    started = time.monotonic()
    recording_path = Path(".recordings") / f"{uuid.uuid4().hex}.har"
    captured = capture_fn(
        url,
        task=task,
        har_path=recording_path,
        agent_prompt=task,
    )
    timings["capture_ms"] = _elapsed(started)
    capture_result = _as_dict(captured)
    events.append({"event": "capture", **capture_result})
    har_path = getattr(captured, "har_path", capture_result.get("har_path", recording_path))
    if not capture_result.get("ok"):
        events.append({"event": "learning_skipped", "reason": "capture was not verified successful"})
        return _response(False, "fallback_failed", events, timings, old_id)

    started = time.monotonic()
    learned = learn_fn(har_path, task, site=site_host, task_key=task_key)
    timings["learn_ms"] = _elapsed(started)
    if old_id is not None:
        new_id = store.supersede(old_id, learned)
        action = "supersede"
    else:
        new_id = store.save_candidate(learned, recording_id=getattr(captured, "recording_id", None))
        action = "save_candidate"
    events.append({"event": action, "recipe_id": str(new_id), "version": learned.version})

    started = time.monotonic()
    try:
        params = fill_fn(learned, task)
    except Exception as exc:
        timings["fill_params_ms"] = _elapsed(started)
        events.append({"event": "fill_params_failed", "error": str(exc)})
        return _response(False, "fallback_failed", events, timings, new_id)
    timings["fill_params_ms"] = _elapsed(started)
    started = time.monotonic()
    replay = runner_fn(learned, params)
    timings["relearn_replay_ms"] = _elapsed(started)
    run_result = _as_dict(replay)
    events.append({"event": "relearn_replay", "recipe_id": str(new_id), **run_result})
    started = time.monotonic()
    status = store.record_result(
        new_id,
        ok=bool(run_result.get("ok")),
        run_ms=int(run_result.get("duration_ms", timings["relearn_replay_ms"])),
        steps=run_result.get("steps", ()),
        task=task,
        llm_calls=1,
    )
    timings["record_result_ms"] = timings.get("record_result_ms", 0) + _elapsed(started)
    events[-1]["status"] = status
    return _response(bool(run_result.get("ok")), "relearned_recipe", events, timings, new_id)


def _normalize_site(site: str) -> str:
    parts = urlsplit(site if "://" in site else f"http://{site}")
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"}:
        raise ValueError("site must be a host[:port] or its HTTP URL")
    return parts.netloc.lower()


def _default_store() -> RecipeStore:
    load_dotenv()
    uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGODB_DB", "recursive_computer_use")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    return RecipeStore(client[db_name])


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        result = dict(value)
        result.pop("vars", None)
        return result
    if is_dataclass(value):
        result = asdict(value)
        result.pop("vars", None)
        return result
    return {
        key: getattr(value, key)
        for key in ("ok", "steps", "error", "duration_ms", "verifier_result", "har_path", "recording_id", "source")
        if hasattr(value, key)
    }


def _response(ok: bool, path: str, events: list[dict[str, Any]], timings: dict[str, int], recipe_id: Any) -> dict[str, Any]:
    return {
        "ok": ok,
        "path": path,
        "recipe_id": str(recipe_id) if recipe_id is not None else None,
        "events": events,
        "timings_ms": timings,
        "total_ms": sum(timings.values()),
    }
