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
import subprocess
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable


# Pointer/keyboard actions we record for guide capture.
_RECORDED_ACTIONS = (
    "click",
    "doubleClick",
    "rightClick",
    "moveTo",
    "dragTo",
    "write",
    "typewrite",
    "press",
    "hotkey",
    "scroll",
)


def _pil_to_base64(image: Any) -> str:
    """Encode a PIL Image to a base64 PNG data URL."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _detect_default_browser() -> tuple[str, str | None]:
    """Return the OS default browser name and app path when discoverable."""
    if sys.platform != "darwin":
        return "unknown", None
    try:
        from AppKit import NSWorkspace
        from Foundation import NSURL

        url = NSURL.URLWithString_("https://example.com")
        app_url = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(url)
        if app_url is not None:
            app_path = str(app_url.path())
            name = str(app_url.lastPathComponent())
            name = name[:-4] if name.lower().endswith(".app") else name
            return name, app_path
    except Exception:  # noqa: BLE001 — browser discovery must not block a session
        pass
    return "unknown", None


class _RecordingPyAutoGUI:
    """
    Thin proxy around the real ``pyautogui`` module.

    Attribute access passes through unchanged, except for the action functions
    in ``_RECORDED_ACTIONS``: those are wrapped so each call is recorded (via a
    callback) *before* delegating to the real function. Recording is best-effort
    and never breaks the underlying desktop action.
    """

    def __init__(self, real: Any, on_action: Callable[[str, tuple, dict], None]) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_on_action", on_action)

    def __getattr__(self, name: str) -> Any:
        real = object.__getattribute__(self, "_real")
        attr = getattr(real, name)
        if name in _RECORDED_ACTIONS and callable(attr):
            on_action = object.__getattribute__(self, "_on_action")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    on_action(name, args, kwargs)
                except Exception:  # noqa: BLE001 — never break the action
                    pass
                return attr(*args, **kwargs)

            return wrapper
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_real"), name, value)


class Sandbox:
    """
    A persistent execution environment for one computer-use session.

    Usage::

        sb = Sandbox()
        result = sb.run("import pyautogui; display(pyautogui.screenshot())")
        # result is a JSON-serialisable dict
    """

    def __init__(self, trace_dir: Path | None = None) -> None:
        # Persistent namespace shared across all run() calls in this session.
        # Pre-populate with helpers the model description promises are available.
        self._ns: dict[str, Any] = {}
        self._images: list[str] = []
        self._trace_dir = trace_dir
        self._screenshot_seq = 0
        self.default_browser, default_browser_app_path = _detect_default_browser()
        if self._trace_dir is not None:
            self._trace_dir.mkdir(parents=True, exist_ok=True)

        # Captured desktop actions for guide learning. Each entry:
        #   {"kind": str, "args": tuple, "kwargs": dict, "x": int|None, "y": int|None}
        self._actions: list[dict[str, Any]] = []

        # Inject helpers into the namespace
        self._ns["__builtins__"] = __builtins__

        # log(value) — appends to stdout-like output
        # display(pil_image) — captures a screenshot for the observation
        sandbox_self = self

        def _log(value: Any) -> None:
            print(value)

        def _display(image: Any) -> None:
            # On Retina macOS, screenshot() may return physical pixels while
            # pyautogui.size() and pointer actions use logical screen points.
            # Send the model an image in the same coordinate space as clicks.
            size = sandbox_self._real_pyautogui.size()
            width, height = int(size.width), int(size.height)
            if width > 0 and height > 0 and image.size != (width, height):
                source_size = image.size
                image = image.resize((width, height))
                print(
                    f"[display] normalized screenshot {source_size[0]}x"
                    f"{source_size[1]} to logical coordinate size "
                    f"{width}x{height}"
                )
            sandbox_self._images.append(_pil_to_base64(image))
            if sandbox_self._trace_dir is not None:
                sandbox_self._screenshot_seq += 1
                path = sandbox_self._trace_dir / (
                    f"screenshot-{sandbox_self._screenshot_seq:03d}.png"
                )
                image.save(path, format="PNG")

        self._ns["log"] = _log
        self._ns["display"] = _display
        self._ns["default_browser"] = self.default_browser

        def _launch_app(app_name: str) -> None:
            if not isinstance(app_name, str) or not app_name.strip():
                raise ValueError("An application name is required.")
            app_name = app_name.strip()
            requested_app = app_name
            if app_name == "default_browser":
                if default_browser_app_path is None:
                    raise RuntimeError("The operating system's default browser could not be detected.")
                app_name = default_browser_app_path
            elif app_name == self.default_browser and default_browser_app_path:
                app_name = default_browser_app_path
            if sys.platform != "darwin":
                raise RuntimeError("Direct app launching is currently supported on macOS only.")
            try:
                subprocess.run(
                    ["/usr/bin/open", "-a", app_name],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self._record_action("launch_app", (requested_app,), {})
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Could not launch {app_name}: {exc}") from exc

        self._ns["launch_app"] = _launch_app

        # Pre-import common modules so the model can use them without imports
        import pyautogui  # noqa: F401 — available in model's namespace
        import time  # noqa: F401

        # Wrap pyautogui so pointer/keyboard actions are recorded for guide
        # learning. The model still sees a normal `pyautogui`.
        self._real_pyautogui = pyautogui
        self._ns["pyautogui"] = _RecordingPyAutoGUI(pyautogui, self._record_action)
        self._ns["time"] = time

    def _record_action(self, kind: str, args: tuple, kwargs: dict) -> None:
        """Callback invoked by the recording proxy before each action fires."""
        x, y = self._resolve_xy(kind, args, kwargs)
        self._actions.append(
            {"kind": kind, "args": list(args), "kwargs": dict(kwargs), "x": x, "y": y}
        )

    def _resolve_xy(self, kind: str, args: tuple, kwargs: dict) -> tuple[int | None, int | None]:
        """
        Determine the pointer coordinates for an action.

        For click-like calls the first two positional args (or x=/y= kwargs) are
        the target; otherwise we read the current pointer position.
        """
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is None and len(args) >= 1 and isinstance(args[0], (int, float)):
            x = args[0]
        if y is None and len(args) >= 2 and isinstance(args[1], (int, float)):
            y = args[1]
        if x is None or y is None:
            try:
                pos = self._real_pyautogui.position()
                x = int(pos.x) if x is None else int(x)
                y = int(pos.y) if y is None else int(y)
            except Exception:  # noqa: BLE001
                return (None if x is None else int(x), None if y is None else int(y))
        return int(x), int(y)

    @property
    def captured_actions(self) -> list[dict[str, Any]]:
        """All desktop actions recorded so far this session (in order)."""
        return list(self._actions)

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
                "image_url": {"url": data_url},
            })

        return blocks
