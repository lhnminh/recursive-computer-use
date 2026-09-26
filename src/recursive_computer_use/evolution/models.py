"""Validated data contracts for durable harness evolution.

The evolution layer intentionally stores compact, redacted summaries rather
than screenshots, keystrokes, or complete desktop traces.  These models are
the boundary between the local computer-use process and MongoDB Atlas.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


MAX_TEXT_LENGTH = 2_000
MAX_RULES = 24
ALLOWED_POLICY_LIMITS = frozenset(
    {
        "max_actions_without_screenshot",
        "action_budget",
        "retry_limit",
        "tool_allowlist",
    }
)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_API_KEY_RE = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b")
_CREDENTIAL_URI_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I)
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|api[_ -]?key|access[_ -]?token|secret)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)


class EvolutionValidationError(ValueError):
    """Raised when data is unsafe or outside the evolution contract."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_task_key(value: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise EvolutionValidationError(
            "task_key must be 1-128 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return value


def redact_text(value: str, *, field_name: str = "text") -> str:
    """Return a bounded, privacy-filtered summary safe for remote storage."""

    if not isinstance(value, str):
        raise EvolutionValidationError(f"{field_name} must be text")
    if value.startswith("data:image/") or "base64," in value[:128].lower():
        raise EvolutionValidationError(f"{field_name} must not contain image data")
    value = value.strip()
    if not value:
        raise EvolutionValidationError(f"{field_name} must not be empty")
    if len(value) > MAX_TEXT_LENGTH:
        raise EvolutionValidationError(
            f"{field_name} exceeds the {MAX_TEXT_LENGTH}-character summary limit"
        )

    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _API_KEY_RE.sub("[REDACTED_KEY]", value)
    value = _CREDENTIAL_URI_RE.sub(r"\g<scheme>[REDACTED]@", value)
    value = _NAMED_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    return value


def _normalise_created_at(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Experience:
    task_key: str
    outcome: str
    failure_tags: tuple[str, ...]
    summary: str
    lesson: str
    policy_version: int
    metrics: Mapping[str, float | int] = field(default_factory=dict)
    embedding: tuple[float, ...] | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", validate_task_key(self.task_key))
        if self.outcome not in {"success", "failure"}:
            raise EvolutionValidationError("outcome must be 'success' or 'failure'")
        tags = tuple(dict.fromkeys(self.failure_tags))
        if any(not _TAG_RE.fullmatch(tag) for tag in tags):
            raise EvolutionValidationError("failure_tags must use lowercase slug values")
        object.__setattr__(self, "failure_tags", tags)
        object.__setattr__(self, "summary", redact_text(self.summary, field_name="summary"))
        object.__setattr__(self, "lesson", redact_text(self.lesson, field_name="lesson"))
        if not isinstance(self.policy_version, int) or self.policy_version < 1:
            raise EvolutionValidationError("policy_version must be a positive integer")
        allowed_metrics = {
            "success_rate",
            "wrong_clicks",
            "wrong_field_entries",
            "policy_violations",
            "action_count",
            "duration_ms",
        }
        if set(self.metrics) - allowed_metrics:
            raise EvolutionValidationError("experience contains unsupported metrics")
        object.__setattr__(self, "metrics", dict(self.metrics))
        if self.embedding is not None:
            vector = tuple(float(value) for value in self.embedding)
            if not vector or len(vector) > 4096:
                raise EvolutionValidationError("embedding must contain 1-4096 numbers")
            object.__setattr__(self, "embedding", vector)
        object.__setattr__(self, "created_at", _normalise_created_at(self.created_at))

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["failure_tags"] = list(self.failure_tags)
        if self.embedding is None:
            document.pop("embedding")
        else:
            document["embedding"] = list(self.embedding)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Experience":
        return cls(
            task_key=document["task_key"],
            outcome=document["outcome"],
            failure_tags=tuple(document.get("failure_tags", ())),
            summary=document["summary"],
            lesson=document["lesson"],
            policy_version=document["policy_version"],
            metrics=document.get("metrics", {}),
            embedding=(
                tuple(document["embedding"])
                if document.get("embedding") is not None
                else None
            ),
            created_at=document.get("created_at", utcnow()),
        )


def _validate_limits(limits: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(limits) - ALLOWED_POLICY_LIMITS
    if unknown:
        raise EvolutionValidationError(
            f"unsupported policy limit(s): {', '.join(sorted(unknown))}"
        )
    result = dict(limits)
    for name in ("max_actions_without_screenshot", "action_budget"):
        if name in result and (
            not isinstance(result[name], int) or not 1 <= result[name] <= 500
        ):
            raise EvolutionValidationError(f"{name} must be an integer from 1 to 500")
    if "retry_limit" in result and (
        not isinstance(result["retry_limit"], int)
        or not 0 <= result["retry_limit"] <= 20
    ):
        raise EvolutionValidationError("retry_limit must be an integer from 0 to 20")
    if "tool_allowlist" in result:
        tools = result["tool_allowlist"]
        if isinstance(tools, (str, bytes)) or not isinstance(tools, Sequence):
            raise EvolutionValidationError("tool_allowlist must be a sequence of tool names")
        normalised_tools = tuple(dict.fromkeys(str(tool) for tool in tools))
        if any(not _TAG_RE.fullmatch(tool) for tool in normalised_tools):
            raise EvolutionValidationError("tool_allowlist entries must be lowercase slugs")
        result["tool_allowlist"] = list(normalised_tools)
    return result


@dataclass(frozen=True)
class HarnessPolicy:
    task_key: str
    version: int
    parent_version: int | None
    status: str
    rules: tuple[str, ...]
    limits: Mapping[str, Any]
    reason: str
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", validate_task_key(self.task_key))
        if not isinstance(self.version, int) or self.version < 1:
            raise EvolutionValidationError("version must be a positive integer")
        if self.parent_version is not None and (
            not isinstance(self.parent_version, int)
            or self.parent_version < 1
            or self.parent_version >= self.version
        ):
            raise EvolutionValidationError("parent_version must be lower than version")
        if self.status not in {"candidate", "accepted", "rejected"}:
            raise EvolutionValidationError("invalid policy status")
        if not 1 <= len(self.rules) <= MAX_RULES:
            raise EvolutionValidationError(f"rules must contain 1-{MAX_RULES} entries")
        rules = tuple(redact_text(rule, field_name="rule") for rule in self.rules)
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "limits", _validate_limits(self.limits))
        object.__setattr__(self, "reason", redact_text(self.reason, field_name="reason"))
        object.__setattr__(self, "created_at", _normalise_created_at(self.created_at))

    @property
    def tool_allowlist(self) -> tuple[str, ...]:
        return tuple(self.limits.get("tool_allowlist", ()))

    def to_document(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "version": self.version,
            "parent_version": self.parent_version,
            "status": self.status,
            "rules": list(self.rules),
            "limits": dict(self.limits),
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "HarnessPolicy":
        return cls(
            task_key=document["task_key"],
            version=document["version"],
            parent_version=document.get("parent_version"),
            status=document["status"],
            rules=tuple(document["rules"]),
            limits=document.get("limits", {}),
            reason=document["reason"],
            created_at=document.get("created_at", utcnow()),
        )


@dataclass(frozen=True)
class EvaluationMetrics:
    success_rate: float
    wrong_clicks: int = 0
    wrong_field_entries: int = 0
    policy_violations: int = 0
    action_count: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.success_rate) <= 1.0:
            raise EvolutionValidationError("success_rate must be between 0 and 1")
        object.__setattr__(self, "success_rate", float(self.success_rate))
        for name in (
            "wrong_clicks",
            "wrong_field_entries",
            "policy_violations",
            "action_count",
            "duration_ms",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise EvolutionValidationError(f"{name} must be a non-negative integer")

    def to_document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationRecord:
    task_key: str
    baseline_policy_version: int
    candidate_policy_version: int
    baseline_metrics: EvaluationMetrics
    candidate_metrics: EvaluationMetrics
    decision: str
    reason: str
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_key", validate_task_key(self.task_key))
        if self.decision not in {"accepted", "rejected"}:
            raise EvolutionValidationError("decision must be accepted or rejected")
        object.__setattr__(self, "reason", redact_text(self.reason, field_name="reason"))
        object.__setattr__(self, "created_at", _normalise_created_at(self.created_at))

    def to_document(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "baseline_policy_version": self.baseline_policy_version,
            "candidate_policy_version": self.candidate_policy_version,
            "baseline_metrics": self.baseline_metrics.to_document(),
            "candidate_metrics": self.candidate_metrics.to_document(),
            "decision": self.decision,
            "reason": self.reason,
            "created_at": self.created_at,
        }
