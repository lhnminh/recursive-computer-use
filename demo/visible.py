"""Visible-browser lanes for the live demo: watch agents use the real WebShop site.

Each lane gets its own Chromium window (Playwright, headed), placed side by
side. Playwright drives the page directly; it does not move the user's mouse
or type on their keyboard.

- :func:`browse_visible` is the memoryless browsing agent: every turn the
  model reads the page and picks search/click, and the window really types,
  opens products, selects options and presses "Buy Now".
- :func:`show_recipe_path` shows what the recipe agent did: after its API
  replay (milliseconds), the window walks through the same pages it hit
  (results, product with the chosen options, purchase with its score).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable
from urllib.parse import quote

from scripts import webshop_eval as ws

Event = Callable[[dict[str, Any]], None]
_ASIN = re.compile(r"\b(B[0-9A-Z]{9})\b")


def screen_size() -> tuple[int, int]:
    """Logical size of the main display (macOS), with a safe default."""
    try:
        out = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        x0, y0, x1, y1 = (int(v) for v in out.strip().split(", "))
        return x1 - x0, y1 - y0
    except Exception:  # noqa: BLE001
        return 1440, 900


def slot(index: int, count: int = 2) -> tuple[int, int, int, int]:
    """Window rectangle (x, y, w, h) for lane *index* of *count*, side by side."""
    w, h = screen_size()
    width = w // count
    return index * width, 25, width, h - 25


def split_slot(index: int, left_share: float = 0.4) -> tuple[int, int, int, int]:
    """Two unequal side-by-side windows: index 0 gets *left_share* of the width."""
    w, h = screen_size()
    left = int(w * left_share)
    return (0, 25, left, h - 25) if index == 0 else (left, 25, w - left, h - 25)


def bring_to_front(page: Any = None) -> None:
    """macOS opens the automation browser behind other windows; raise it."""
    try:
        if page is not None:
            page.bring_to_front()
        subprocess.Popen(["osascript", "-e", 'tell application "Google Chrome for Testing" to activate'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def open_window(pw: Any, rect: tuple[int, int, int, int], *, slow_mo: int = 0, zoom: float = 0.75) -> tuple[Any, Any]:
    """A headed window at *rect*. *zoom* < 1 keeps sites in their desktop layout at half-screen width."""
    x, y, w, h = rect
    browser = pw.chromium.launch(
        headless=False, slow_mo=slow_mo,
        args=[f"--window-position={x},{y}", f"--window-size={w},{h}", f"--force-device-scale-factor={zoom}"],
    )
    page = browser.new_context(no_viewport=True).new_page()
    bring_to_front(page)
    return browser, page


def banner(page: Any, text: str, color: str = "#1f6feb") -> None:
    """Pin a label on top of the page so viewers know which agent this window is."""
    try:
        page.evaluate(
            """([text, color]) => {
                let b = document.getElementById('__agent_banner');
                if (!b) { b = document.createElement('div'); b.id = '__agent_banner'; document.body.prepend(b); }
                b.textContent = text;
                b.style.cssText = `position:sticky;top:0;z-index:99999;padding:10px 14px;font:600 15px system-ui;
                  color:#fff;background:${color};box-shadow:0 2px 8px #0005`;
            }""",
            [text, color],
        )
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


# -- recipe lane -------------------------------------------------------------------


def _search_param(params: dict[str, str]) -> str | None:
    for name, value in params.items():
        if "search" in name or "query" in name or "keyword" in name:
            return value
    return None


def show_recipe_path(page: Any, session: str, row: dict[str, Any], *, pause: float = 1.2) -> None:
    """Walk the window through the pages the API replay went through."""
    base = ws.BASE
    label = f"Agent using MongoDB · replayed via API in {row['seconds']:.1f}s · {row['llm_calls']} model calls"
    query = _search_param(row.get("params") or {})
    chosen = row.get("chosen") or {}
    asin = next((m.group(1) for v in chosen.values() if isinstance(v, str) for m in [_ASIN.search(v)] if m), None)
    options = next((v for v in chosen.values() if isinstance(v, str) and v.startswith("{")), "{}")
    keywords = str(query.lower().split(" ")) if query else "[]"
    pages = [f"{base}/{session}"]
    if query:
        pages.append(f"{base}/search_results/{session}/{quote(keywords)}/1")
    if asin:
        pages.append(f"{base}/item_page/{session}/{asin}/{quote(keywords)}/1/{quote(options)}")
        pages.append(f"{base}/done/{session}/{asin}/{quote(options)}")
    for url in pages:
        page.goto(url)
        banner(page, label, "#238636" if row["reward"] >= 0.99 else "#9e6a03")
        time.sleep(pause)


# -- browsing lane -----------------------------------------------------------------


def browse_visible(page: Any, session: str, client: Any, on_event: Event | None = None) -> dict[str, Any]:
    """The memoryless browsing agent, acting in a visible window."""
    emit = on_event or (lambda _e: None)
    t = time.time()
    history: list[str] = []
    calls, reward, error = 0, 0.0, None
    try:
        task = ws.task_text(session)
        need = task.split("Instruction:", 1)[-1].strip()
        ws._emit(emit, "task", task)
        page.goto(f"{ws.BASE}/{session}")
        for _ in range(ws.MAX_BASELINE_STEPS):
            banner(page, f"Browsing agent · no memory · {calls} model calls · {time.time() - t:.0f}s", "#6e40c9")
            markup = page.content()
            m = re.search(ws.REWARD_RE, markup)
            if m:
                reward = float(m.group(1))
                break
            actions = ws._actions(markup, page.url)
            listing = "\n".join(f"{i}: {a['label']}" for i, a in enumerate(actions))
            can_search = page.locator("#search_input").count() > 0
            prompt = {"instruction": task, "history": history[-6:], "page": ws.page_text(markup)[: ws.PAGE_CHARS],
                      "actions": listing, "can_search": can_search}
            reply = ws._complete(client, ws.DEFAULT_MODEL, [
                {"role": "system", "content": ws.BASELINE_SYSTEM},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ])
            calls += 1
            try:
                act = ws._parse_json(reply)
            except ValueError:
                act = {"action": "click", "n": 0}
            if act.get("action") != "search" and can_search and not actions:
                act = {"action": "search", "query": act.get("query") or need}
            if act.get("action") == "search" and can_search:
                q = str(act.get("query", ""))[:200]
                history.append(f"search[{q}]")
                ws._emit(emit, "action", f"search[{q}]")
                page.fill("#search_input", q)
                page.click("button[type=submit]")
                page.wait_for_load_state()
                continue
            n = int(act.get("n", 0)) if str(act.get("n", "0")).isdigit() else 0
            a = actions[n] if 0 <= n < len(actions) else (actions[0] if actions else None)
            if a is None:
                error = "no actions"
                break
            history.append(f"click[{a['label']}]")
            ws._emit(emit, "action", f"click[{a['label']}]")
            if a["method"] == "POST":
                page.get_by_role("button", name=a["label"]).first.click()
            else:  # links and option radios (the radio's own JS navigates to its data-url)
                page.goto(a["url"])
            page.wait_for_load_state()
        else:
            error = "step limit"
    except Exception as exc:  # noqa: BLE001
        error = repr(exc)[:200]
    row = {"arm": "baseline", "session": session, "reward": reward, "seconds": time.time() - t,
           "llm_calls": calls, "error": error, "trace": history}
    banner(page, f"Browsing agent · score {reward:.2f} · {row['seconds']:.1f}s · {calls} model calls",
           "#238636" if reward >= 0.99 else "#da3633")
    ws._emit(emit, "done", f"Score {reward:.2f} in {row['seconds']:.1f}s, {calls} model calls"
             + (f" ({error})" if error else ""), row=row)
    return row
