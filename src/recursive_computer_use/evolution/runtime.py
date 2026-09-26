"""Runtime coordinator joining verified outcomes, memory, and policy evolution."""

from __future__ import annotations

from typing import Any, Mapping

from .evaluator import evaluate_candidate
from .memory import ExperienceMemory, lessons_from
from .models import EvaluationMetrics, Experience, HarnessPolicy, redact_text
from .policy import PolicyRepository, propose_policy


DEFAULT_RULES = (
    "Inspect the current screen before the first desktop action.",
    "Use short action groups and verify visible state after each group.",
    "Stop rather than guessing when the expected control is not visible.",
)
# The only lessons the harness writes. Anything else in Atlas is untrusted
# and never reaches a policy, because policy rules become the system prompt.
LESSONS = {
    "wrong_field": "Immediately before typing, inspect the screen and verify the focused field label.",
    "wrong_click": "Immediately before clicking, inspect the screen and verify the target control.",
    "policy_violation": "Use smaller action groups and remain within the enforced action budget.",
    "task_failure": "After an unexpected state, inspect the screen and revise the next action instead of retrying blindly.",
}
DEFAULT_LIMITS = {
    "max_actions_without_screenshot": 3,
    "action_budget": 40,
    "retry_limit": 2,
    "tool_allowlist": [
        "click",
        "double_click",
        "right_click",
        "move",
        "drag",
        "type",
        "press",
        "hotkey",
        "scroll",
        "screenshot",
    ],
}


def metrics_from(document: Mapping[str, Any]) -> EvaluationMetrics:
    return EvaluationMetrics(
        success_rate=float(document.get("success_rate", 0.0)),
        wrong_clicks=int(document.get("wrong_clicks", 0)),
        wrong_field_entries=int(document.get("wrong_field_entries", 0)),
        policy_violations=int(document.get("policy_violations", 0)),
        action_count=int(document.get("action_count", 0)),
        duration_ms=int(document.get("duration_ms", 0)),
    )


class EvolutionRuntime:
    """Advance one task-specific policy only from externally verified results."""

    def __init__(self, database: Any) -> None:
        self.memory = ExperienceMemory(database["experiences"])
        self.policies = PolicyRepository(
            database["policies"], database["evaluations"]
        )

    def policy_for_run(
        self, task_key: str, *, version: int | None = None
    ) -> HarnessPolicy:
        if version is not None:
            selected = self.policies.by_version(task_key, version)
            if selected is None:
                raise ValueError(f"policy {task_key} v{version} does not exist")
            return selected
        candidate = self.policies.latest_candidate(task_key)
        if candidate is not None:
            return candidate
        accepted = self.policies.latest_accepted(task_key)
        if accepted is not None:
            return accepted
        initial = HarnessPolicy(
            task_key=task_key,
            version=1,
            parent_version=None,
            status="accepted",
            rules=DEFAULT_RULES,
            limits=DEFAULT_LIMITS,
            reason="Initial conservative local computer-use policy.",
        )
        self.policies.save(initial)
        return initial

    def record_verified_run(
        self,
        policy: HarnessPolicy,
        metrics: EvaluationMetrics,
    ) -> dict[str, Any]:
        outcome = "success" if metrics.success_rate >= 1.0 else "failure"
        failure_tags = self._failure_tags(metrics)
        summary = self._summary(outcome, metrics)
        lesson = self._lesson(failure_tags)
        experience = Experience(
            task_key=policy.task_key,
            outcome=outcome,
            failure_tags=tuple(failure_tags),
            summary=summary,
            lesson=lesson,
            policy_version=policy.version,
            metrics=metrics.to_document(),
            # No client-side vector: Atlas embeds ``lesson`` with Voyage on write.
        )
        self.memory.store(experience)

        if policy.status == "candidate":
            return self._evaluate_trial(policy, metrics)
        if outcome == "failure":
            return self._propose_candidate(policy, experience)
        return {"result": "retained", "policy_version": policy.version}

    def _evaluate_trial(
        self, candidate: HarnessPolicy, candidate_metrics: EvaluationMetrics
    ) -> dict[str, Any]:
        baseline_document = self.memory.latest_for_policy(
            candidate.task_key, candidate.parent_version or 0
        )
        if not baseline_document:
            return {"result": "pending", "reason": "baseline experience unavailable"}
        baseline = self.policies.latest_accepted(candidate.task_key)
        if baseline is None or baseline.version != candidate.parent_version:
            return {"result": "pending", "reason": "baseline policy unavailable"}
        evaluation = evaluate_candidate(
            baseline,
            candidate,
            metrics_from(baseline_document.get("metrics", {})),
            candidate_metrics,
        )
        self.policies.record_evaluation(evaluation)
        return {
            "result": evaluation.decision,
            "policy_version": candidate.version,
            "reason": evaluation.reason,
        }

    def _propose_candidate(
        self, parent: HarnessPolicy, experience: Experience
    ) -> dict[str, Any]:
        memories = self.memory.find_similar(
            parent.task_key,
            query_text=f"{experience.summary} {experience.lesson}",
            limit=3,
        )
        known_lessons = set(LESSONS.values())
        learned_lessons = [
            lesson for lesson in lessons_from(memories) if lesson in known_lessons
        ]
        rules = list(parent.rules)
        for lesson in learned_lessons:
            if lesson not in rules:
                rules.append(lesson)
        rules = rules[-24:]
        changes: dict[str, Any] = {"rules": rules, "screenshot_cadence": 1}
        if experience.metrics.get("policy_violations", 0):
            changes["retry_limit"] = max(
                0, int(parent.limits.get("retry_limit", 2)) - 1
            )
        candidate = propose_policy(
            parent,
            changes,
            reason=(
                "Verified failure triggered a constrained update using similar "
                "redacted experiences from Atlas."
            ),
        )
        self.policies.save(candidate)
        return {
            "result": "candidate_created",
            "policy_version": candidate.version,
            "lessons": learned_lessons,
        }

    @staticmethod
    def _failure_tags(metrics: EvaluationMetrics) -> list[str]:
        tags = []
        if metrics.wrong_field_entries:
            tags.append("wrong_field")
        if metrics.wrong_clicks:
            tags.append("wrong_click")
        if metrics.policy_violations:
            tags.append("policy_violation")
        if metrics.success_rate < 1.0 and not tags:
            tags.append("task_failure")
        return tags

    @staticmethod
    def _summary(outcome: str, metrics: EvaluationMetrics) -> str:
        return redact_text(
            f"Verified {outcome}; wrong clicks={metrics.wrong_clicks}; "
            f"wrong fields={metrics.wrong_field_entries}; "
            f"policy violations={metrics.policy_violations}; "
            f"actions={metrics.action_count}."
        )

    @staticmethod
    def _lesson(tags: list[str]) -> str:
        for tag in ("wrong_field", "wrong_click", "policy_violation"):
            if tag in tags:
                return LESSONS[tag]
        return LESSONS["task_failure"]
