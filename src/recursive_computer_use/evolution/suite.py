"""Persist a multi-task replay suite and use it for candidate promotion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .models import (
    EvaluationMetrics,
    EvaluationRecord,
    EvolutionValidationError,
    HarnessPolicy,
    validate_task_key,
)
from .policy import PolicyRepository
from .replay import ReplayCase, ReplayDecision, evaluate_replay_suite


class ReplaySuiteRepository:
    """Evaluate recorded task pairs and decide one pending policy candidate."""

    def __init__(self, database: Any) -> None:
        self.database = database
        self.suites = database["replay_evaluations"]
        self.policies = PolicyRepository(
            database["policies"], database["evaluations"]
        )

    def evaluate_and_record(
        self,
        *,
        suite_id: str,
        task_key: str,
        candidate_version: int,
        cases: Sequence[ReplayCase],
    ) -> ReplayDecision:
        """Record suite evidence and atomically accept or reject its candidate."""

        suite_id = str(suite_id).strip()
        if not suite_id or len(suite_id) > 128:
            raise EvolutionValidationError("suite_id must contain 1-128 characters")
        task_key = validate_task_key(task_key)
        candidate = self.policies.by_version(task_key, candidate_version)
        if candidate is None or candidate.status != "candidate":
            raise EvolutionValidationError("requested policy is not a pending candidate")
        if candidate.parent_version is None:
            raise EvolutionValidationError("candidate has no accepted parent")
        baseline = self.policies.by_version(task_key, candidate.parent_version)
        if baseline is None or baseline.status != "accepted":
            raise EvolutionValidationError("candidate parent is not accepted")
        if not set(candidate.tool_allowlist).issubset(baseline.tool_allowlist):
            raise EvolutionValidationError("candidate expands tool access")

        decision = evaluate_replay_suite(cases)
        selected = [case for case in cases if case.split in {"evolve", "regression"}]
        baseline_metrics = _aggregate(case.baseline for case in selected)
        candidate_metrics = _aggregate(case.candidate for case in selected)
        reason = (
            "; ".join(decision.reasons)
            if decision.reasons
            else (
                f"replay passed on {decision.evolve_tasks} evolve and "
                f"{decision.regression_tasks} regression task(s); "
                f"{decision.holdout_tasks_ignored} holdout task(s) excluded from selection"
            )
        )
        evaluation = EvaluationRecord(
            task_key=task_key,
            baseline_policy_version=baseline.version,
            candidate_policy_version=candidate.version,
            baseline_metrics=baseline_metrics,
            candidate_metrics=candidate_metrics,
            decision="accepted" if decision.accepted else "rejected",
            reason=reason,
        )

        audit = {
            "suite_id": suite_id,
            "task_key": task_key,
            "baseline_policy_version": baseline.version,
            "candidate_policy_version": candidate.version,
            "decision": evaluation.decision,
            "reason": reason,
            "evolve_tasks": decision.evolve_tasks,
            "regression_tasks": decision.regression_tasks,
            "holdout_tasks_ignored": decision.holdout_tasks_ignored,
            "success_gain": decision.success_gain,
            "token_delta": decision.token_delta,
            "cases": [_case_document(case) for case in cases],
            "created_at": datetime.now(timezone.utc),
        }
        inserted = self.suites.insert_one(audit)
        try:
            self.policies.record_evaluation(evaluation)
        except Exception:
            inserted_id = getattr(inserted, "inserted_id", None)
            if inserted_id is not None:
                self.suites.delete_one({"_id": inserted_id})
            raise
        return decision


def replay_case_from_document(document: Mapping[str, Any]) -> ReplayCase:
    return ReplayCase(
        task_id=str(document["task_id"]),
        split=str(document["split"]),
        baseline=_metrics_from(document["baseline_metrics"]),
        candidate=_metrics_from(document["candidate_metrics"]),
        baseline_tokens=int(document.get("baseline_tokens", 0)),
        candidate_tokens=int(document.get("candidate_tokens", 0)),
    )


def _metrics_from(document: Mapping[str, Any]) -> EvaluationMetrics:
    return EvaluationMetrics(
        success_rate=float(document.get("success_rate", 0.0)),
        wrong_clicks=int(document.get("wrong_clicks", 0)),
        wrong_field_entries=int(document.get("wrong_field_entries", 0)),
        policy_violations=int(document.get("policy_violations", 0)),
        action_count=int(document.get("action_count", 0)),
        duration_ms=int(document.get("duration_ms", 0)),
    )


def _aggregate(metrics: Sequence[EvaluationMetrics] | Any) -> EvaluationMetrics:
    rows = list(metrics)
    if not rows:
        return EvaluationMetrics(success_rate=0.0)
    return EvaluationMetrics(
        success_rate=sum(row.success_rate for row in rows) / len(rows),
        wrong_clicks=sum(row.wrong_clicks for row in rows),
        wrong_field_entries=sum(row.wrong_field_entries for row in rows),
        policy_violations=sum(row.policy_violations for row in rows),
        action_count=sum(row.action_count for row in rows),
        duration_ms=sum(row.duration_ms for row in rows),
    )


def _case_document(case: ReplayCase) -> dict[str, Any]:
    return {
        "task_id": case.task_id,
        "split": case.split,
        "baseline_metrics": case.baseline.to_document(),
        "candidate_metrics": case.candidate.to_document(),
        "baseline_tokens": case.baseline_tokens,
        "candidate_tokens": case.candidate_tokens,
    }
