"""Public API for safe, persistent harness self-improvement."""

from .evaluator import evaluate_candidate
from .embedding import EMBEDDING_DIMENSIONS, embed_text
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
from .runtime import DEFAULT_LIMITS, DEFAULT_RULES, EvolutionRuntime

__all__ = [
    "EvaluationMetrics",
    "EvaluationRecord",
    "EvolutionValidationError",
    "Experience",
    "ExperienceMemory",
    "EvolutionRuntime",
    "EMBEDDING_DIMENSIONS",
    "HarnessPolicy",
    "PolicyRepository",
    "evaluate_candidate",
    "embed_text",
    "DEFAULT_LIMITS",
    "DEFAULT_RULES",
    "lessons_from",
    "propose_policy",
    "redact_text",
    "with_status",
]
