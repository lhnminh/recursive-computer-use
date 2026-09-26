"""Read deterministic task outcomes from a local verifier endpoint."""

from __future__ import annotations

import json
from urllib.parse import urlparse
from urllib.request import urlopen

from .evolution.models import EvaluationMetrics


def fetch_local_metrics(url: str, *, timeout: float = 2.0) -> EvaluationMetrics:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("verifier URL must be a local HTTP endpoint")
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - localhost enforced above
        payload = json.loads(response.read().decode("utf-8"))
    return EvaluationMetrics(
        success_rate=1.0 if payload.get("success") else 0.0,
        wrong_clicks=int(payload.get("wrong_click_count", 0)),
        wrong_field_entries=int(payload.get("wrong_field_count", 0)),
        policy_violations=int(payload.get("policy_violations", 0)),
        action_count=int(payload.get("action_count", 0)),
        duration_ms=int(payload.get("duration_ms", 0)),
    )
