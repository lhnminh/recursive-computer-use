"""
agent.py — Computer-use agentic loop using the Chat Completions API.

Uses client.chat.completions.create() with tool calling, which is supported
by both the standard OpenAI API and the local codex-as-api proxy.

The model is given an exec_py function tool backed by a Sandbox instance.
It writes Python that uses pyautogui to operate the desktop, calls display()
to send screenshots back, and calls log() to emit text.

The loop runs until:
  - The model returns a final message with no tool calls, or
  - The turn limit is reached.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from openai import OpenAI

from .auth import resolve as resolve_auth
from .guides import GuideStore, Guide, Step, detect_env
from .sandbox import Sandbox

# Maximum round-trips before we give up.
MAX_TURNS = 30

# Explicit image `detail` to request for screenshots, or None to omit it.
# Must be None for Responses Lite models (e.g. gpt-5.6-sol), which reject an
# explicit detail with "explicit image detail cannot be represented by
# Responses Lite". Non-Lite models (gpt-5.5, standard OpenAI) accept "high".
IMAGE_DETAIL: str | None = None

# LinkedIn workflows use a stable key and omit text from saved guides.
_LINKEDIN_SITES = {"linkedin.com", "www.linkedin.com"}

# Give the model an operating procedure, not just a Python tool description.
# In particular, distinguish preparing a post from publishing one.
SYSTEM_PROMPT = """You control a desktop through screenshots and pyautogui.
Work in short steps: inspect the current screen first, perform only a small
group of actions, wait for the page to update, then inspect again. Use the
visible interface as evidence; do not assume a click worked. If navigation or a
control is unclear, take another screenshot and recover from the current screen.
When opening a website, put its address in the browser address bar: focus the
bar with the platform's address-bar shortcut (Command+L on macOS, Ctrl+L on
Windows/Linux), type the URL, press Enter, wait, and inspect a fresh screenshot
to confirm the destination loaded. Never type a URL into a webpage text field.
If the address bar is not available, use only a visibly identified browser
address bar; do not guess at a page field. If text appears in the wrong place,
stop typing, inspect the screen, and recover by focusing the address bar.
Before each tool call, include a brief progress update in your assistant message:
say what you can currently observe, what action you plan next, and any
uncertainty. Give a concise status, not private chain-of-thought. Report only
what the screenshot or tool result supports. If an action fails, say what visibly
did or did not change; do not blame another process or infer a cause without
evidence. Try a different interaction method after a failed attempt.
For requests to draft or compose content, enter the requested text and leave it
ready for review. Do not publish, submit, or share it unless the user explicitly
asks you to do so. At the end, report what you actually completed and mention
when a draft is left open for review."""

# Kinds we treat as replayable / worth capturing into a guide.
_MEANINGFUL_KINDS = {
    "click", "doubleClick", "rightClick", "write", "typewrite",
    "press", "hotkey", "scroll", "moveTo", "dragTo",
}

# Tool definition exposed to the model.
EXEC_PY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "exec_py",
        "description": (
            "Run Python in a persistent desktop environment. "
            "Variables and imports persist across calls within the same session. "
            "Pre-imported: pyautogui, time. "
            "Helpers: log(value) to print text, display(pil_image) to attach a screenshot. "
            "Screenshots passed to display() are resized to match pyautogui.size(), "
            "so screenshot coordinates match click coordinates. "
            "For website navigation, focus the browser address bar with Command+L "
            "on macOS or Ctrl+L on Windows/Linux, enter the URL, press Enter, "
            "then inspect a screenshot to confirm the page loaded. Never type a "
            "URL into a webpage text field. "
            "Before each call, briefly state what you observe and what you will do next. "
            "Always inspect the screen first with display(pyautogui.screenshot()) before "
            "taking any action, then verify after each short group of actions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def _check_proxy(base_url: str) -> None:
    """Raise a clear error if the local codex-as-api proxy is not reachable."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 18080), timeout=2):
            pass
    except (OSError, ConnectionRefusedError):
        raise RuntimeError(
            "Cannot reach the local Codex proxy at 127.0.0.1:18080.\n"
            "Start it in a separate terminal first:\n\n"
            "    npx codex-as-api\n\n"
            "Then re-run your command."
        )


def run(
    prompt: str,
    *,
    model: str = "gpt-5.6-sol",
    verbose: bool = False,
    site: str | None = None,
    task: str | None = None,
    use_guides: bool = False,
    relearn: bool = False,
    guide_store: GuideStore | None = None,
) -> str:
    """
    Run a computer-use task described by *prompt*.

    Parameters
    ----------
    prompt:
        Natural-language description of the task to complete.
    model:
        Model to use. Defaults to ``gpt-5.6-sol`` (available via Codex proxy).
    verbose:
        Print turn-by-turn activity to stderr.
    site, task:
        Optional ``(site, task)`` key for guide lookup/capture. When omitted,
        they are inferred from *prompt* (see :func:`infer_site_task`).
    use_guides:
        When True, consult the guide store for a fast-path replay and
        record a new guide on a successful free-navigation run.
    relearn:
        Force a fresh free-navigation run and overwrite any stored guide.
    guide_store:
        Optional pre-built :class:`GuideStore`. When omitted, a local-only
        store is opened (MongoDB wiring is layered in separately).

    Returns
    -------
    str
        The model's final text response.
    """
    creds = resolve_auth()

    # If using the local proxy, check it's actually running before the first call.
    if creds.base_url and "127.0.0.1" in creds.base_url:
        _check_proxy(creds.base_url)

    client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)
    trace_dir = Path(tempfile.mkdtemp(prefix="recursive-computer-use-trace-")) \
        if verbose else None
    sandbox = Sandbox(trace_dir=trace_dir)
    if verbose and trace_dir is not None:
        print(f"[trace] screenshots: {trace_dir}", file=sys.stderr)

    # Resolve the (site, task) key and current machine env.
    if site is None or task is None:
        inferred_site, inferred_task = infer_site_task(prompt)
        site = site or inferred_site
        task = task or inferred_task
    env = detect_env()

    store = guide_store
    if use_guides and store is None:
        store = GuideStore.open(None, verbose=verbose)  # local-only by default

    is_linkedin = _is_linkedin_workflow(site, task)
    guide_task = _guide_task_key(site, task)

    # -- Fast path: replay a matching guide if we have one -----------------
    if use_guides and store is not None and not relearn and site and guide_task:
        guide = store.find_guide(site, guide_task, env.fingerprint)
        if guide is not None and guide.steps:
            if verbose:
                print(f"[guide] HIT for ({site!r}, {task!r}) @ {env.fingerprint} "
                      f"— {len(guide.steps)} step(s); attempting fast path",
                      file=sys.stderr)
            ok, final_text = _fast_path(sandbox, guide, verbose=verbose)
            store.bump_guide_stats(site, guide_task, env.fingerprint, ok=ok)
            if ok:
                if verbose:
                    print("[guide] fast path succeeded", file=sys.stderr)
                return final_text
            if verbose:
                print("[guide] fast path checkpoint failed — falling back to "
                      "free navigation", file=sys.stderr)
        elif verbose:
            print(f"[guide] MISS for ({site!r}, {task!r}) @ {env.fingerprint} "
                  "— free navigation", file=sys.stderr)

    # -- Free navigation: the model drives via screenshots + reasoning -----
    final_text = _free_navigation(client, sandbox, prompt, model=model, verbose=verbose)

    # -- Learn: distill the successful run into a coordinate guide ---------
    if use_guides and store is not None and site and guide_task:
        try:
            # Do not persist literal text typed into LinkedIn (it may be a
            # private post or message). Coordinates and action kinds are enough
            # for the guide, and the empty text action replays as a no-op.
            steps = _distill_steps(sandbox, env, include_text=not is_linkedin)
            if steps:
                guide = Guide(
                    site=site, task=guide_task, env=env, steps=steps,
                    title=f"{guide_task} on {site}", source_run_id=None,
                    model=model,
                )
                store.upsert_guide(guide)
                store.bump_guide_stats(site, guide_task, env.fingerprint, ok=True)
                if verbose:
                    print(f"[guide] recorded {len(steps)} step(s) for "
                          f"({site!r}, {guide_task!r}) @ {env.fingerprint}",
                          file=sys.stderr)
            elif verbose:
                print("[guide] no replayable actions captured; nothing stored",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never fail the run on learning
            print(f"[guide] distillation failed (non-fatal): {exc}", file=sys.stderr)

    return final_text


def _free_navigation(
    client: OpenAI,
    sandbox: Sandbox,
    prompt: str,
    *,
    model: str,
    verbose: bool,
) -> str:
    """The original slow loop: model looks, reasons, acts, repeats."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    for turn in range(1, MAX_TURNS + 1):
        if verbose:
            print(f"[turn {turn}] calling model …", file=sys.stderr)

        response = client.chat.completions.create(
            model=model,
            tools=[EXEC_PY_TOOL],
            messages=messages,
        )

        choice = response.choices[0]
        msg = choice.message

        # Append assistant message to history.
        # exclude_unset=True + exclude_none=True drops fields like `annotations`
        # that are present in newer SDK versions but rejected by some API endpoints.
        messages.append(msg.model_dump(exclude_unset=True, exclude_none=True))

        if verbose:
            progress = msg.content
            if isinstance(progress, str) and progress.strip():
                print(f"  model: {progress.strip()}", file=sys.stderr)
            else:
                print("  model: (no progress update in assistant message)",
                      file=sys.stderr)

        # No tool calls → model is done
        if not msg.tool_calls:
            final_text = msg.content or ""
            if verbose:
                print(f"[done] {final_text}", file=sys.stderr)
            return final_text

        if turn == MAX_TURNS:
            raise RuntimeError(
                f"Reached the {MAX_TURNS}-turn limit without a final answer."
            )

        # Execute each tool call and append results
        for tool_call in msg.tool_calls:
            if tool_call.function.name != "exec_py":
                raise ValueError(
                    f"Model requested unexpected tool: {tool_call.function.name!r}"
                )

            args = json.loads(tool_call.function.arguments)
            code: str = args["code"]

            if verbose:
                print(f"  exec_py ({tool_call.id}):\n{code}", file=sys.stderr)

            result = sandbox.run(code)

            if verbose:
                stdout = result.get("stdout", "").strip()
                if stdout:
                    print(f"  stdout:\n{stdout}", file=sys.stderr)
                error = result.get("error")
                if error:
                    print(f"  error:\n{error.rstrip()}", file=sys.stderr)
                n_imgs = len(result.get("images", []))
                print(f"  observation: {n_imgs} screenshot(s); "
                      f"{'error' if result.get('error') else 'no execution error'}",
                      file=sys.stderr)

            # Build tool result — text only in the `tool` message.
            # Some API endpoints (e.g. the local Codex proxy) reject image_url
            # blocks inside tool-role messages, so screenshots are appended as a
            # follow-up `user` message instead.
            text_content, image_blocks = _build_tool_content(result)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": text_content,
            })

            # Inject screenshots as a user message so the model can see them.
            if image_blocks:
                messages.append({
                    "role": "user",
                    "content": image_blocks,
                })

    raise RuntimeError("Agentic loop exited unexpectedly.")


def _fast_path(sandbox: Sandbox, guide: Guide, *, verbose: bool) -> tuple[bool, str]:
    """
    Replay a guide's steps directly via pyautogui — no screenshots, no model.

    Returns ``(ok, final_text)``. ``ok`` is False if replay raised, so the
    caller can fall back to free navigation.

    NOTE: checkpoint verification (URL/title checks) is a planned enhancement
    (design G4). For now the fast path fires steps blind with small pauses; a
    replay exception is the failure signal.
    """
    lines = ["import pyautogui, time"]
    for step in guide.steps:
        lines.append(_replay_line(step))
        lines.append("time.sleep(0.4)")
    code = "\n".join(lines)

    if verbose:
        print(f"[guide] replaying {len(guide.steps)} step(s) fast", file=sys.stderr)

    result = sandbox.run(code)
    if result.get("error"):
        if verbose:
            print(f"  replay error: {result['error'].splitlines()[-1]}",
                  file=sys.stderr)
        return False, ""
    return True, f"Replayed guide '{guide.title or guide.task}' ({len(guide.steps)} steps)."


def _replay_line(step: Step) -> str:
    """Render one guide step as a pyautogui call for replay."""
    k = step.kind
    if k in ("click", "doubleClick", "rightClick", "moveTo"):
        if step.x is not None and step.y is not None:
            return f"pyautogui.{k}({step.x}, {step.y})"
        return f"pyautogui.{k}()"
    if k in ("write", "typewrite"):
        return f"pyautogui.write({step.text!r})" if step.text else "pass"
    if k == "press":
        return f"pyautogui.press({step.text!r})" if step.text else "pass"
    if k == "hotkey":
        keys = step.args or ([] if step.text is None else [step.text])
        return "pyautogui.hotkey(" + ", ".join(repr(x) for x in keys) + ")"
    if k == "scroll":
        amount = step.args[0] if step.args else 0
        return f"pyautogui.scroll({amount})"
    if k == "dragTo":
        if step.x is not None and step.y is not None:
            return f"pyautogui.dragTo({step.x}, {step.y})"
    return "pass"


def _distill_steps(sandbox: Sandbox, env, *, include_text: bool = True) -> list[Step]:
    """
    Build guide steps from the actions captured during a successful run.

    v1 heuristic: take the meaningful captured actions in order and normalize
    them into :class:`Step` records with absolute + normalized coordinates.
    Model-authored distillation is a planned enhancement (design G5).
    """
    steps: list[Step] = []
    seq = 0
    w = env.screen_width or 1
    h = env.screen_height or 1
    for act in sandbox.captured_actions:
        kind = act.get("kind")
        if kind not in _MEANINGFUL_KINDS:
            continue
        seq += 1
        x = act.get("x")
        y = act.get("y")
        text = None
        args = act.get("args") or []
        if include_text and kind in ("write", "typewrite", "press") and args:
            text = args[0] if isinstance(args[0], str) else None
        nx = round(x / w, 4) if isinstance(x, (int, float)) and kind not in ("scroll",) else None
        ny = round(y / h, 4) if isinstance(y, (int, float)) and kind not in ("scroll",) else None
        steps.append(Step(
            seq=seq, kind=kind,
            x=x if kind not in ("scroll",) else None,
            y=y if kind not in ("scroll",) else None,
            nx=nx, ny=ny, text=text,
            args=args if kind in ("hotkey", "scroll") else {},
            checkpoint=(seq == 1),  # verify the first step later (G4)
        ))
    return steps


def infer_site_task(prompt: str) -> tuple[str | None, str | None]:
    """
    Best-effort extraction of a ``(site, task)`` key from a natural-language
    prompt.

    * ``site`` — first hostname/domain found (e.g. ``openai.com``), lowercased.
    * ``task`` — the whole prompt, lowercased and trimmed, as a coarse intent.

    This is intentionally simple for v1; a smarter classifier can replace it
    without changing callers.
    """
    import re

    site = None
    # Match a URL or a bare domain like foo.com / sub.foo.co.uk
    m = re.search(r"https?://([^/\s]+)", prompt)
    if m:
        site = m.group(1).lower()
    else:
        m = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", prompt, re.IGNORECASE)
        if m:
            site = m.group(1).lower()
        elif "linkedin" in prompt.lower():
            site = "linkedin.com"
    if site and site.startswith("www."):
        site = site[4:]

    task = " ".join(prompt.lower().split()) or None
    return site, task


def _is_linkedin_workflow(site: str | None, task: str | None) -> bool:
    """Whether to use LinkedIn's shared guide key and omit typed text."""
    host = (site or "").lower().split(":", 1)[0].rstrip(".")
    if host in _LINKEDIN_SITES or host.endswith(".linkedin.com"):
        return True

    # Prompts often name the service but omit its URL, so also inspect the
    # inferred task text.
    return "linkedin" in (task or "").lower()


def _guide_task_key(site: str | None, task: str | None) -> str | None:
    """Avoid storing LinkedIn prompt text, which may contain post/message data."""
    if _is_linkedin_workflow(site, task):
        return "linkedin:workflow"
    return task


def _build_tool_content(
    result: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Build the content for a tool result.

    Returns
    -------
    text_content:
        Plain-text summary suitable for the ``tool`` role message.
    image_blocks:
        List of ``image_url`` content blocks (may be empty) to be sent in a
        follow-up ``user`` role message, because many API endpoints reject
        image content inside tool-role messages.
    """
    text_parts: list[str] = []
    stdout = result.get("stdout", "").strip()
    if stdout:
        text_parts.append(f"[stdout]\n{stdout}")
    error = result.get("error")
    if error:
        text_parts.append(f"[error]\n{error.strip()}")
    if not text_parts:
        text_parts.append("[ok]")

    text_content = "\n\n".join(text_parts)

    image_blocks: list[dict[str, Any]] = []
    for data_url in result.get("images", []):
        block: dict[str, Any] = {"type": "image_url", "image_url": {"url": data_url}}
        # Only set explicit detail when enabled. Responses Lite models
        # (e.g. gpt-5.6-sol) reject an explicit `detail` field with:
        #   "explicit image detail cannot be represented by Responses Lite"
        if IMAGE_DETAIL:
            block["image_url"]["detail"] = IMAGE_DETAIL
        image_blocks.append(block)

    return text_content, image_blocks
