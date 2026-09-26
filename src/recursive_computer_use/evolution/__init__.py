"""Public API for safe, persistent harness self-improvement."""

from .evaluator import evaluate_candidate
from .memory import ExperienceMemory, lessons_from
from .models import (
    EvaluationMetrics,
    EvaluationRecord,
    EvolutionValidationError,
    Experience,
    HarnessPolicy,
    redact_text,
)
from .policy import PolicyRepository, propose_policy, with_status

__all__ = [
    "EvaluationMetrics",
    "EvaluationRecord",
    "EvolutionValidationError",
    "Experience",
    "ExperienceMemory",
    "HarnessPolicy",
    "PolicyRepository",
    "evaluate_candidate",
    "lessons_from",
    "propose_policy",
    "redact_text",
    "with_status",
]
