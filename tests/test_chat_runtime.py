import unittest

from recursive_computer_use.chat_runtime import (
    ChatOptions,
    build_agent_prompt,
    execute_task,
    normalize_task_key,
    safe_error,
    validate_verifier_url,
)


class ChatRuntimeTests(unittest.TestCase):
    def test_prompt_is_passed_through_unchanged_by_default(self):
        self.assertEqual(
            build_agent_prompt("Draft an email", stop_before_irreversible=False),
            "Draft an email",
        )

    def test_task_key_is_bounded_and_normalized(self):
        self.assertEqual(normalize_task_key("  Job Applications / Workday  "), "job-applications-workday")
        self.assertLessEqual(len(normalize_task_key("x" * 200)), 80)

    def test_verifier_must_be_local_http(self):
        self.assertEqual(
            validate_verifier_url("http://127.0.0.1:8765/api/result"),
            "http://127.0.0.1:8765/api/result",
        )
        with self.assertRaises(ValueError):
            validate_verifier_url("https://example.com/result")

    def test_execute_task_uses_existing_agent_contract(self):
        calls = []

        def runner(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "done"

        result = execute_task(
            "Open the browser",
            ChatOptions(
                task_key="Browser Tasks",
                verifier_url="http://localhost:8765/api/result",
                log_actions=False,
                evolve=True,
            ),
            runner=runner,
        )

        self.assertEqual(result, "done")
        self.assertEqual(calls[0][1]["task_key"], "browser-tasks")
        self.assertFalse(calls[0][1]["log_actions"])
        self.assertTrue(calls[0][1]["evolve"])

    def test_web_defaults_match_terminal_runtime_defaults(self):
        calls = []

        def runner(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "done"

        execute_task("  Click the button  ", ChatOptions(), runner=runner)

        self.assertEqual(calls[0][0], "Click the button")
        self.assertEqual(calls[0][1]["task_key"], "general-desktop")
        self.assertTrue(calls[0][1]["log_actions"])
        self.assertTrue(calls[0][1]["evolve"])

    def test_errors_redact_credentials(self):
        message = safe_error(
            RuntimeError("mongodb+srv://user:pass@example/db token=abcdef sk-secret12345")
        )
        self.assertNotIn("user:pass", message)
        self.assertNotIn("abcdef", message)
        self.assertNotIn("sk-secret12345", message)


if __name__ == "__main__":
    unittest.main()
