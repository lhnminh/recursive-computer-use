"""Constrained, versioned mutations of the computer-use harness policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from .models import (
    EvaluationRecord,
    EvolutionValidationError,
    HarnessPolicy,
    redact_text,
)


ALLOWED_CHANGE_KEYS = frozenset(
    {
        "rules",
        "screenshot_cadence",
        "action_budget",
        "retry_limit",
        "tool_allowlist",
    }
)


def propose_policy(
    parent: HarnessPolicy,
    changes: Mapping[str, Any],
    *,
    reason: str,
) -> HarnessPolicy:
    """Create a candidate policy without expanding the parent's capabilities.

    The improver may change only the explicit policy surface.  In particular,
    it cannot modify source code, replace the base prompt, create tools, or add
    tools that were not already granted by the accepted parent policy.
    """

    unknown = set(changes) - ALLOWED_CHANGE_KEYS
    if unknown:
        raise EvolutionValidationError(
            f"unsupported policy change(s): {', '.join(sorted(unknown))}"
        )
    if not changes:
        raise EvolutionValidationError("a candidate must contain at least one change")

    rules = parent.rules
    if "rules" in changes:
        proposed_rules = changes["rules"]
        if isinstance(proposed_rules, (str, bytes)) or not isinstance(
            proposed_rules, Sequence
        ):
            raise EvolutionValidationError("rules must be a sequence of strings")
        rules = tuple(str(rule) for rule in proposed_rules)

    limits = dict(parent.limits)
    if "screenshot_cadence" in changes:
        limits["max_actions_without_screenshot"] = changes["screenshot_cadence"]
    if "action_budget" in changes:
        limits["action_budget"] = changes["action_budget"]
    if "retry_limit" in changes:
        limits["retry_limit"] = changes["retry_limit"]
    if "tool_allowlist" in changes:
        proposed_tools = changes["tool_allowlist"]
        if isinstance(proposed_tools, (str, bytes)) or not isinstance(
            proposed_tools, Sequence
        ):
            raise EvolutionValidationError("tool_allowlist must be a sequence")
        parent_tools = set(parent.tool_allowlist)
        candidate_tools = set(str(tool) for tool in proposed_tools)
        added_tools = candidate_tools - parent_tools
        if added_tools:
            raise EvolutionValidationError(
                "candidate policies cannot grant new tools: "
                + ", ".join(sorted(added_tools))
            )
        limits["tool_allowlist"] = list(dict.fromkeys(str(tool) for tool in proposed_tools))

    return HarnessPolicy(
        task_key=parent.task_key,
        version=parent.version + 1,
        parent_version=parent.version,
        status="candidate",
        rules=rules,
        limits=limits,
        reason=redact_text(reason, field_name="reason"),
    )


class PolicyRepository:
    """Small persistence adapter for immutable policy versions and evals."""

    def __init__(self, policies: Any, evaluations: Any) -> None:
        self.policies = policies
        self.evaluations = evaluations

    def save(self, policy: HarnessPolicy) -> Any:
        return self.policies.insert_one(policy.to_document())

    def latest_accepted(self, task_key: str) -> HarnessPolicy | None:
        document = self.policies.find_one(
            {"task_key": task_key, "status": "accepted"},
            sort=[("version", -1)],
        )
        return HarnessPolicy.from_document(document) if document else None

    def latest_candidate(self, task_key: str) -> HarnessPolicy | None:
        document = self.policies.find_one(
            {"task_key": task_key, "status": "candidate"},
            sort=[("version", -1)],
        )
        return HarnessPolicy.from_document(document) if document else None

    def record_evaluation(self, evaluation: EvaluationRecord) -> None:
        """Store the verdict and update only the evaluated candidate's status."""

        self.evaluations.insert_one(evaluation.to_document())
        self.policies.update_one(
            {
                "task_key": evaluation.task_key,
                "version": evaluation.candidate_policy_version,
                "status": "candidate",
            },
            {"$set": {"status": evaluation.decision}},
        )


def with_status(policy: HarnessPolicy, status: str) -> HarnessPolicy:
    """Return a validated copy after a repository evaluation decision."""

    return replace(policy, status=status)
