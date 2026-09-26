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
from .store import ActionStore

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
After launching the required app, navigate to a requested website in a new tab
while preserving the current tab (Command+T on macOS, Ctrl+T on Windows/Linux).
Then focus the new tab's address bar with Command+L on macOS or Ctrl+L on
Windows/Linux, type the URL, press Enter, wait, and inspect a fresh screenshot
to confirm the destination loaded. Do this even if another site is already
open. Never type a URL into a webpage text field.
The sandbox provides `default_browser`, detected from the operating system, and
`launch_app(app_name)`, which directly opens a named app without closing other
apps. After the initial screenshot, identify the app required by the task and
make `launch_app(app_name)` your first desktop action, even if its window might
already be open. For a website task, call `launch_app("default_browser")`. Wait
and inspect a screenshot to confirm the app's window before continuing. This
launch call is captured as the first guide step; the harness does not insert it.
A ChatGPT window is not a browser window, even if it can display web content;
do not type the URL into ChatGPT.
Do not use Command+Tab, shell commands, AppleScript, or guessed coordinates to
launch or find apps. Do not press Command+M or another minimize shortcut to try
to reveal the browser; minimizing the current window hides it.
Never close, quit, minimize, or dismiss the terminal or app that launched this
task. Leave unrelated windows open. Do not use Command+Q, window close buttons,
or other window-management actions as a navigation or recovery method. Complete
the requested task using the browser while keeping the launching terminal open.
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
    "launch_app", "click", "doubleClick", "rightClick", "write", "typewrite",
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
            "default_browser is the OS-reported browser name; "
            "launch_app(app_name) directly opens the named app on macOS. "
            "Helpers: log(value) to print text, display(pil_image) to attach a screenshot. "
            "Screenshots passed to display() are resized to match pyautogui.size(), "
            "so screenshot coordinates match click coordinates. "
            "Inspect the screen first. After the initial screenshot, call "
            "launch_app(app_name) as the first desktop action, even if the app "
            "might already be open. For a website task use "
            "launch_app(\"default_browser\"). Wait and inspect a screenshot to "
            "confirm the app window. That call becomes the first guide step; "
            "the harness does not insert it. For a requested website, preserve "
            "the current tab and open a new tab with Command+T on macOS or Ctrl+T "
            "on Windows/Linux. Then focus the new tab's address bar with "
            "Command+L or Ctrl+L, enter the URL, press Enter, and inspect a "
            "screenshot to confirm the page loaded. Never type a URL into a "
            "webpage text field. If "
            "default_browser is unknown or launch fails, inspect the Dock for a "
            "visible browser. Do not use Command+Tab, shell commands, or AppleScript "
            "to launch or find apps, and do not press Command+M to reveal a browser. "
            "A ChatGPT window is not a browser window; do not type the URL there. Never "
            "close, quit, minimize, or dismiss the terminal or app that launched "
            "this task. Leave unrelated windows open; do not use Command+Q, close "
            "buttons, or other window-management actions to navigate or recover. "
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
    model: str = "gpt-5.6-terra",
    verbose: bool = False,
    site: str | None = None,
    task: str | None = None,
    use_guides: bool = True,
    replay_guides: bool = True,
    guide_store: GuideStore | None = None,
) -> str:
    """
    Run a computer-use task described by *prompt*.

    Parameters
    ----------
    prompt:
        Natural-language description of the task to complete.
    model:
        Model to use. Defaults to ``gpt-5.6-terra`` (available via Codex proxy).
    verbose:
        Print turn-by-turn activity to stderr.
    site, task:
        Optional ``(site, task)`` key for guide lookup/capture. When omitted,
        they are inferred from *prompt* (see :func:`infer_site_task`).
    use_guides:
        When True, record or refresh a guide from a successful free-navigation
        run. Enabled by default.
    replay_guides:
        When True, attempt to replay a matching saved guide before navigation.
        Enabled by default.
    guide_store:
        Optional pre-built :class:`GuideStore`. When omitted and guide
        capture/replay is enabled, MongoDB is used when configured and the
        local JSON store remains available as a fallback.

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
    if (use_guides or replay_guides) and store is None:
        # Use the configured MongoDB database for shared guides, with the
        # GuideStore's local JSON file as a fallback/warm cache.
        mongo = ActionStore.connect(verbose=verbose)
        guide_collection = mongo.collection("guides")
        store = GuideStore.open(guide_collection, verbose=verbose)

    is_linkedin = _is_linkedin_workflow(site, task)
    guide_task = _guide_task_key(site, task)

    replay_failed = False
    if replay_guides and store is not None and site and guide_task:
        guide = store.find_guide(site, guide_task, env.fingerprint)
        if guide is not None and guide.steps:
            if verbose:
                print(f"[guide] replaying {len(guide.steps)} saved step(s) for "
                      f"({site!r}, {guide_task!r})", file=sys.stderr)
            ok, final_text = _fast_path(
                sandbox, guide, site=site, prompt=prompt, verbose=verbose
            )
            store.bump_guide_stats(site, guide_task, env.fingerprint, ok=ok)
            if ok:
                return final_text
            replay_failed = True
            if verbose:
                print("[guide] replay failed; falling back to screenshot-guided "
                      "navigation", file=sys.stderr)
        elif verbose:
            print(f"[guide] no saved guide for ({site!r}, {guide_task!r}); "
                  "using screenshot-guided navigation", file=sys.stderr)

    # Screenshot-guided navigation is the default and replay fallback.
    final_text = _free_navigation(client, sandbox, prompt, model=model, verbose=verbose)

    # -- Learn: distill the successful run into a coordinate guide ---------
    if (use_guides or replay_failed) and store is not None and site and guide_task:
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
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n\n"
                f"The operating system reports {sandbox.default_browser!r} "
                "as the default browser. Use that app when opening websites."
            ),
        },
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


def _fast_path(
    sandbox: Sandbox,
    guide: Guide,
    *,
    site: str,
    prompt: str,
    verbose: bool,
) -> tuple[bool, str]:
    """Replay a guide, filling redacted values from the current prompt."""
    lines = ["import pyautogui, time"]
    url = site.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    prompt_text = _extract_prompt_text(prompt)

    # Older guides have no parameter labels. Treat the final blank text entry
    # after address-bar navigation as the prompt-supplied content.
    legacy_prompt_step = None
    address_bar_focused = False
    waiting_for_navigation = False
    page_navigated = False
    for step in guide.steps:
        if step.kind == "hotkey":
            keys = step.args if isinstance(step.args, list) else []
            normalized = {str(key).lower() for key in keys}
            address_bar_focused = (
                ("command" in normalized and "l" in normalized)
                or (("ctrl" in normalized or "control" in normalized)
                    and "l" in normalized)
            )
        elif address_bar_focused and step.kind in ("write", "typewrite"):
            address_bar_focused = False
            waiting_for_navigation = True
        elif waiting_for_navigation and step.kind == "press":
            waiting_for_navigation = False
            page_navigated = True
        elif page_navigated and step.kind in ("write", "typewrite") and step.text is None:
            legacy_prompt_step = step.seq
    prompt_step = max(
        (step.seq for step in guide.steps if step.parameter == "prompt_text"),
        default=legacy_prompt_step,
    )

    address_bar_focused = False
    navigation_url_entered = False
    for step in guide.steps:
        if step.kind == "hotkey":
            keys = step.args if isinstance(step.args, list) else []
            normalized = {str(key).lower() for key in keys}
            is_address_shortcut = (
                ("command" in normalized and "l" in normalized)
                or (("ctrl" in normalized or "control" in normalized)
                    and "l" in normalized)
            )
            lines.append(_replay_line(step))
            address_bar_focused = is_address_shortcut
        elif (step.parameter == "navigation_url"
              or address_bar_focused and step.kind in ("write", "typewrite")):
            lines.append(
                f"pyautogui.write({url!r})"
                if step.parameter == "navigation_url" or step.text is None
                else _replay_line(step)
            )
            navigation_url_entered = step.parameter == "navigation_url" or step.text is None
            address_bar_focused = False
        elif navigation_url_entered and step.kind == "press" and step.text is None:
            lines.append("pyautogui.press('enter')")
            # LinkedIn needs longer than the normal inter-action pause to load.
            lines.append("time.sleep(5.0)")
            navigation_url_entered = False
            continue
        elif step.seq == prompt_step:
            lines.append(
                f"pyautogui.write({prompt_text!r})" if prompt_text else "pass"
            )
        else:
            lines.append(_replay_line(step))
        lines.append("time.sleep(0.4)")
    result = sandbox.run("\n".join(lines))
    if result.get("error"):
        if verbose:
            print(f"  replay error: {result['error'].splitlines()[-1]}",
                  file=sys.stderr)
        return False, ""
    return True, f"Replayed guide '{guide.title or guide.task}' ({len(guide.steps)} steps)."


def _replay_line(step: Step) -> str:
    """Render one guide step as a pyautogui call for replay."""
    kind = step.kind
    if kind == "launch_app":
        return f"launch_app({(step.text or 'default_browser')!r})"
    if kind in ("click", "doubleClick", "rightClick", "moveTo"):
        if step.x is not None and step.y is not None:
            return f"pyautogui.{kind}({step.x}, {step.y})"
        return f"pyautogui.{kind}()"
    if kind in ("write", "typewrite"):
        return f"pyautogui.write({step.text!r})" if step.text else "pass"
    if kind == "press":
        return f"pyautogui.press({step.text!r})" if step.text else "pass"
    if kind == "hotkey":
        keys = step.args or ([] if step.text is None else [step.text])
        return "pyautogui.hotkey(" + ", ".join(repr(key) for key in keys) + ")"
    if kind == "scroll":
        amount = step.args[0] if step.args else 0
        return f"pyautogui.scroll({amount})"
    if kind == "dragTo" and step.x is not None and step.y is not None:
        return f"pyautogui.dragTo({step.x}, {step.y})"
    return "pass"


def _extract_prompt_text(prompt: str) -> str | None:
    """Extract explicitly requested content from a task prompt, if present."""
    import re

    quoted = re.search(
        r"\b(?:saying|that says|with the text|with text|text:?)\s*[\"'“‘](.+?)[\"'”’]",
        prompt,
        re.IGNORECASE,
    )
    if quoted:
        return quoted.group(1).strip() or None
    unquoted = re.search(
        r"\b(?:saying|that says|with the text|with text|text:)\s+(.+?)\s*[.!?]*$",
        prompt,
        re.IGNORECASE,
    )
    return unquoted.group(1).strip() if unquoted else None


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
    address_bar_focused = False
    for act in sandbox.captured_actions:
        kind = act.get("kind")
        if kind not in _MEANINGFUL_KINDS:
            continue
        seq += 1
        x = act.get("x")
        y = act.get("y")
        text = None
        parameter = None
        args = act.get("args") or []
        if kind == "hotkey":
            normalized = {str(key).lower() for key in args}
            address_bar_focused = (
                ("command" in normalized and "l" in normalized)
                or (("ctrl" in normalized or "control" in normalized)
                    and "l" in normalized)
            )
        if kind == "launch_app" and args:
            text = args[0] if isinstance(args[0], str) else None
        elif include_text and kind in ("write", "typewrite", "press") and args:
            text = args[0] if isinstance(args[0], str) else None
        elif not include_text and kind in ("write", "typewrite") and args:
            parameter = "navigation_url" if address_bar_focused else "prompt_text"
        if kind in ("write", "typewrite"):
            address_bar_focused = False
        nx = round(x / w, 4) if isinstance(x, (int, float)) and kind not in ("scroll",) else None
        ny = round(y / h, 4) if isinstance(y, (int, float)) and kind not in ("scroll",) else None
        steps.append(Step(
            seq=seq, kind=kind,
            x=x if kind not in ("scroll",) else None,
            y=y if kind not in ("scroll",) else None,
            nx=nx, ny=ny, text=text, parameter=parameter,
            args=args if kind in ("hotkey", "scroll") else {},
            checkpoint=(seq == 1),  # verify the first step later (G4)
        ))
    return steps


def infer_site_task(prompt: str) -> tuple[str | None, str | None]:
    """
    Best-effort extraction of a ``(site, task)`` key from a natural-language
    prompt.

    * ``site`` — first hostname/domain found, or a service named after a
      common site preposition ("on Reddit", "in Notion", "at Acme").
    * ``task`` — the whole prompt, lowercased and trimmed, as a coarse intent.

    Named services without an explicit domain are normalized to ``<name>.com``.
    This is deliberately generic; it does not require a maintained service list.
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
        else:
            # Common natural-language forms: "on Reddit", "in Notion", or
            # "at Acme". Avoid treating articles and task verbs as services.
            m = re.search(
                r"\b(?:on|in|at|from|via|using|through)\s+"
                r"([A-Za-z0-9][A-Za-z0-9-]*)\b",
                prompt,
                re.IGNORECASE,
            )
            if m and m.group(1).lower() not in {
                "a", "an", "the", "my", "our", "your", "this", "that",
                "website", "web", "browser", "internet",
            }:
                site = f"{m.group(1).lower()}.com"
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
