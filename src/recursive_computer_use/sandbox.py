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

import ast
import base64
import builtins
import io
import sys
import traceback
import types
from contextlib import redirect_stdout
from typing import Any, Callable

from .store import Action, ActionStore
from .evolution.models import HarnessPolicy


def _pil_to_base64(image: Any) -> str:
    """Encode a PIL Image to a base64 PNG data URL."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _to_logical_size(image: Any, pyautogui_module: Any) -> Any:
    """Shrink a HiDPI screenshot to the logical screen size.

    On Retina displays a screenshot has 2x the pixels of the coordinate space
    that ``pyautogui`` clicks in. Without this, every coordinate the model
    reads off the image lands at twice the intended position.
    """
    try:
        screen_w, screen_h = pyautogui_module.size()
        img_w, img_h = image.size
    except Exception:
        return image
    if img_w <= screen_w and img_h <= screen_h:
        return image
    return image.resize((int(screen_w), int(screen_h)))


_RECORDED_ACTIONS = frozenset(
    {
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
        "screenshot",
    }
)

_TOOL_KIND = {
    "click": "click",
    "doubleClick": "double_click",
    "rightClick": "right_click",
    "moveTo": "move",
    "dragTo": "drag",
    "write": "type",
    "typewrite": "type",
    "press": "press",
    "hotkey": "hotkey",
    "scroll": "scroll",
    "screenshot": "screenshot",
}

_SAFE_IMPORTS = frozenset(
    {"collections", "datetime", "itertools", "json", "math", "PIL", "pyautogui", "re", "statistics", "time"}
)


class PolicyViolationError(RuntimeError):
    """Raised before an action that violates the active harness policy."""


class SandboxViolationError(RuntimeError):
    """Raised when model code reaches for interpreter internals."""


_BLOCKED_BUILTINS = frozenset(
    {
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "globals",
        "help",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "vars",
    }
)

_BLOCKED_ATTRS = frozenset(
    {
        "ag_frame",
        "cr_frame",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "gi_frame",
        "tb_frame",
        "load",
        "load_path",
        "open",
        "save",
        "show",
        "truetype",
    }
)

_UNRECORDED_ACTIONS = frozenset(
    {
        "drag",
        "dragRel",
        "hold",
        "hscroll",
        "keyDown",
        "keyUp",
        "leftClick",
        "middleClick",
        "mouseDown",
        "mouseUp",
        "move",
        "moveRel",
        "run",
        "tripleClick",
        "vscroll",
    }
)


def _blocked_name(name: str) -> bool:
    return name.startswith("_") or name in _BLOCKED_ATTRS


def _check_source(code: str) -> None:
    """Reject private names and frame access before model code executes."""

    for node in ast.walk(ast.parse(code, "<model>", "exec")):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.rsplit(".", 1)[-1]
        if name and _blocked_name(name):
            raise SandboxViolationError(
                f"access to '{name}' is blocked by the local harness"
            )


def _safe_getattr(obj: Any, name: str, *default: Any) -> Any:
    if _blocked_name(str(name)):
        raise SandboxViolationError(
            f"access to '{name}' is blocked by the local harness"
        )
    return _guard_value(getattr(obj, name, *default))


class _ModuleProxy:
    """Expose an allowlisted module without modules it imported internally."""

    def __init__(self, module: types.ModuleType) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str) -> Any:
        if _blocked_name(name):
            raise SandboxViolationError(
                f"access to '{name}' is blocked by the local harness"
            )
        module = object.__getattribute__(self, "_module")
        return _guard_value(getattr(module, name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise SandboxViolationError("modules are read-only in the local harness")

    def __repr__(self) -> str:
        module = object.__getattribute__(self, "_module")
        return f"<sandboxed module {module.__name__}>"


def _guard_value(value: Any) -> Any:
    if not isinstance(value, types.ModuleType):
        return value
    if value.__name__.split(".", 1)[0] in _SAFE_IMPORTS - {"pyautogui"}:
        return _ModuleProxy(value)
    raise SandboxViolationError(
        f"module '{value.__name__}' is blocked by the local harness"
    )


_NON_TEXT_KEYS = frozenset(
    {
        "alt",
        "backspace",
        "capslock",
        "command",
        "ctrl",
        "delete",
        "down",
        "end",
        "enter",
        "esc",
        "home",
        "left",
        "option",
        "pagedown",
        "pageup",
        "return",
        "right",
        "shift",
        "space",
        "tab",
        "up",
        "win",
    }
)


def _safe_key(value: Any) -> str:
    """Keep control-key names while hiding potentially typed characters."""

    key = str(value).lower()
    if key in _NON_TEXT_KEYS or key.startswith("f") and key[1:].isdigit():
        return key
    return "[character]"


class RecordingPyAutoGUI:
    """Transparent pyautogui proxy that emits sanitized action records.

    Persistence is deliberately best-effort.  A callback failure is swallowed
    before the real desktop action is delegated, so telemetry cannot make the
    computer-use worker less reliable.
    """

    def __init__(
        self,
        pyautogui_module: Any,
        emit: Callable[[str, int, int, str, int | None, int | None, dict[str, Any]], None]
        | None = None,
    ) -> None:
        self._pyautogui = pyautogui_module
        self._emit = emit
        self._run_id: str | None = None
        self._turn = 0
        self._tool_allowlist: set[str] | None = None
        self._action_budget = 40
        self._max_without_screenshot = 3
        self._action_count = 0
        self._actions_since_screenshot = 0

    def set_context(self, run_id: str | None, turn: int) -> None:
        self._run_id = run_id
        self._turn = turn

    def apply_policy(self, policy: HarnessPolicy) -> None:
        self._tool_allowlist = set(policy.tool_allowlist)
        self._action_budget = int(policy.limits.get("action_budget", 40))
        self._max_without_screenshot = int(
            policy.limits.get("max_actions_without_screenshot", 3)
        )
        self._action_count = 0
        self._actions_since_screenshot = 0

    def __getattr__(self, name: str) -> Any:
        if _blocked_name(name):
            raise SandboxViolationError(
                f"access to '{name}' is blocked by the local harness"
            )
        if name in _UNRECORDED_ACTIONS:
            raise PolicyViolationError(
                f"pyautogui.{name} bypasses the harness; use a recorded action"
            )
        target = getattr(self._pyautogui, name)
        if name not in _RECORDED_ACTIONS or not callable(target):
            return _guard_value(target)

        def recorded(*args: Any, **kwargs: Any) -> Any:
            if name == "screenshot":
                if args or "imageFilename" in kwargs:
                    raise PolicyViolationError("screenshot() may not write to a file")
                result = target(*args, **kwargs)
                self._actions_since_screenshot = 0
                return result
            self._authorize(name)
            self._record(name, args, kwargs)
            self._action_count += 1
            self._actions_since_screenshot += 1
            return target(*args, **kwargs)

        return recorded

    def _authorize(self, name: str) -> None:
        tool = _TOOL_KIND[name]
        reason = None
        if self._tool_allowlist is not None and tool not in self._tool_allowlist:
            reason = f"tool '{tool}' is not allowed by the active policy"
        elif self._action_count >= self._action_budget:
            reason = f"action budget of {self._action_budget} is exhausted"
        elif self._actions_since_screenshot >= self._max_without_screenshot:
            reason = (
                "a screenshot is required before another action "
                f"({self._max_without_screenshot} action limit)"
            )
        if reason is None:
            return
        if self._emit is not None and self._run_id is not None:
            try:
                self._emit(
                    self._run_id,
                    self._turn,
                    0,
                    "policy_violation",
                    None,
                    None,
                    {"tool": tool, "reason": reason},
                )
            except Exception:
                pass
        raise PolicyViolationError(reason)

    def _record(
        self, kind: str, positional: tuple[Any, ...], keyword: dict[str, Any]
    ) -> None:
        if self._emit is None or self._run_id is None:
            return
        try:
            x, y = self._coordinates(kind, positional, keyword)
            details = self._sanitize(kind, positional, keyword)
            self._emit(self._run_id, self._turn, 0, kind, x, y, details)
        except Exception:
            # Telemetry must never block or alter the real pyautogui call.
            return

    def _coordinates(
        self, kind: str, positional: tuple[Any, ...], keyword: dict[str, Any]
    ) -> tuple[int | None, int | None]:
        if kind not in {"click", "doubleClick", "rightClick", "moveTo", "dragTo"}:
            return None, None

        x = keyword.get("x", positional[0] if len(positional) > 0 else None)
        y = keyword.get("y", positional[1] if len(positional) > 1 else None)
        if x is None or y is None:
            position = self._pyautogui.position()
            if x is None:
                x = position[0]
            if y is None:
                y = position[1]
        return int(x), int(y)

    @staticmethod
    def _sanitize(
        kind: str, positional: tuple[Any, ...], keyword: dict[str, Any]
    ) -> dict[str, Any]:
        """Return BSON-safe metadata without recording text or secret keys."""

        if kind in {"write", "typewrite"}:
            text = keyword.get("message", keyword.get("text", positional[0] if positional else ""))
            result: dict[str, Any] = {"text_length": len(str(text))}
            if "interval" in keyword:
                result["interval"] = float(keyword["interval"])
            elif len(positional) > 1:
                result["interval"] = float(positional[1])
            return result

        if kind == "press":
            keys = keyword.get("keys", positional[0] if positional else ())
            if isinstance(keys, str):
                keys = (keys,)
            else:
                keys = tuple(keys)
            result = {
                "keys": [_safe_key(key) for key in keys],
                "key_count": len(keys),
            }
            presses = keyword.get("presses", positional[1] if len(positional) > 1 else 1)
            result["presses"] = int(presses)
            return result

        if kind == "hotkey":
            return {
                "keys": [_safe_key(key) for key in positional],
                "key_count": len(positional),
            }

        if kind == "scroll":
            clicks = keyword.get("clicks", positional[0] if positional else 0)
            return {"clicks": int(clicks)}

        result = {}
        for name in ("button", "clicks", "duration", "tween"):
            if name in keyword:
                value = keyword[name]
                result[name] = (
                    value
                    if isinstance(value, (str, int, float, bool)) or value is None
                    else getattr(value, "__name__", type(value).__name__)
                )
        return result


class Sandbox:
    """
    A persistent execution environment for one computer-use session.

    Usage::

        sb = Sandbox()
        result = sb.run("import pyautogui; display(pyautogui.screenshot())")
        # result is a JSON-serialisable dict
    """

    def __init__(
        self,
        *,
        store: ActionStore | None = None,
        pyautogui_module: Any | None = None,
    ) -> None:
        # Persistent namespace shared across all run() calls in this session.
        # Pre-populate with helpers the model description promises are available.
        self._ns: dict[str, Any] = {}
        self._images: list[str] = []

        # log(value) — appends to stdout-like output
        # display(pil_image) — captures a screenshot for the observation
        sandbox_self = self

        def _log(value: Any) -> None:
            print(value)

        def _display(image: Any) -> None:
            image = _to_logical_size(image, pyautogui_module)
            sandbox_self._images.append(_pil_to_base64(image))

        self._ns["log"] = _log
        self._ns["display"] = _display

        # Pre-import common modules so the model can use them without imports
        if pyautogui_module is None:
            import pyautogui as pyautogui_module
        import time  # noqa: F401

        self._store = store
        self._recorder = RecordingPyAutoGUI(pyautogui_module, self._emit_action)
        safe_builtins = {
            name: value
            for name, value in vars(builtins).items()
            if name not in _BLOCKED_BUILTINS
        }
        safe_builtins["getattr"] = _safe_getattr

        def _safe_import(
            name: str,
            globals: Any = None,
            locals: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            root = name.split(".", 1)[0]
            if root not in _SAFE_IMPORTS:
                raise ImportError(f"import of '{root}' is blocked by the local harness")
            if root == "pyautogui":
                return self._recorder
            imported = builtins.__import__(name, globals, locals, fromlist, level)
            return _ModuleProxy(imported)

        safe_builtins["__import__"] = _safe_import
        self._ns["__builtins__"] = safe_builtins
        self._ns["pyautogui"] = self._recorder
        self._ns["time"] = _ModuleProxy(time)
        # Accessibility element grounding; clicks go through the recorded proxy.
        from .elements import make_helpers

        self._ns.update(make_helpers(self._recorder))

    def set_action_context(self, run_id: str | None, turn: int) -> None:
        """Tag future desktop actions with the active run and model turn."""

        self._recorder.set_context(run_id, turn)

    def apply_policy(self, policy: HarnessPolicy) -> None:
        """Apply enforceable limits before executing any model-generated code."""

        self._recorder.apply_policy(policy)

    def _emit_action(
        self,
        run_id: str,
        turn: int,
        _seq: int,
        kind: str,
        x: int | None,
        y: int | None,
        args: dict[str, Any],
    ) -> None:
        if self._store is None:
            return
        action = Action(
            run_id=run_id,
            turn=turn,
            seq=self._store.next_seq(),
            kind=kind,
            x=x,
            y=y,
            args=args,
        )
        self._store.record_action(action)

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
            _check_source(code)
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
