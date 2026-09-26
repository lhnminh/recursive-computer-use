"""
recovery.py — Detect a stuck computer-use run and steer it back on track.

The agent loop feeds every tool result to :class:`RecoveryMonitor`. The
monitor watches for three failure signals:

  - **no effect**: desktop actions ran, but the next screenshot looks the same
    as the previous one;
  - **repeated code**: the model sends the same code as its last call;
  - **repeated error**: the code fails with the same error as its last call.

Each signal adds one to a *stuck streak*. A screen change after actions, or
a new successful result, resets it. When the streak reaches
``STUCK_THRESHOLD``, the monitor returns a hint that the agent appends to the
tool result, and counts one nudge. After more than ``retry_limit`` nudges,
:attr:`should_abort` turns true and the agent stops the run.

The monitor also owns verifier retries. When the model says it is done but
the local verifier reports failure, :meth:`verifier_retry_prompt` returns a
correction prompt while retries remain. ``retry_limit`` comes from the active
policy, so the evolution engine can tune it.

The monitor stores only counts and hashes in memory. :meth:`to_document`
returns counts and an abort reason, which are safe for MongoDB. It never
keeps screenshot bytes or code text beyond a digest.
"""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Any, Mapping

# Consecutive stuck signals before the monitor sends a hint.
STUCK_THRESHOLD = 2

# Two screenshots whose 64-bit average hashes differ in at most this many bits
# count as the same screen. This ignores a blinking cursor or a clock tick.
SAME_SCREEN_MAX_BITS = 2

ABORT_STUCK = "stuck"


def screen_hash(data_url: str) -> int | None:
    """Return a 64-bit average hash of a PNG data URL, or None if unreadable."""
    try:
        from PIL import Image

        raw = base64.b64decode(data_url.split(",", 1)[1])
        image = Image.open(io.BytesIO(raw)).convert("L").resize((8, 8))
        pixels = list(image.tobytes())
    except Exception:
        return None
    mean = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | int(value > mean)
    return bits


def same_screen(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    return bin(a ^ b).count("1") <= SAME_SCREEN_MAX_BITS


def _digest(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _last_error_line(error: str | None) -> str | None:
    if not error:
        return None
    lines = [line for line in error.strip().splitlines() if line.strip()]
    return lines[-1] if lines else None


class RecoveryMonitor:
    """Track failure signals for one run. Pure in-memory state, no I/O."""

    def __init__(self, retry_limit: int = 2) -> None:
        self.retry_limit = max(0, int(retry_limit))
        self._last_code: str | None = None
        self._last_error: str | None = None
        self._last_screen: int | None = None
        self._pending_actions = 0
        self.streak = 0
        self.nudges = 0
        self.no_effect = 0
        self.repeated_code = 0
        self.repeated_errors = 0
        self.tool_errors = 0
        self.verifier_retries = 0
        self.abort_reason: str | None = None

    @property
    def should_abort(self) -> bool:
        return self.abort_reason is not None

    def observe(
        self, code: str, result: Mapping[str, Any], actions_taken: int
    ) -> str | None:
        """Record one tool result. Return a hint for the model, or None."""
        signals: list[str] = []
        images = result.get("images") or []
        current = screen_hash(images[-1]) if images else None
        screen_changed = (
            current is not None
            and self._last_screen is not None
            and not same_screen(current, self._last_screen)
        )

        # Repeating a screenshot while a page loads is fine if the screen moved.
        code_digest = _digest(code)
        if code_digest == self._last_code and not screen_changed:
            self.repeated_code += 1
            signals.append("you sent the same code as your last call")
        self._last_code = code_digest

        error = _last_error_line(result.get("error"))
        if error:
            self.tool_errors += 1
            if error == self._last_error:
                self.repeated_errors += 1
                signals.append(f"the same error happened again ({error[:160]})")
        self._last_error = error

        # Actions can run in one call and the screenshot can come in the next.
        self._pending_actions += max(0, actions_taken)
        progress = bool(self._pending_actions) and screen_changed
        if current is not None:
            if self._pending_actions and same_screen(current, self._last_screen):
                self.no_effect += 1
                signals.append(
                    f"{self._pending_actions} desktop action(s) ran but the "
                    "screen did not change"
                )
            self._pending_actions = 0
            self._last_screen = current

        if signals:
            self.streak += 1
        elif progress or not error:
            self.streak = 0

        if self.streak < STUCK_THRESHOLD:
            return None
        self.streak = 0
        self.nudges += 1
        if self.nudges > self.retry_limit:
            self.abort_reason = ABORT_STUCK
            return None
        return (
            "[harness] You look stuck: "
            + "; ".join(signals)
            + ". Do not repeat the last approach. Take a fresh screenshot and "
            "check for a modal, banner, error message, or a field that lost "
            "focus. Then try a different method: scroll the target into view, "
            "click a different point on it, or use the keyboard (Tab, Enter). "
            f"Recovery attempt {self.nudges} of {self.retry_limit}."
        )

    def verifier_retry_prompt(self, metrics: Any) -> str | None:
        """Return a correction prompt after a failed verification, or None."""
        if metrics.success_rate >= 1.0 or self.verifier_retries >= self.retry_limit:
            return None
        self.verifier_retries += 1
        return (
            "[harness] The local verifier checked the task and it is NOT "
            f"complete (wrong clicks: {metrics.wrong_clicks}, wrong field "
            f"entries: {metrics.wrong_field_entries}). Take a screenshot, find "
            "what is wrong or missing, fix it, and submit again. Then reply "
            "with your final answer. "
            f"Verifier retry {self.verifier_retries} of {self.retry_limit}."
        )

    def to_document(self) -> dict[str, Any]:
        """Counts and abort reason only. Safe to persist."""
        return {
            "nudges": self.nudges,
            "no_effect": self.no_effect,
            "repeated_code": self.repeated_code,
            "repeated_errors": self.repeated_errors,
            "tool_errors": self.tool_errors,
            "verifier_retries": self.verifier_retries,
            "abort_reason": self.abort_reason,
        }
