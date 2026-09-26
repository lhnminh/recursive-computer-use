from __future__ import annotations

import unittest

from PIL import Image

from recursive_computer_use.sandbox import RecordingPyAutoGUI, Sandbox, _to_logical_size


class FakePyAutoGUI:
    def __init__(self):
        self.calls = []

    def position(self):
        return (41, 73)

    def screenshot(self, *args, **kwargs):
        self.calls.append(("screenshot", args, kwargs))

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))
        return "clicked"

    def write(self, *args, **kwargs):
        self.calls.append(("write", args, kwargs))
        return "written"

    def press(self, *args, **kwargs):
        self.calls.append(("press", args, kwargs))

    def hotkey(self, *args, **kwargs):
        self.calls.append(("hotkey", args, kwargs))

    def scroll(self, *args, **kwargs):
        self.calls.append(("scroll", args, kwargs))


class FakeStore:
    def __init__(self):
        self.actions = []
        self.seq = 0

    def next_seq(self):
        self.seq += 1
        return self.seq

    def record_action(self, action):
        self.actions.append(action)


class RecordingTests(unittest.TestCase):
    def test_click_records_coordinates_and_delegates(self):
        fake = FakePyAutoGUI()
        events = []
        recorder = RecordingPyAutoGUI(fake, lambda *event: events.append(event))
        recorder.set_context("run-1", 3)

        result = recorder.click(10, 20, button="left")

        self.assertEqual(result, "clicked")
        self.assertEqual(fake.calls[0][0], "click")
        self.assertEqual(events[0][:7], ("run-1", 3, 0, "click", 10, 20, {"button": "left"}))

    def test_click_without_coordinates_uses_current_position(self):
        fake = FakePyAutoGUI()
        events = []
        recorder = RecordingPyAutoGUI(fake, lambda *event: events.append(event))
        recorder.set_context("run-1", 1)

        recorder.click()

        self.assertEqual(events[0][4:6], (41, 73))

    def test_typed_content_is_never_recorded(self):
        fake = FakePyAutoGUI()
        events = []
        recorder = RecordingPyAutoGUI(fake, lambda *event: events.append(event))
        recorder.set_context("run-1", 1)

        recorder.write("private-password", interval=0.1)
        recorder.press(["a", "tab", "b"])
        recorder.hotkey("ctrl", "c")

        serialized = repr(events)
        self.assertNotIn("private-password", serialized)
        self.assertNotIn("'a'", serialized)
        self.assertNotIn("'b'", serialized)
        self.assertEqual(events[0][6]["text_length"], 16)
        self.assertEqual(events[1][6]["keys"], ["[character]", "tab", "[character]"])
        self.assertEqual(events[2][6]["keys"], ["ctrl", "[character]"])

    def test_telemetry_failure_does_not_block_desktop_action(self):
        fake = FakePyAutoGUI()

        def fail(*_args):
            raise RuntimeError("database unavailable")

        recorder = RecordingPyAutoGUI(fake, fail)
        recorder.set_context("run-1", 1)

        result = recorder.write("still executes")

        self.assertEqual(result, "written")
        self.assertEqual(fake.calls[0][0], "write")

    def test_sandbox_emits_ordered_actions_without_real_desktop(self):
        fake_module = FakePyAutoGUI()
        store = FakeStore()
        sandbox = Sandbox(store=store, pyautogui_module=fake_module)
        sandbox.set_action_context("run-7", 2)

        result = sandbox.run(
            'pyautogui.click(5, 6); pyautogui.write("do-not-store-me")'
        )

        self.assertIsNone(result["error"])
        self.assertEqual([action.seq for action in store.actions], [1, 2])
        self.assertEqual([action.kind for action in store.actions], ["click", "write"])
        self.assertEqual(store.actions[0].run_id, "run-7")
        self.assertEqual(store.actions[0].turn, 2)
        self.assertNotIn("do-not-store-me", repr(store.actions[1].args))

    def test_sandbox_blocks_known_escapes(self):
        sandbox = Sandbox(store=FakeStore(), pyautogui_module=FakePyAutoGUI())
        escapes = [
            "pyautogui._pyautogui",
            "().__class__.__base__.__subclasses__()",
            "getattr(log, '__glob' + 'als__')",
            "(i for i in []).gi_frame",
            "import json; json.codecs",
            "import re; re.enum",
            "pyautogui._action_budget = 10**9",
            "from PIL import Image; Image.open('/etc/hosts')",
            "pyautogui.screenshot().save('/tmp/x.png')",
        ]
        for code in escapes:
            with self.subTest(code=code):
                result = sandbox.run(code)
                self.assertIn("SandboxViolationError", result["error"] or "")

    def test_sandbox_blocks_unrecorded_and_file_actions(self):
        sandbox = Sandbox(store=FakeStore(), pyautogui_module=FakePyAutoGUI())
        for code in ("pyautogui.mouseDown()", "pyautogui.screenshot('/tmp/x.png')"):
            with self.subTest(code=code):
                result = sandbox.run(code)
                self.assertIn("PolicyViolationError", result["error"] or "")

    def test_retina_screenshot_scales_to_click_coordinates(self):
        class RetinaScreen:
            def size(self):
                return (1512, 982)

        retina = Image.new("RGB", (3024, 1964))
        self.assertEqual(_to_logical_size(retina, RetinaScreen()).size, (1512, 982))
        normal = Image.new("RGB", (1512, 982))
        self.assertIs(_to_logical_size(normal, RetinaScreen()), normal)
        # A fake module without size() leaves the image alone.
        self.assertIs(_to_logical_size(retina, FakePyAutoGUI()), retina)


if __name__ == "__main__":
    unittest.main()
