from __future__ import annotations

import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from recursive_computer_use import agent
from recursive_computer_use.evolution.models import EvaluationMetrics
from recursive_computer_use.recovery import RecoveryMonitor, same_screen, screen_hash
from recursive_computer_use.store import ActionStore


def png(color: tuple[int, int, int], box: tuple[int, int, int, int] | None = None) -> str:
    image = Image.new("RGB", (64, 64), color)
    if box:
        image.paste((255, 255, 255), box)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


BLANK = png((0, 0, 0))
FORM = png((0, 0, 0), (0, 0, 32, 64))


def metrics(success: float, wrong_fields: int = 0) -> EvaluationMetrics:
    return EvaluationMetrics(
        success_rate=success,
        wrong_clicks=0,
        wrong_field_entries=wrong_fields,
        policy_violations=0,
        action_count=1,
        duration_ms=10,
    )


class ScreenHashTests(unittest.TestCase):
    def test_same_image_matches_and_different_image_does_not(self):
        self.assertTrue(same_screen(screen_hash(BLANK), screen_hash(png((0, 0, 0)))))
        self.assertFalse(same_screen(screen_hash(BLANK), screen_hash(FORM)))

    def test_unreadable_image_never_matches(self):
        self.assertIsNone(screen_hash("data:image/png;base64,bm90IGEgcG5n"))
        self.assertFalse(same_screen(None, None))


class MonitorTests(unittest.TestCase):
    def test_actions_without_screen_change_trigger_hint_then_abort(self):
        monitor = RecoveryMonitor(retry_limit=1)
        monitor.observe("display(s())", {"images": [BLANK]}, 0)
        hints = [
            monitor.observe(f"click({i})", {"images": [BLANK]}, 1) for i in range(4)
        ]
        self.assertIsNone(hints[0])
        self.assertIn("screen did not change", hints[1])
        self.assertIn("attempt 1 of 1", hints[1])
        self.assertIsNone(hints[3])
        self.assertTrue(monitor.should_abort)
        self.assertEqual(monitor.to_document()["abort_reason"], "stuck")
        self.assertEqual(monitor.no_effect, 4)

    def test_screen_change_resets_streak(self):
        monitor = RecoveryMonitor()
        monitor.observe("display(s())", {"images": [BLANK]}, 0)
        monitor.observe("click(1)", {"images": [BLANK]}, 1)
        monitor.observe("click(2)", {"images": [FORM]}, 1)
        self.assertEqual(monitor.streak, 0)
        self.assertIsNone(monitor.observe("click(3)", {"images": [FORM]}, 1))

    def test_repeated_error_is_a_signal(self):
        monitor = RecoveryMonitor()
        error = {"error": "Traceback\nNameError: name 'x' is not defined"}
        monitor.observe("x", error, 0)
        self.assertIsNone(monitor.observe("x + 0", error, 0))
        hint = monitor.observe("x + 1", error, 0)
        self.assertIn("same error", hint)
        self.assertEqual(monitor.tool_errors, 3)

    def test_repeated_screenshot_while_page_loads_is_not_stuck(self):
        monitor = RecoveryMonitor()
        monitor.observe("display(s())", {"images": [BLANK]}, 0)
        monitor.observe("display(s())", {"images": [FORM]}, 0)
        self.assertEqual(monitor.repeated_code, 0)

    def test_action_then_screenshot_in_next_call_is_compared(self):
        monitor = RecoveryMonitor()
        monitor.observe("display(s())", {"images": [BLANK]}, 0)
        monitor.observe("click(1)", {}, 1)
        monitor.observe("display(s()) ", {"images": [BLANK]}, 0)
        self.assertEqual(monitor.no_effect, 1)

    def test_verifier_retries_are_bounded(self):
        monitor = RecoveryMonitor(retry_limit=1)
        self.assertIsNone(monitor.verifier_retry_prompt(metrics(1.0)))
        self.assertIn("NOT complete", monitor.verifier_retry_prompt(metrics(0.0, 2)))
        self.assertIsNone(monitor.verifier_retry_prompt(metrics(0.0)))

    def test_document_holds_counts_only(self):
        monitor = RecoveryMonitor()
        monitor.observe("secret_password = 'hunter2'", {"images": [BLANK]}, 0)
        self.assertNotIn("hunter2", json.dumps(monitor.to_document()))


def tool_reply(code: str, call_id: str) -> SimpleNamespace:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="exec_py", arguments=json.dumps({"code": code})),
    )
    return reply(tool_calls=[call])


def reply(content: str | None = None, tool_calls=None) -> SimpleNamespace:
    message = mock.Mock()
    message.tool_calls = tool_calls
    message.content = content
    message.model_dump.return_value = {"role": "assistant", "content": content or ""}
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeSandbox:
    """Counts clicks through the store and returns scripted screenshots."""

    def __init__(self, store: ActionStore, screens: list[str]) -> None:
        self.store = store
        self.screens = screens

    def apply_policy(self, policy) -> None:
        pass

    def set_action_context(self, run_id, turn) -> None:
        pass

    def run(self, code: str) -> dict:
        if "click" in code:
            self.store._action_count += 1
        return {"stdout": "", "images": [self.screens.pop(0)], "error": None}


class AgentLoopTests(unittest.TestCase):
    def run_agent(self, replies, screens, verifier_results):
        store = ActionStore.disabled()
        client = mock.Mock()
        client.chat.completions.create.side_effect = replies
        with (
            mock.patch.object(agent, "resolve_auth", return_value=SimpleNamespace(api_key="k", base_url=None)),
            mock.patch.object(agent, "OpenAI", return_value=client),
            mock.patch.object(agent, "Sandbox", lambda store: FakeSandbox(store, screens)),
            mock.patch.object(agent, "fetch_local_metrics", side_effect=verifier_results) as verify,
        ):
            try:
                result = agent.run(
                    "fill the form",
                    action_store=store,
                    verifier_url="http://127.0.0.1:8765/api/result",
                )
            except RuntimeError as exc:
                result = exc
        return result, client, verify

    def test_failed_verification_sends_model_back_to_fix_it(self):
        result, client, verify = self.run_agent(
            [reply("done"), tool_reply("click(1)", "a"), reply("fixed")],
            [FORM],
            [metrics(0.0, 1), metrics(1.0)],
        )
        self.assertEqual(result, "fixed")
        self.assertEqual(verify.call_count, 2)
        sent = client.chat.completions.create.call_args.kwargs["messages"]
        retries = [m for m in sent if "NOT complete" in str(m.get("content"))]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["role"], "user")

    def test_stuck_run_aborts_and_still_runs_verifier(self):
        replies = [tool_reply("display(s())", "0")] + [
            tool_reply(f"click({i})", str(i)) for i in range(1, 10)
        ]
        result, client, verify = self.run_agent(replies, [BLANK] * 10, [metrics(0.0)])
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("stuck", str(result))
        self.assertEqual(verify.call_count, 1)
        # retry_limit 2 -> 3 nudges needed -> 6 stuck clicks after the first look.
        self.assertEqual(client.chat.completions.create.call_count, 7)


if __name__ == "__main__":
    unittest.main()
