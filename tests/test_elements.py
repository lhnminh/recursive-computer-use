from __future__ import annotations

import contextlib
import io
import unittest

from recursive_computer_use.elements import (
    ElementGroundingError,
    RawElement,
    make_helpers,
)
from recursive_computer_use.sandbox import RecordingPyAutoGUI


class FakeBackend:
    def __init__(self, frames):
        self.frames = list(frames)
        self.apps = []

    def snapshot(self, app):
        self.apps.append(app)
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class FakePyAutoGUI:
    def __init__(self):
        self.calls = []

    def size(self):
        return (1512, 982)

    def position(self):
        return (0, 0)

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))


FORM = [
    RawElement("AXTextField", "Full name", 282, 565, 760, 53, value="Ada"),
    RawElement("AXGroup", "wrapper", 0, 0, 1000, 1000),  # not interactive
    RawElement("AXTextField", "Email", 282, 659, 760, 53, focused=True, value=""),
    RawElement("AXButton", "Hidden", 5000, 5000, 10, 10),  # off screen
    RawElement("AXButton", "Complete check-in", 282, 822, 155, 40),
]


def _helpers(frames):
    fake = FakePyAutoGUI()
    events = []
    recorder = RecordingPyAutoGUI(fake, lambda *event: events.append(event))
    recorder.set_context("run-1", 1)
    return make_helpers(recorder, backend=FakeBackend(frames)), fake, events


class ElementTests(unittest.TestCase):
    def test_lists_only_visible_interactive_elements(self):
        helpers, _, _ = _helpers([FORM])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lines = helpers["elements"]()
        self.assertEqual(len(lines), 3)
        self.assertIn("e1 TextField 'Full name' value='Ada' at (662, 591)", lines[0])
        self.assertIn("FOCUSED", lines[1])
        self.assertIn("e3 Button 'Complete check-in'", lines[2])
        self.assertIn("e3", out.getvalue())

    def test_click_element_uses_the_recorded_proxy(self):
        helpers, fake, events = _helpers([FORM])
        with contextlib.redirect_stdout(io.StringIO()):
            helpers["elements"]()
        helpers["click_element"]("e3")
        self.assertEqual(fake.calls, [("click", (359, 842), {})])
        self.assertEqual(events[0][3], "click")

    def test_unknown_or_stale_id_never_falls_back(self):
        helpers, fake, _ = _helpers([FORM, FORM[:1]])
        with self.assertRaises(ElementGroundingError):
            helpers["click_element"]("e1")  # no snapshot yet
        with contextlib.redirect_stdout(io.StringIO()):
            helpers["elements"]()
            helpers["elements"]()  # page changed: only one element left
        with self.assertRaises(ElementGroundingError):
            helpers["click_element"]("e3")
        self.assertEqual(fake.calls, [])

    def test_focused_element_rereads_the_tree(self):
        moved = [RawElement("AXTextField", "Full name", 282, 565, 760, 53, focused=True)]
        helpers, _, _ = _helpers([FORM, moved])
        with contextlib.redirect_stdout(io.StringIO()):
            helpers["elements"]()
        self.assertIn("'Full name'", helpers["focused_element"]())


if __name__ == "__main__":
    unittest.main()
