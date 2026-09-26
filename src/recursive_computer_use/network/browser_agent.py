"""Do a web task inside a Playwright page, with no screen, mouse or keyboard.

This is the programmatic teacher for the recipe loop. The model never sees
pixels. Each turn it gets the page's visible text and a list of interactive
elements with short IDs (``e1``, ``e2`` ...), and it answers with a batch of
actions on those IDs. Playwright performs them. A whole form can be filled
and submitted in one model turn.

``record_headless`` wraps the agent in a HAR recording, with the same
signature as ``capture.record``, so ``loop.do_task(capture_fn=...)`` and the
demo can learn recipes without a person or the desktop.

Rules:

- IDs are valid for one snapshot. An unknown ID stops the batch with an
  error; it never falls back to a nearby element.
- The model cannot navigate to a URL. It can act only on the page it has.
- Page text and values go to the model only. Nothing here writes to MongoDB
  except the redacted recording metadata that ``capture`` already writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capture import CaptureResult
from .learner import DEFAULT_MODEL, default_client

MAX_TURNS = 12
MAX_ACTIONS_PER_TURN = 12
MAX_ELEMENTS = 150
MAX_PAGE_TEXT = 3000
ACTION_TIMEOUT_MS = 5000

SYSTEM_PROMPT = (
    "You operate a web page through element IDs. Each observation lists the "
    "page URL, its visible text, and its interactive elements as "
    "`id | tag/type | label | value | flags`. Call `act` with a batch of "
    "actions using those IDs; batch everything you can see how to do, such as "
    "filling every field and then clicking submit. IDs change after every "
    "observation, so use only IDs from the latest one. Use the exact values "
    "the task or page asks for. When the task is done, or cannot be done, "
    "call `finish`."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Perform actions in order on elements from the latest observation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["click", "fill", "select", "check", "uncheck", "press"]},
                                "id": {"type": "string", "description": "Element id such as e3."},
                                "text": {"type": "string", "description": "Text for fill, option for select, key for press."},
                            },
                            "required": ["op", "id"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["actions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Stop. Report whether the task is complete.",
            "parameters": {
                "type": "object",
                "properties": {"success": {"type": "boolean"}, "summary": {"type": "string"}},
                "required": ["success", "summary"],
                "additionalProperties": False,
            },
        },
    },
]

# Tags every visible interactive element with data-rcu-id and describes it.
_SNAPSHOT_JS = """
(maxElements) => {
  const selector = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[contenteditable=true]';
  document.querySelectorAll('[data-rcu-id]').forEach(e => e.removeAttribute('data-rcu-id'));
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const labelOf = el => {
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) label = l.innerText;
    }
    if (!label && el.closest('label')) label = el.closest('label').innerText;
    if (!label) label = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
    if (!label && !['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) label = el.innerText;
    return clean(label);
  };
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (el.type === 'hidden') continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    const id = 'e' + (out.length + 1);
    el.setAttribute('data-rcu-id', id);
    let value = null;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && !['checkbox', 'radio', 'submit', 'button'].includes(el.type)) {
      value = el.type === 'password' ? (el.value ? '***' : '') : clean(el.value);
    }
    out.push({
      id, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
      label: labelOf(el), value,
      checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      focused: document.activeElement === el, disabled: !!el.disabled,
    });
    if (out.length >= maxElements) break;
  }
  const text = document.body ? document.body.innerText.replace(/\\n\\s*\\n+/g, '\\n') : '';
  return {url: location.href, title: document.title, elements: out, text};
}
"""


@dataclass
class BrowserAgentResult:
    ok: bool
    verified: bool
    summary: str
    turns: int
    llm_calls: int
    actions: int
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class HeadlessCaptureResult(CaptureResult):
    """``CaptureResult`` plus the browser agent's turns, calls and actions."""

    agent: BrowserAgentResult | None = None


def observe(page: Any) -> dict[str, Any]:
    """Snapshot the page and assign fresh element IDs."""
    return page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)


def render_observation(snapshot: dict[str, Any]) -> str:
    lines = [f"URL: {snapshot.get('url', '')}", f"Title: {snapshot.get('title', '')}", "", "Page text:"]
    lines.append((snapshot.get("text") or "")[:MAX_PAGE_TEXT])
    lines += ["", "Elements:"]
    for el in snapshot.get("elements", []):
        flags = [name for name in ("focused", "disabled") if el.get(name)]
        if el.get("checked") is not None:
            flags.append("checked" if el["checked"] else "unchecked")
        value = "" if el.get("value") is None else repr(el["value"])
        kind = el["tag"] + (f"/{el['type']}" if el.get("type") else "")
        lines.append(f"{el['id']} | {kind} | {el.get('label', '')!r} | {value} | {' '.join(flags)}")
    if not snapshot.get("elements"):
        lines.append("(no interactive elements)")
    return "\n".join(lines)


def perform(page: Any, actions: list[dict[str, Any]], known_ids: set[str]) -> tuple[int, str | None]:
    """Run a batch of actions. Stop at the first failure; return (done, error)."""
    done = 0
    start_url = page.url
    for action in actions[:MAX_ACTIONS_PER_TURN]:
        op, element_id, text = action.get("op"), str(action.get("id", "")), action.get("text")
        if element_id not in known_ids:
            return done, f"unknown id {element_id!r}; use an id from the latest observation"
        locator = page.locator(f'[data-rcu-id="{element_id}"]')
        try:
            if op == "click":
                locator.click(timeout=ACTION_TIMEOUT_MS)
            elif op == "fill":
                locator.fill(str(text or ""), timeout=ACTION_TIMEOUT_MS)
            elif op == "select":
                locator.select_option(str(text or ""), timeout=ACTION_TIMEOUT_MS)
            elif op == "check":
                locator.check(timeout=ACTION_TIMEOUT_MS)
            elif op == "uncheck":
                locator.uncheck(timeout=ACTION_TIMEOUT_MS)
            elif op == "press":
                locator.press(str(text or "Enter"), timeout=ACTION_TIMEOUT_MS)
            else:
                return done, f"unknown op {op!r}"
        except Exception as exc:  # report to the model; it decides what next
            return done, f"{op} {element_id} failed: {str(exc).splitlines()[0][:200]}"
        done += 1
        if page.url != start_url and done < len(actions):
            return done, "page URL changed; use the refreshed element IDs before continuing"
    _settle(page)
    return done, None


def run_browser_task(
    page: Any,
    task: str,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = MAX_TURNS,
    verify: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> BrowserAgentResult:
    """Drive *page* until the model finishes, *verify* passes, or turns run out."""
    started = time.monotonic()
    result = BrowserAgentResult(False, False, "", 0, 0, 0, 0)
    client = client or default_client()
    system_message = {"role": "system", "content": SYSTEM_PROMPT}
    task_message = {"role": "user", "content": f"Task: {task}"}
    snapshot = observe(page)
    observation = {"role": "user", "content": "Current page:\n" + render_observation(snapshot)}
    messages = [system_message, task_message, observation]
    recent_actions: list[str] = []

    for turn in range(1, max_turns + 1):
        if deadline is not None and time.monotonic() > deadline:
            result.error = "deadline reached"
            break
        result.turns = turn
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto"
        )
        result.llm_calls += 1
        usage = getattr(response, "usage", None)
        result.tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
        result.tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)
        message = response.choices[0].message
        calls = list(message.tool_calls or [])
        messages.append(_assistant_message(message, calls))
        if not calls:
            recent_actions.append("No action was returned; choose an act or finish tool call.")
            messages = _compact_messages(
                system_message, task_message, recent_actions, snapshot
            )
            continue

        finished = False
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            if call.function.name == "finish":
                result.ok = bool(args.get("success"))
                result.summary = str(args.get("summary", ""))[:500]
                result.trace.append({"turn": turn, "finish": result.ok})
                messages.append(_tool_reply(call, "finished"))
                finished = True
                break
            actions = args.get("actions") if isinstance(args.get("actions"), list) else []
            known = {el["id"] for el in snapshot.get("elements", [])}
            count, error = perform(page, actions, known)
            result.actions += count
            result.trace.append({"turn": turn, "actions": count, "error": error})
            operations = ", ".join(
                str(action.get("op", "?")) for action in actions[:count]
            )
            note = f"Completed {count} action(s)" + (f" ({operations})" if operations else "")
            if error:
                note += f"; {error}"
            recent_actions.append(note)
            recent_actions = recent_actions[-3:]
            if verify is not None and _safe_verify(verify):
                result.ok = result.verified = True
                result.summary = "verifier passed"
                finished = True
            snapshot = observe(page)
            status = f"Performed {count} of {len(actions)} actions." + (f" Error: {error}" if error else "")
            messages.append(_tool_reply(call, status + "\n\n" + render_observation(snapshot)))
        if finished:
            break
        messages = _compact_messages(system_message, task_message, recent_actions, snapshot)
    else:
        result.error = result.error or f"no finish after {max_turns} turns"

    if verify is not None and not result.verified:
        result.verified = _safe_verify(verify)
        result.ok = result.ok and result.verified
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def _compact_messages(
    system_message: dict[str, Any],
    task_message: dict[str, Any],
    recent_actions: list[str],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep the task and latest page, not every full snapshot in the transcript."""
    notes = "\n".join(f"- {note}" for note in recent_actions)
    summary = {"role": "assistant", "content": "Recent actions:\n" + notes}
    observation = {
        "role": "user",
        "content": "Current page:\n" + render_observation(snapshot),
    }
    return [system_message, task_message, summary, observation]


def record_headless(
    url: str,
    *,
    task: str,
    har_path: str | Path,
    agent_prompt: str | None = None,
    timeout_s: float = 300,
    headless: bool = True,
    client: Any = None,
    model: str = DEFAULT_MODEL,
) -> "HeadlessCaptureResult":
    """``capture.record`` with the browser agent as the operator.

    Same signature and ``CaptureResult`` as ``capture.record``, so it plugs
    into ``loop.do_task(capture_fn=record_headless)``.
    """
    from playwright.sync_api import sync_playwright

    from . import capture
    from .har import Redactor, endpoints, har_sha256, load_exchanges
    from .session import save_session

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    target = Path(har_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    site = capture._site(url)
    verify_url = capture._verifier_url(url)
    started = time.monotonic()
    deadline = started + timeout_s
    saved_session = None
    agent: BrowserAgentResult | None = None

    def verified() -> bool:
        payload = capture._fetch_verifier(verify_url)
        return bool(payload and payload.get("success") is True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            record_har_path=str(target), record_har_mode="full", record_har_content="embed"
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _settle(page)
            agent = run_browser_task(
                page, agent_prompt or task, client=client, model=model, verify=verified, deadline=deadline
            )
            try:
                saved_session = save_session(context, site)
            except Exception:  # a lost session must not lose the recording
                saved_session = None
        finally:
            context.close()  # flushes the HAR
            browser.close()

    verifier_result = capture._fetch_verifier(verify_url)
    ok = bool(verifier_result and verifier_result.get("success") is True)
    recording_id = None
    if target.is_file():
        exchanges = load_exchanges(target, site=site, redactor=Redactor())
        recording_id = capture._persist_metadata(
            {
                "site": site,
                "task": f"Recorded workflow for {site}",
                "har_sha256": har_sha256(target),
                "exchange_count": len(exchanges),
                "endpoints": endpoints(exchanges),
                "source": "agent",
                "created_at": datetime.now(timezone.utc),
            }
        )
    return HeadlessCaptureResult(
        har_path=target,
        duration_ms=int((time.monotonic() - started) * 1000),
        verifier_result=capture._safe_verifier(verifier_result),
        ok=ok,
        timed_out=not ok and time.monotonic() > deadline,
        source="agent",
        recording_id=recording_id,
        session_path=saved_session,
        agent=agent,
    )


def _settle(page: Any) -> None:
    # Actions already auto-wait for navigation. Avoid a network-idle wait here:
    # analytics, polling, and long-lived requests can otherwise cost 3 seconds
    # after every model action batch.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=500)
    except Exception:
        pass


def _safe_verify(verify: Callable[[], bool]) -> bool:
    try:
        return bool(verify())
    except Exception:
        return False


def _assistant_message(message: Any, calls: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    return out


def _tool_reply(call: Any, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}
