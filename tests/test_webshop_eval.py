from __future__ import annotations

import unittest

from scripts.webshop_eval import arm_session


class WebShopEvaluationTests(unittest.TestCase):
    def test_arms_get_isolated_sessions_for_the_same_fixed_task(self):
        recipe = arm_session("recipe", 17)
        baseline = arm_session("baseline", 17)

        self.assertNotEqual(recipe, baseline)
        self.assertEqual(recipe.rsplit("_", 1)[-1], "17")
        self.assertEqual(baseline.rsplit("_", 1)[-1], "17")

    def test_rejects_session_ids_that_do_not_preserve_fixed_task_shape(self):
        with self.assertRaises(ValueError):
            arm_session("recipe/test", 1)
        with self.assertRaises(ValueError):
            arm_session("recipe", -1)


if __name__ == "__main__":
    unittest.main()
