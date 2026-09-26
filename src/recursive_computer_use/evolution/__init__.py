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
from .policy import (
    PolicyRepository,
    evaluation_evidence_hash,
    policy_fingerprint,
    propose_policy,
    verify_evaluation_evidence,
    with_status,
)
from .regularization import ProposalReview, annealed_edit_budget, review_proposal
from .replay import ReplayCase, ReplayDecision, evaluate_replay_suite
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
    "ProposalReview",
    "ReplayCase",
    "ReplayDecision",
    "evaluate_candidate",
    "evaluate_replay_suite",
    "evaluation_evidence_hash",
    "embed_text",
    "DEFAULT_LIMITS",
    "DEFAULT_RULES",
    "lessons_from",
    "policy_fingerprint",
    "propose_policy",
    "redact_text",
    "annealed_edit_budget",
    "review_proposal",
    "verify_evaluation_evidence",
    "with_status",
]
