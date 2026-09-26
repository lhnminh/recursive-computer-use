from __future__ import annotations

import unittest

from recursive_computer_use.evolution import (
    EvaluationMetrics,
    EvolutionValidationError,
    HarnessPolicy,
    ReplayCase,
    annealed_edit_budget,
    evaluate_pruning_ablation,
    evaluate_replay_suite,
    propose_policy,
    review_proposal,
)


def policy(version: int = 1) -> HarnessPolicy:
    return HarnessPolicy(
        task_key="form-family",
        version=version,
        parent_version=version - 1 or None,
        status="accepted",
        rules=("Inspect the screen before acting.",),
        limits={
            "max_actions_without_screenshot": 3,
            "action_budget": 30,
            "retry_limit": 2,
            "tool_allowlist": ["click", "type", "screenshot"],
        },
        reason="Accepted policy.",
    )


def metrics(
    success: float,
    *,
    wrong_clicks: int = 0,
    wrong_fields: int = 0,
    actions: int = 5,
) -> EvaluationMetrics:
    return EvaluationMetrics(
        success_rate=success,
        wrong_clicks=wrong_clicks,
        wrong_field_entries=wrong_fields,
        action_count=actions,
    )


class ProposalRegularizationTests(unittest.TestCase):
    def test_edit_budget_shrinks_with_policy_age(self):
        self.assertEqual(annealed_edit_budget(1), 3)
        self.assertEqual(annealed_edit_budget(4), 2)
        self.assertEqual(annealed_edit_budget(8), 1)

    def test_mature_policy_rejects_bundled_hypotheses(self):
        parent = policy(version=8)
        review = review_proposal(
            parent,
            {
                "rules": [*parent.rules, "Verify focus before typing."],
                "screenshot_cadence": 1,
            },
        )
        self.assertFalse(review.accepted)
        self.assertEqual(review.edit_count, 2)
        self.assertEqual(review.edit_budget, 1)

    def test_leakage_critic_blocks_protected_answer(self):
        parent = policy()
        with self.assertRaisesRegex(EvolutionValidationError, "protected literal"):
            propose_policy(
                parent,
                {"rules": [*parent.rules, "Always type Ada Lovelace into the name field."]},
                reason="Learned from one failed form.",
                protected_literals=["Ada Lovelace"],
            )

    def test_general_rule_passes_leakage_critic(self):
        parent = policy()
        candidate = propose_policy(
            parent,
            {"rules": [*parent.rules, "Verify the focused field before typing."]},
            reason="General focus check.",
            protected_literals=["Ada Lovelace"],
        )
        self.assertEqual(candidate.status, "candidate")


class ReplayGateTests(unittest.TestCase):
    def test_accepts_evolve_gain_with_clean_regression_replay(self):
        decision = evaluate_replay_suite(
            [
                ReplayCase(
                    "new-form",
                    "evolve",
                    metrics(0.0, wrong_fields=1),
                    metrics(1.0),
                    baseline_tokens=1_000,
                    candidate_tokens=1_100,
                ),
                ReplayCase(
                    "old-form",
                    "regression",
                    metrics(1.0),
                    metrics(1.0),
                    baseline_tokens=1_000,
                    candidate_tokens=950,
                ),
                ReplayCase(
                    "secret-form",
                    "holdout",
                    metrics(1.0),
                    metrics(0.0, wrong_clicks=10),
                ),
            ]
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.holdout_tasks_ignored, 1)

    def test_rejects_regression_even_when_evolve_task_improves(self):
        decision = evaluate_replay_suite(
            [
                ReplayCase("new", "evolve", metrics(0.0), metrics(1.0)),
                ReplayCase("old", "regression", metrics(1.0), metrics(0.0)),
            ]
        )
        self.assertFalse(decision.accepted)
        self.assertIn("regression success fell", " ".join(decision.reasons))

    def test_rejects_unjustified_token_growth(self):
        decision = evaluate_replay_suite(
            [
                ReplayCase(
                    "new",
                    "evolve",
                    metrics(0.49),
                    metrics(0.50),
                    baseline_tokens=100,
                    candidate_tokens=10_000,
                ),
                ReplayCase("old", "regression", metrics(1.0), metrics(1.0)),
            ],
            max_tokens_per_success_point=100,
        )
        self.assertFalse(decision.accepted)
        self.assertIn("token cost increased", " ".join(decision.reasons))

    def test_pruning_requires_equal_results_and_lower_cost(self):
        accepted = evaluate_pruning_ablation(
            [
                ReplayCase(
                    "old",
                    "regression",
                    metrics(1.0, actions=8),
                    metrics(1.0, actions=6),
                    baseline_tokens=1_000,
                    candidate_tokens=900,
                )
            ]
        )
        self.assertTrue(accepted.accepted)

        regressed = evaluate_pruning_ablation(
            [
                ReplayCase(
                    "old",
                    "regression",
                    metrics(1.0, actions=8),
                    metrics(0.0, actions=6),
                )
            ]
        )
        self.assertFalse(regressed.accepted)


if __name__ == "__main__":
    unittest.main()
