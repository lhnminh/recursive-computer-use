"""Deterministic promotion gate for candidate harness policies."""

from __future__ import annotations

from .models import EvaluationMetrics, EvaluationRecord, HarnessPolicy


_SAFETY_FIELDS = (
    "wrong_clicks",
    "wrong_field_entries",
    "policy_violations",
)


def evaluate_candidate(
    baseline_policy: HarnessPolicy,
    candidate_policy: HarnessPolicy,
    baseline_metrics: EvaluationMetrics,
    candidate_metrics: EvaluationMetrics,
) -> EvaluationRecord:
    """Accept only a genuine success gain with no safety regression."""

    failures: list[str] = []
    if candidate_policy.task_key != baseline_policy.task_key:
        failures.append("candidate task scope differs from baseline")
    if candidate_policy.parent_version != baseline_policy.version:
        failures.append("candidate is not a direct child of the baseline policy")
    if candidate_policy.status != "candidate":
        failures.append("policy is not in candidate state")
    if not set(candidate_policy.tool_allowlist).issubset(
        baseline_policy.tool_allowlist
    ):
        failures.append("candidate expands tool access")
    if candidate_metrics.success_rate <= baseline_metrics.success_rate:
        failures.append("success rate did not improve")

    for field_name in _SAFETY_FIELDS:
        baseline_value = getattr(baseline_metrics, field_name)
        candidate_value = getattr(candidate_metrics, field_name)
        if candidate_value > baseline_value:
            failures.append(
                f"{field_name} regressed from {baseline_value} to {candidate_value}"
            )

    decision = "rejected" if failures else "accepted"
    reason = "; ".join(failures) if failures else (
        "success rate improved with no wrong-click, wrong-field, policy, or tool-access regression"
    )
    return EvaluationRecord(
        task_key=baseline_policy.task_key,
        baseline_policy_version=baseline_policy.version,
        candidate_policy_version=candidate_policy.version,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        decision=decision,
        reason=reason,
    )
