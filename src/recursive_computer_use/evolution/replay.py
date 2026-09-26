"""Deterministic regression and held-out evaluation for policy candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import EvaluationMetrics


SELECTION_SPLITS = frozenset({"evolve", "regression"})
ALL_SPLITS = SELECTION_SPLITS | {"holdout"}
_SAFETY_FIELDS = ("wrong_clicks", "wrong_field_entries", "policy_violations")


@dataclass(frozen=True)
class ReplayCase:
    """Baseline and candidate evidence for one deterministic task."""

    task_id: str
    split: str
    baseline: EvaluationMetrics
    candidate: EvaluationMetrics
    baseline_tokens: int = 0
    candidate_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if self.split not in ALL_SPLITS:
            raise ValueError(f"split must be one of {sorted(ALL_SPLITS)}")
        if self.baseline_tokens < 0 or self.candidate_tokens < 0:
            raise ValueError("token counts must be non-negative")


@dataclass(frozen=True)
class ReplayDecision:
    """Promotion decision based only on evolve and regression cases."""

    accepted: bool
    reasons: tuple[str, ...]
    evolve_tasks: int
    regression_tasks: int
    holdout_tasks_ignored: int
    success_gain: float
    token_delta: int


def evaluate_replay_suite(
    cases: Sequence[ReplayCase],
    *,
    max_tokens_per_success_point: int = 2_000,
) -> ReplayDecision:
    """Accept reusable gains while keeping holdout evidence out of selection.

    At least one evolve case and one regression case are required.  Holdout
    cases are counted for reporting but never affect promotion.  This prevents
    repeated promotion decisions from gradually training on the holdout set.
    """

    selected = [case for case in cases if case.split in SELECTION_SPLITS]
    evolve = [case for case in selected if case.split == "evolve"]
    regression = [case for case in selected if case.split == "regression"]
    holdout_count = sum(case.split == "holdout" for case in cases)
    reasons: list[str] = []

    if not evolve:
        reasons.append("at least one evolve task is required")
    if not regression:
        reasons.append("at least one regression task is required")

    for case in selected:
        for field_name in _SAFETY_FIELDS:
            before = getattr(case.baseline, field_name)
            after = getattr(case.candidate, field_name)
            if after > before:
                reasons.append(
                    f"{case.task_id}: {field_name} regressed from {before} to {after}"
                )
        if case.split == "regression" and (
            case.candidate.success_rate < case.baseline.success_rate
        ):
            reasons.append(
                f"{case.task_id}: regression success fell from "
                f"{case.baseline.success_rate:.3f} to {case.candidate.success_rate:.3f}"
            )

    baseline_evolve = _mean(case.baseline.success_rate for case in evolve)
    candidate_evolve = _mean(case.candidate.success_rate for case in evolve)
    gain = candidate_evolve - baseline_evolve
    if evolve and gain <= 0:
        reasons.append("mean evolve success did not improve")

    baseline_tokens = sum(case.baseline_tokens for case in selected)
    candidate_tokens = sum(case.candidate_tokens for case in selected)
    token_delta = candidate_tokens - baseline_tokens
    if token_delta > 0 and gain > 0:
        success_points = gain * 100.0
        allowed_delta = int(success_points * max_tokens_per_success_point)
        if token_delta > allowed_delta:
            reasons.append(
                f"token cost increased by {token_delta}, above the {allowed_delta} "
                "token budget justified by measured success gain"
            )

    return ReplayDecision(
        accepted=not reasons,
        reasons=tuple(reasons),
        evolve_tasks=len(evolve),
        regression_tasks=len(regression),
        holdout_tasks_ignored=holdout_count,
        success_gain=gain,
        token_delta=token_delta,
    )


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0
