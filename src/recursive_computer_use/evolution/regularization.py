"""Regularize policy proposals before they become executable candidates.

The harness deliberately keeps a much smaller mutation surface than research
systems that rewrite their own source.  Regularization still matters inside
that surface: a proposal can bundle too many independent ideas or memorize a
task-specific literal.  This module provides a deterministic pre-flight critic
for those two failure modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import HarnessPolicy


_URL_RE = re.compile(r"https?://[^\s]+", re.I)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.I)


@dataclass(frozen=True)
class ProposalReview:
    """Result of the deterministic proposal critic."""

    accepted: bool
    edit_budget: int
    edit_count: int
    components: tuple[str, ...]
    reasons: tuple[str, ...]


def annealed_edit_budget(parent_version: int) -> int:
    """Return a shrinking edit budget for a policy lineage.

    Early candidates may combine a lesson with tighter execution limits.  As
    the policy matures, candidates must isolate their hypotheses more sharply.
    """

    if parent_version <= 0:
        raise ValueError("parent_version must be positive")
    if parent_version <= 2:
        return 3
    if parent_version <= 5:
        return 2
    return 1


def review_proposal(
    parent: HarnessPolicy,
    changes: Mapping[str, Any],
    *,
    protected_literals: Sequence[str] = (),
) -> ProposalReview:
    """Screen a proposal for over-bundling and task-specific leakage.

    ``protected_literals`` should contain task answers, benchmark identifiers,
    DOM ids, or other strings that the evolved policy must not memorize.  They
    are checked only against newly added rules, never persisted by this module.
    """

    components = _changed_components(parent, changes)
    budget = annealed_edit_budget(parent.version)
    reasons: list[str] = []
    if not components:
        reasons.append("proposal does not change the parent policy")
    if len(components) > budget:
        reasons.append(
            f"proposal bundles {len(components)} edits but the current budget is {budget}"
        )

    added_rules = _added_rules(parent, changes)
    leaked = _leaked_literals(
        added_rules,
        protected_literals=protected_literals,
        task_key=parent.task_key,
    )
    if leaked:
        reasons.append(
            "new rules contain protected or task-specific literals: "
            + ", ".join(leaked)
        )

    return ProposalReview(
        accepted=not reasons,
        edit_budget=budget,
        edit_count=len(components),
        components=tuple(components),
        reasons=tuple(reasons),
    )


def _changed_components(
    parent: HarnessPolicy, changes: Mapping[str, Any]
) -> list[str]:
    components: list[str] = []
    if "rules" in changes:
        proposed = tuple(str(rule).strip() for rule in changes["rules"])
        parent_rules = tuple(rule.strip() for rule in parent.rules)
        additions = [rule for rule in proposed if rule not in parent_rules]
        removals = [rule for rule in parent_rules if rule not in proposed]
        components.extend("rule" for _ in range(max(1, len(additions) + len(removals))))
        if proposed == parent_rules:
            components = [item for item in components if item != "rule"]

    limit_changes = (
        ("screenshot_cadence", "max_actions_without_screenshot"),
        ("action_budget", "action_budget"),
        ("retry_limit", "retry_limit"),
    )
    for change_key, limit_key in limit_changes:
        if change_key in changes and changes[change_key] != parent.limits.get(limit_key):
            components.append(change_key)

    if "tool_allowlist" in changes:
        proposed_tools = tuple(dict.fromkeys(str(tool) for tool in changes["tool_allowlist"]))
        if proposed_tools != parent.tool_allowlist:
            components.append("tool_allowlist")
    return components


def _added_rules(parent: HarnessPolicy, changes: Mapping[str, Any]) -> list[str]:
    if "rules" not in changes:
        return []
    existing = set(parent.rules)
    return [str(rule) for rule in changes["rules"] if str(rule) not in existing]


def _leaked_literals(
    rules: Sequence[str], *, protected_literals: Sequence[str], task_key: str
) -> list[str]:
    if not rules:
        return []
    text = "\n".join(rules)
    folded = text.casefold()
    leaked: list[str] = []

    for label, pattern in (
        ("URL", _URL_RE),
        ("email address", _EMAIL_RE),
        ("opaque identifier", _LONG_HEX_RE),
    ):
        if pattern.search(text):
            leaked.append(label)

    candidates = [task_key, *protected_literals]
    for literal in candidates:
        normalized = str(literal).strip()
        if len(normalized) < 4:
            continue
        if normalized.casefold() in folded:
            label = "task key" if normalized == task_key else _safe_label(normalized)
            if label not in leaked:
                leaked.append(label)
    return leaked


def _safe_label(value: str) -> str:
    """Describe a protected value without echoing it into logs or errors."""

    return f"protected literal ({len(value)} chars)"
