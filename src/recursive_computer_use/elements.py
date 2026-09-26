"""Ground actions on accessibility elements instead of raw screen pixels.

The model reads screen coordinates off a screenshot, and it guesses them
badly. The macOS Accessibility (AX) tree gives every visible control a role,
a label, and an exact frame in the same point space that ``pyautogui`` clicks
in. This module lists those controls with short IDs (``e1``, ``e2`` ...) so
the model can write ``click_element("e7")``.

Rules, borrowed from TipTour's local harness contract:

- An ID is valid only for the snapshot that produced it. A stale or unknown
  ID raises an error; it never falls back to a nearby label.
- Clicks go through the recorded, policy-checked ``pyautogui`` proxy, so
  telemetry and enforcement stay the same as a pixel click.
- Element values reach the model only through tool output. They are never
  persisted.

Only macOS is supported. On other platforms the helpers raise
``ElementGroundingError`` and the model falls back to screenshots.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

MAX_ELEMENTS = 80
MAX_DEPTH = 60
MAX_TEXT = 60

INTERACTIVE_ROLES = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXComboBox",
        "AXLink",
        "AXMenuButton",
        "AXPopUpButton",
        "AXRadioButton",
        "AXSearchField",
        "AXSlider",
        "AXTextArea",
        "AXTextField",
    }
)
TEXT_ROLES = frozenset({"AXComboBox", "AXSearchField", "AXTextArea", "AXTextField"})


class ElementGroundingError(RuntimeError):
    """Raised when an element cannot be listed or resolved."""


@dataclass(frozen=True)
class RawElement:
    """One accessibility node as the backend reports it."""

    role: str
    label: str
    x: float
    y: float
    width: float
    height: float
    focused: bool = False
    value: str | None = None


@dataclass(frozen=True)
class Element:
    """A listed element with a snapshot-scoped ID."""

    id: str
    role: str
    label: str
    center: tuple[int, int]
    size: tuple[int, int]
    focused: bool
    value: str | None

    def describe(self) -> str:
        role = self.role.removeprefix("AX")
        parts = [f"{self.id} {role} {self.label!r}"]
        if self.value is not None:
            parts.append(f"value={self.value!r}")
        if self.focused:
            parts.append("FOCUSED")
        parts.append(f"at {self.center}")
        return " ".join(parts)


class Backend(Protocol):
    def snapshot(self, app: str | None) -> list[RawElement]: ...


class ElementIndex:
    """Holds the latest snapshot and resolves IDs against it."""

    def __init__(self, backend: Backend, *, screen_size: Callable[[], tuple[int, int]] | None = None) -> None:
        self._backend = backend
        self._screen_size = screen_size
        self._snapshot: dict[str, Element] = {}
        self._generation = 0

    def refresh(self, app: str | None = None) -> list[Element]:
        raw = self._backend.snapshot(app)
        bounds = self._bounds()
        elements: list[Element] = []
        for node in raw:
            if node.role not in INTERACTIVE_ROLES or node.width < 1 or node.height < 1:
                continue
            cx, cy = int(node.x + node.width / 2), int(node.y + node.height / 2)
            if bounds and not (0 <= cx < bounds[0] and 0 <= cy < bounds[1]):
                continue
            elements.append(
                Element(
                    id=f"e{len(elements) + 1}",
                    role=node.role,
                    label=_clip(node.label),
                    center=(cx, cy),
                    size=(int(node.width), int(node.height)),
                    focused=node.focused,
                    value=_clip(node.value) if node.role in TEXT_ROLES and node.value is not None else None,
                )
            )
            if len(elements) >= MAX_ELEMENTS:
                break
        self._snapshot = {element.id: element for element in elements}
        self._generation += 1
        return elements

    def resolve(self, element_id: str) -> Element:
        element = self._snapshot.get(str(element_id))
        if element is None:
            raise ElementGroundingError(
                f"unknown element id {element_id!r}; call elements() again and use an id from that list"
            )
        return element

    def focused(self) -> Element | None:
        for element in self._snapshot.values():
            if element.focused:
                return element
        return None

    def _bounds(self) -> tuple[int, int] | None:
        if self._screen_size is None:
            return None
        try:
            width, height = self._screen_size()
            return int(width), int(height)
        except Exception:
            return None


def make_helpers(pyautogui_proxy: Any, *, backend: Backend | None = None) -> dict[str, Callable[..., Any]]:
    """Return the sandbox helpers ``elements``, ``click_element`` and ``focused_element``.

    ``pyautogui_proxy`` must be the recorded proxy, never the raw module.
    """

    index = ElementIndex(backend or default_backend(), screen_size=getattr(pyautogui_proxy, "size", None))

    def elements(app: str | None = None) -> list[str]:
        """List clickable elements of the frontmost app (or ``app``) and print them."""
        listed = index.refresh(app)
        lines = [element.describe() for element in listed] or ["(no interactive elements found)"]
        print("\n".join(lines))
        return lines

    def click_element(element_id: str) -> str:
        """Click the center of an element from the latest ``elements()`` list."""
        element = index.resolve(element_id)
        pyautogui_proxy.click(*element.center)
        return element.describe()

    def focused_element() -> str | None:
        """Re-read the tree and return the focused element, or None."""
        index.refresh()
        element = index.focused()
        return element.describe() if element else None

    return {"elements": elements, "click_element": click_element, "focused_element": focused_element}


def default_backend() -> Backend:
    if sys.platform == "darwin":
        return MacAXBackend()
    return _UnsupportedBackend()


class _UnsupportedBackend:
    def snapshot(self, app: str | None) -> list[RawElement]:
        raise ElementGroundingError("element grounding needs macOS; use screenshots instead")


class MacAXBackend:
    """Reads the macOS Accessibility tree of one running app."""

    def snapshot(self, app: str | None) -> list[RawElement]:
        try:
            import ApplicationServices as ax
            from AppKit import NSWorkspace
        except ImportError as exc:  # pragma: no cover - platform dependency
            raise ElementGroundingError(
                "install pyobjc-framework-ApplicationServices for element grounding"
            ) from exc
        if not ax.AXIsProcessTrusted():
            raise ElementGroundingError("grant Accessibility permission to this terminal")

        pid = _target_pid(ax, NSWorkspace.sharedWorkspace(), app)
        root = ax.AXUIElementCreateApplication(pid)
        # Chromium builds its web AX tree only when an assistive client asks.
        for flag in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            ax.AXUIElementSetAttributeValue(root, flag, True)

        out: list[RawElement] = []
        for window in _attr(ax, root, "AXWindows") or []:
            _walk(ax, window, out, 0)
        return out


def _target_pid(ax: Any, workspace: Any, app: str | None) -> int:
    if app is None:
        # Ask AX, not NSWorkspace: without an AppKit run loop,
        # NSWorkspace.frontmostApplication() never updates after the first call.
        focused = _attr(ax, ax.AXUIElementCreateSystemWide(), "AXFocusedApplication")
        if focused is not None:
            err, pid = ax.AXUIElementGetPid(focused, None)
            if err == 0:
                return int(pid)
        pid = _frontmost_window_pid()
        if pid is None:
            raise ElementGroundingError("no frontmost application")
        return pid
    wanted = app.lower()
    for running in workspace.runningApplications():
        name = (running.localizedName() or "").lower()
        if wanted in name:
            return int(running.processIdentifier())
    raise ElementGroundingError(f"no running application matches {app!r}")


def _frontmost_window_pid() -> int | None:
    """Owner of the frontmost normal window, from the window server."""
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if window.get("kCGWindowLayer") == 0 and window.get("kCGWindowOwnerPID"):
            return int(window["kCGWindowOwnerPID"])
    return None


def _walk(ax: Any, node: Any, out: list[RawElement], depth: int) -> None:
    if depth > MAX_DEPTH or len(out) >= MAX_ELEMENTS * 4:
        return
    role = _attr(ax, node, "AXRole")
    if role in INTERACTIVE_ROLES:
        frame = _frame(ax, node)
        if frame is not None:
            value = _attr(ax, node, "AXValue") if role in TEXT_ROLES else None
            out.append(
                RawElement(
                    role=str(role),
                    label=_label(ax, node),
                    x=frame[0],
                    y=frame[1],
                    width=frame[2],
                    height=frame[3],
                    focused=bool(_attr(ax, node, "AXFocused")),
                    value=str(value) if isinstance(value, str) else None,
                )
            )
    for child in _attr(ax, node, "AXChildren") or []:
        _walk(ax, child, out, depth + 1)


def _attr(ax: Any, node: Any, name: str) -> Any:
    err, value = ax.AXUIElementCopyAttributeValue(node, name, None)
    return value if err == 0 else None


def _frame(ax: Any, node: Any) -> tuple[float, float, float, float] | None:
    position, size = _attr(ax, node, "AXPosition"), _attr(ax, node, "AXSize")
    if position is None or size is None:
        return None
    ok_p, point = ax.AXValueGetValue(position, ax.kAXValueCGPointType, None)
    ok_s, extent = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    return float(point.x), float(point.y), float(extent.width), float(extent.height)


def _label(ax: Any, node: Any) -> str:
    for name in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
        value = _attr(ax, node, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title_element = _attr(ax, node, "AXTitleUIElement")
    if title_element is not None:
        value = _attr(ax, title_element, "AXValue") or _attr(ax, title_element, "AXTitle")
        if isinstance(value, str):
            return value.strip()
    return ""


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


__all__: Sequence[str] = (
    "Element",
    "ElementGroundingError",
    "ElementIndex",
    "MacAXBackend",
    "RawElement",
    "make_helpers",
)
