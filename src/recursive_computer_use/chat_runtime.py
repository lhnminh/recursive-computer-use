"""Shared runtime helpers for the local computer-use chat web UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse


DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_TASK_KEY = "general-desktop"


@dataclass(frozen=True)
class ChatOptions:
    """Options passed from the local chat surface into ``agent.run``."""

    model: str = DEFAULT_MODEL
    task_key: str = DEFAULT_TASK_KEY
    verifier_url: str | None = None
    mongodb_db: str | None = None
    log_actions: bool = True
    evolve: bool = True
    verbose: bool = False
    stop_before_irreversible: bool = False


def normalize_task_key(value: str) -> str:
    """Return a bounded, stable task-family key safe for persisted documents."""

    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return normalized[:80] or DEFAULT_TASK_KEY


def validate_verifier_url(value: str | None) -> str | None:
    """Validate the optional verifier before desktop control starts."""

    if not value or not value.strip():
        return None
    url = value.strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.netloc
    ):
        raise ValueError("Verifier URL must be a local HTTP endpoint.")
    return url


def build_agent_prompt(prompt: str, *, stop_before_irreversible: bool) -> str:
    """Prepare a task prompt, optionally adding a review boundary."""

    cleaned = prompt.strip()
    if not cleaned:
        raise ValueError("Enter a task before starting computer control.")
    return cleaned


def execute_task(
    prompt: str,
    options: ChatOptions,
    *,
    runner: Callable[..., str] | None = None,
) -> str:
    """Execute one chat task through the existing policy-enforced harness."""

    if runner is None:
        from .agent import run as runner

    runtime_prompt = build_agent_prompt(
        prompt,
        stop_before_irreversible=options.stop_before_irreversible,
    )
    verifier_url = validate_verifier_url(options.verifier_url)
    return runner(
        runtime_prompt,
        model=options.model.strip() or DEFAULT_MODEL,
        verbose=options.verbose,
        mongodb_db=options.mongodb_db or None,
        log_actions=options.log_actions,
        task_key=normalize_task_key(options.task_key),
        verifier_url=verifier_url,
        evolve=options.evolve,
    )


def safe_error(exc: BaseException) -> str:
    """Return a bounded UI error with common credentials removed."""

    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"(mongodb(?:\+srv)?://)[^/@\s]+@", r"\1***@", text, flags=re.I)
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(
        r"(?i)\b(password|passwd|api[_ -]?key|token|secret)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:800]
