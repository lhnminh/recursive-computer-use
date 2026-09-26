"""Constrained, versioned mutations of the computer-use harness policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from pymongo.errors import ConfigurationError, DuplicateKeyError, InvalidOperation, OperationFailure

from .models import (
    EvaluationRecord,
    EvolutionValidationError,
    HarnessPolicy,
    redact_text,
)
from .regularization import review_proposal


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
    protected_literals: Sequence[str] = (),
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

    review = review_proposal(
        parent,
        changes,
        protected_literals=protected_literals,
    )
    if not review.accepted:
        raise EvolutionValidationError(
            "proposal rejected by regularization critic: " + "; ".join(review.reasons)
        )

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
        try:
            return self.policies.insert_one(policy.to_document())
        except DuplicateKeyError as exc:
            raise EvolutionValidationError(
                f"policy {policy.task_key} v{policy.version} already exists"
            ) from exc

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
        """Atomically store evidence and decide one still-pending candidate.

        Replica sets and Atlas use a transaction.  Test doubles and standalone
        MongoDB use a compensating fallback that removes the evidence document
        if another process already decided the candidate.
        """

        document = self._evidence_document(evaluation)
        client = getattr(getattr(self.policies, "database", None), "client", None)
        if client is not None and hasattr(client, "start_session"):
            try:
                with client.start_session() as session:
                    with session.start_transaction():
                        self._decide(evaluation, session=session)
                        self.evaluations.insert_one(document, session=session)
                return
            except (ConfigurationError, InvalidOperation, NotImplementedError):
                pass
            except OperationFailure as exc:
                if not _transactions_unavailable(exc):
                    raise

        inserted = self.evaluations.insert_one(document)
        try:
            self._decide(evaluation)
        except Exception:
            inserted_id = getattr(inserted, "inserted_id", None)
            if inserted_id is not None and hasattr(self.evaluations, "delete_one"):
                self.evaluations.delete_one({"_id": inserted_id})
            raise

    def _decide(self, evaluation: EvaluationRecord, *, session: Any = None) -> None:
        kwargs = {"session": session} if session is not None else {}
        result = self.policies.update_one(
            {
                "task_key": evaluation.task_key,
                "version": evaluation.candidate_policy_version,
                "status": "candidate",
            },
            {"$set": {"status": evaluation.decision}},
            **kwargs,
        )
        matched = getattr(result, "matched_count", 1)
        if matched != 1:
            raise EvolutionValidationError(
                "candidate is no longer pending; evaluation was not recorded"
            )

    def _evidence_document(self, evaluation: EvaluationRecord) -> dict[str, Any]:
        document = evaluation.to_document()
        baseline = self._policy_for_hash(
            evaluation.task_key, evaluation.baseline_policy_version
        )
        candidate = self._policy_for_hash(
            evaluation.task_key, evaluation.candidate_policy_version
        )
        if baseline is not None:
            document["baseline_policy_sha256"] = policy_fingerprint(baseline)
        if candidate is not None:
            document["candidate_policy_sha256"] = policy_fingerprint(candidate)
        document["evidence_sha256"] = evaluation_evidence_hash(document)
        return document

    def _policy_for_hash(self, task_key: str, version: int) -> Mapping[str, Any] | None:
        if not hasattr(self.policies, "find_one"):
            return None
        return self.policies.find_one({"task_key": task_key, "version": version})


def policy_fingerprint(policy: HarnessPolicy | Mapping[str, Any]) -> str:
    """Hash immutable policy behavior, excluding mutable lifecycle status."""

    document = policy.to_document() if isinstance(policy, HarnessPolicy) else dict(policy)
    payload = {
        "task_key": document.get("task_key"),
        "version": document.get("version"),
        "parent_version": document.get("parent_version"),
        "rules": document.get("rules", []),
        "limits": document.get("limits", {}),
        "reason": document.get("reason"),
    }
    return _sha256(payload)


def evaluation_evidence_hash(document: Mapping[str, Any]) -> str:
    """Hash an evaluation record so later readers can detect modifications."""

    payload = {
        key: value
        for key, value in document.items()
        if key not in {"_id", "evidence_sha256"}
    }
    return _sha256(payload)


def verify_evaluation_evidence(document: Mapping[str, Any]) -> bool:
    expected = document.get("evidence_sha256")
    return isinstance(expected, str) and expected == evaluation_evidence_hash(document)


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot hash value of type {type(value).__name__}")


def _transactions_unavailable(exc: OperationFailure) -> bool:
    if getattr(exc, "code", None) == 20:
        return True
    message = str(exc).casefold()
    return "transaction numbers are only allowed" in message or "replica set" in message


def with_status(policy: HarnessPolicy, status: str) -> HarnessPolicy:
    """Return a validated copy after a repository evaluation decision."""

    return replace(policy, status=status)
