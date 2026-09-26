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
from typing import Any

from openai import OpenAI

from .auth import resolve as resolve_auth
from .sandbox import Sandbox

# Maximum round-trips before we give up.
MAX_TURNS = 30

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
    model: str = "gpt-5.5",
    verbose: bool = False,
) -> str:
    """
    Run a computer-use task described by *prompt*.

    Parameters
    ----------
    prompt:
        Natural-language description of the task to complete.
    model:
        Model to use. Defaults to ``gpt-5.5`` (available via Codex proxy).
        Use ``gpt-5.6-sol`` or ``gpt-6-astra`` for more capable models.
    verbose:
        Print turn-by-turn activity to stderr.

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
    sandbox = Sandbox()

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

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
                preview = code.splitlines()[0][:80]
                print(f"  exec_py: {preview!r}", file=sys.stderr)

            result = sandbox.run(code)

            if verbose and result.get("error"):
                print(f"  error: {result['error'].splitlines()[-1]}", file=sys.stderr)
            if verbose:
                n_imgs = len(result.get("images", []))
                if n_imgs:
                    print(f"  captured {n_imgs} screenshot(s)", file=sys.stderr)

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
        image_blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url, "detail": "high"},
        })

    return text_content, image_blocks
