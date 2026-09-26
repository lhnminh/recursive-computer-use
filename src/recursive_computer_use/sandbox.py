"""
sandbox.py — Execute model-generated Python code in a persistent desktop session.

The sandbox keeps a live namespace between calls so pyautogui state (e.g. screen
size, last coordinates) and any variables the model sets persist across turns.

Observations returned to the model are a JSON string with:
  - "stdout"  : captured print / log output
  - "images"  : list of base64-encoded PNG screenshots (from display() calls)
  - "error"   : traceback string if the code raised an exception
"""

from __future__ import annotations

import base64
import io
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any


def _pil_to_base64(image: Any) -> str:
    """Encode a PIL Image to a base64 PNG data URL."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


class Sandbox:
    """
    A persistent execution environment for one computer-use session.

    Usage::

        sb = Sandbox()
        result = sb.run("import pyautogui; display(pyautogui.screenshot())")
        # result is a JSON-serialisable dict
    """

    def __init__(self) -> None:
        # Persistent namespace shared across all run() calls in this session.
        # Pre-populate with helpers the model description promises are available.
        self._ns: dict[str, Any] = {}
        self._images: list[str] = []

        # Inject helpers into the namespace
        self._ns["__builtins__"] = __builtins__

        # log(value) — appends to stdout-like output
        # display(pil_image) — captures a screenshot for the observation
        sandbox_self = self

        def _log(value: Any) -> None:
            print(value)

        def _display(image: Any) -> None:
            sandbox_self._images.append(_pil_to_base64(image))

        self._ns["log"] = _log
        self._ns["display"] = _display

        # Pre-import common modules so the model can use them without imports
        import pyautogui  # noqa: F401 — available in model's namespace
        import time  # noqa: F401

        self._ns["pyautogui"] = pyautogui
        self._ns["time"] = time

    def run(self, code: str) -> dict[str, Any]:
        """
        Execute *code* in the persistent namespace and return an observation dict.

        Returns::

            {
                "stdout": "...",          # captured print output
                "images": ["data:image/png;base64,..."],  # screenshots
                "error": "..." | None     # exception traceback or None
            }
        """
        self._images = []
        stdout_buf = io.StringIO()
        error: str | None = None

        try:
            with redirect_stdout(stdout_buf):
                exec(compile(code, "<model>", "exec"), self._ns)  # noqa: S102
        except Exception:
            error = traceback.format_exc()

        return {
            "stdout": stdout_buf.getvalue(),
            "images": list(self._images),
            "error": error,
        }

    def observation_text(self, result: dict[str, Any]) -> str:
        """
        Serialise a run() result to the plain-text string the Responses API
        expects as a function_call_output.

        Format (human-readable so the model can parse it)::

            [stdout]
            <stdout text>

            [images]
            <N screenshot(s) attached>

            [error]
            <traceback>
        """
        import json

        parts: list[str] = []

        stdout = result.get("stdout", "").strip()
        if stdout:
            parts.append(f"[stdout]\n{stdout}")

        images = result.get("images", [])
        if images:
            parts.append(f"[images]\n{len(images)} screenshot(s) attached")

        error = result.get("error")
        if error:
            parts.append(f"[error]\n{error.strip()}")

        if not parts:
            parts.append("[ok]\n(no output)")

        return "\n\n".join(parts)

    def observation_content(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Build the multimodal content list for a function_call_output item.

        Returns a list of content blocks accepted by the Responses API:
          - text block with stdout / error
          - image_url blocks for each screenshot
        """
        blocks: list[dict[str, Any]] = []

        # Text block (stdout + error)
        text_parts: list[str] = []
        stdout = result.get("stdout", "").strip()
        if stdout:
            text_parts.append(f"[stdout]\n{stdout}")
        error = result.get("error")
        if error:
            text_parts.append(f"[error]\n{error.strip()}")
        if not text_parts:
            text_parts.append("[ok]")

        blocks.append({"type": "text", "text": "\n\n".join(text_parts)})

        # Image blocks
        for data_url in result.get("images", []):
            blocks.append({
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "high"},
            })

        return blocks
