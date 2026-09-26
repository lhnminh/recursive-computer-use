import unittest

from recursive_computer_use.agent import (
    _distill_steps,
    _extract_prompt_text,
    _fast_path,
    infer_site_task,
)
from recursive_computer_use.guides import Env, Guide


class CapturedActions:
    def __init__(self, actions):
        self.captured_actions = actions


class RecordingSandbox:
    def __init__(self):
        self.code = None

    def run(self, code):
        self.code = code
        return {"error": None}


class GuideCaptureAndReplayTests(unittest.TestCase):
    def setUp(self):
        self.actions = [
            {"kind": "launch_app", "args": ["Dia"], "x": None, "y": None},
            {"kind": "hotkey", "args": ["command", "t"], "x": 500, "y": 200},
            {"kind": "hotkey", "args": ["command", "l"], "x": 500, "y": 200},
            {"kind": "write", "args": ["https://www.linkedin.com/"], "x": 500, "y": 200},
            {"kind": "press", "args": ["enter"], "x": 500, "y": 200},
            {"kind": "click", "args": [900, 200], "x": 900, "y": 200},
            {"kind": "write", "args": ["hello"], "x": 800, "y": 300},
        ]
        self.env = Env(
            fingerprint="darwin-test",
            os="darwin",
            screen_width=1710,
            screen_height=1112,
            scaling=2.0,
        )

    def test_linkedin_capture_keeps_app_and_parameterizes_private_text(self):
        steps = _distill_steps(CapturedActions(self.actions), self.env, include_text=False)

        self.assertEqual(steps[0].kind, "launch_app")
        self.assertEqual(steps[0].text, "Dia")
        self.assertEqual(steps[3].parameter, "navigation_url")
        self.assertIsNone(steps[3].text)
        self.assertEqual(steps[-1].parameter, "prompt_text")
        self.assertIsNone(steps[-1].text)

    def test_replay_fills_site_and_prompt_text_without_storing_text(self):
        steps = _distill_steps(CapturedActions(self.actions), self.env, include_text=False)
        guide = Guide(
            site="linkedin.com",
            task="linkedin:workflow",
            env=self.env,
            steps=steps,
        )
        sandbox = RecordingSandbox()
        prompt = "go to linkedin.com and draft a post saying hello"
        site, _ = infer_site_task(prompt)

        ok, _ = _fast_path(
            sandbox,
            guide,
            site=site,
            prompt=prompt,
            verbose=False,
        )

        self.assertTrue(ok)
        self.assertIn("pyautogui.write('https://linkedin.com')", sandbox.code)
        self.assertIn("pyautogui.press('enter')", sandbox.code)
        self.assertIn("pyautogui.write('hello')", sandbox.code)
        self.assertNotIn("https://www.linkedin.com/", sandbox.code)

    def test_prompt_text_extraction_supports_plain_and_quoted_text(self):
        self.assertEqual(
            _extract_prompt_text("draft a post saying hello"),
            "hello",
        )
        self.assertEqual(
            _extract_prompt_text('draft a post saying "hello world"'),
            "hello world",
        )

    def test_named_services_produce_generic_guide_site_keys(self):
        cases = {
            "On Reddit, draft a post saying hello": "reddit.com",
            "Create a page in Notion": "notion.com",
            "Post this via Mastodon": "mastodon.com",
            "Search on example.org": "example.org",
        }
        for prompt, expected_site in cases.items():
            with self.subTest(prompt=prompt):
                site, task = infer_site_task(prompt)
                self.assertEqual(site, expected_site)
                self.assertEqual(task, prompt.lower())

    def test_named_service_inference_skips_articles(self):
        site, _ = infer_site_task("Click on the search button")

        self.assertIsNone(site)


if __name__ == "__main__":
    unittest.main()
