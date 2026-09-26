"""Record a task in headed Chromium and save its network trace as a local HAR.

Install the browser once with ``uv run playwright install chromium``. The HAR
is sensitive and remains at the caller's path; Atlas receives only its hash,
redacted task summary, and endpoint metadata.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from dotenv import load_dotenv
from pymongo import MongoClient

from .har import Redactor, endpoints, har_sha256, load_exchanges


@dataclass(frozen=True)
class CaptureResult:
    har_path: Path
    duration_ms: int
    verifier_result: dict[str, Any] | None
    ok: bool
    timed_out: bool
    source: str
    recording_id: Any = None


def record(
    url: str,
    *,
    task: str,
    har_path: str | Path,
    agent_prompt: str | None = None,
    timeout_s: float = 300,
) -> CaptureResult:
    """Capture a human or agent run in headed Chromium until verified or timed out.

    Set ``agent_prompt`` to opt into computer use. It requires the user's
    computer-use proxy and operates the visible browser window.
    """

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    target = Path(har_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    verify_url = _verifier_url(url)
    started = time.monotonic()
    source = "agent" if agent_prompt else "human"
    verifier_result: dict[str, Any] | None = None
    timed_out = False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - package dependency
        raise RuntimeError(
            "Playwright is required; install it with `uv add playwright` and "
            "run `uv run playwright install chromium`."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            record_har_path=str(target),
            record_har_mode="full",
            record_har_content="embed",
        )
        page = context.new_page()
        try:
            deadline = started + timeout_s
            remaining_ms = max(1, min(30_000, int((deadline - time.monotonic()) * 1000)))
            page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms)
            if agent_prompt:
                page.bring_to_front()
                verifier_result = _run_agent_until_verified(
                    agent_prompt, verify_url, deadline
                )
            else:
                while time.monotonic() < deadline:
                    verifier_result = _fetch_verifier(verify_url)
                    if verifier_result and verifier_result.get("success") is True:
                        break
                    if page.is_closed():
                        break
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            timed_out = not bool(verifier_result and verifier_result.get("success") is True)
        finally:
            context.close()  # flushes the HAR
            browser.close()

    duration_ms = int((time.monotonic() - started) * 1000)
    recording_id = None
    if target.is_file():
        redactor = Redactor()
        exchanges = load_exchanges(target, site=_site(url), redactor=redactor)
        metadata = {
            "site": _site(url),
            "task": f"Recorded workflow for {_site(url)}",
            "har_sha256": har_sha256(target),
            "exchange_count": len(exchanges),
            "endpoints": endpoints(exchanges),
            "source": source,
            "created_at": datetime.now(timezone.utc),
        }
        recording_id = _persist_metadata(metadata)

    return CaptureResult(
        har_path=target,
        duration_ms=duration_ms,
        verifier_result=_safe_verifier(verifier_result),
        ok=bool(verifier_result and verifier_result.get("success") is True),
        timed_out=timed_out,
        source=source,
        recording_id=recording_id,
    )


def _safe_verifier(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    allowed = {
        "success",
        "wrong_field_count",
        "wrong_click_count",
        "action_count",
        "policy_violations",
        "duration_ms",
    }
    return {key: payload[key] for key in allowed if key in payload}


def _site(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute HTTP or HTTPS URL")
    return parts.netloc.lower()


def _verifier_url(url: str) -> str:
    parts = urlsplit(url)
    _site(url)
    return urlunsplit((parts.scheme, parts.netloc, "/api/result", "", ""))


def _fetch_verifier(url: str) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=2) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(64 * 1024))
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, URLError):
        return None


def _run_agent_until_verified(
    prompt: str, verifier_url: str, deadline: float
) -> dict[str, Any] | None:
    """Run computer use in a killable worker so capture honors its deadline."""

    process = multiprocessing.get_context("spawn").Process(
        target=_agent_worker,
        args=(prompt, verifier_url),
        daemon=True,
    )
    process.start()
    result = None
    try:
        while time.monotonic() < deadline and process.is_alive():
            result = _fetch_verifier(verifier_url)
            if result and result.get("success") is True:
                process.join(timeout=1)
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        if not result or result.get("success") is not True:
            result = _fetch_verifier(verifier_url)
    finally:
        if process.is_alive():
            try:
                os.kill(process.pid, signal.SIGINT)
            except OSError:
                pass
            process.join(timeout=2)
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
    return result


def _agent_worker(prompt: str, verifier_url: str) -> None:
    from ..agent import run as run_agent

    run_agent(
        prompt,
        model="gpt-5.6-terra",
        verifier_url=verifier_url,
    )


def _persist_metadata(document: dict[str, Any]) -> Any:
    """Store recording metadata when MongoDB is configured; never the HAR."""

    load_dotenv()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        return None
    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=1500)
        db = client[os.environ.get("MONGODB_DB", "recursive_computer_use")]
        return db["recordings"].insert_one(document).inserted_id
    except Exception:
        return None
    finally:
        if client is not None:
            client.close()
