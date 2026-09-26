"""
learning.py — Feed every harness run into the learning collections.

Per run, the agent calls two methods:

  - :meth:`LearningStore.sync_policy_skills` before the loop. Each rule of the
    active policy becomes a ``skills`` document (kind ``lesson``). Returns the
    ``skills_used`` list for the episode.
  - :meth:`LearningStore.record_episode` when the run ends. It writes one
    ``episodes`` document, upserts the ``sites`` document, bumps ``uses`` and
    ``wins`` on each skill with ``$inc``, and recomputes ``lift`` for all
    skills with one aggregation.

Lift of a skill = verified success rate of episodes that used it, minus the
success rate of episodes on the same ``task_key`` that did not. Negative lift
marks a learned skill as a pruning candidate. Retirement requires a separate
replay ablation and protected safety rules can never be retired automatically.

Only verified outcomes (``success`` / ``failure``) count toward uses, wins and
lift. The agent never grades itself.

Text fields (``episodes.task``, ``skills.description``) are embedded by Atlas
with Voyage on write. See ``schema.SEARCH_INDEXES``.

Like ``store.py``, every method is non-fatal: a MongoDB error prints a warning
and the desktop session continues.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from pymongo import UpdateOne
from pymongo.database import Database

from .evolution.replay import ReplayCase, evaluate_pruning_ablation
from .store import _safe_summary

EMBEDDING_MODEL = "voyage-4"
RETIRE_MIN_USES = 3
RETIRE_BELOW_LIFT = -0.2
VERIFIED_OUTCOMES = ("success", "failure")
# $rankFusion weights for skill retrieval. Tune here only.
SKILL_FUSION_WEIGHTS = {"semantic": 2, "keyword": 1, "lift": 1}

_URL_HOST_RE = re.compile(r"https?://([^\s/?#]+)", re.I)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _warn(what: str, exc: Exception) -> None:
    print(f"[learning] {what} failed: {exc}", file=sys.stderr)


def site_from_prompt(prompt: str, fallback: str) -> str:
    """Return the first URL host in *prompt*, or *fallback* (the task_key)."""
    match = _URL_HOST_RE.search(prompt)
    return match.group(1).lower() if match else fallback


def skill_name(task_key: str, rule: str) -> str:
    """Stable skill name for one policy rule within one task family."""
    digest = hashlib.sha1(rule.strip().encode("utf-8")).hexdigest()[:12]
    return f"{task_key}:rule:{digest}"


class LearningStore:
    """Writes episodes, sites and skills. Built from an enabled ActionStore."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.sites = db["sites"]
        self.skills = db["skills"]
        self.episodes = db["episodes"]

    @classmethod
    def for_store(cls, action_store: Any) -> "LearningStore | None":
        """Return a store sharing *action_store*'s database, or None if disabled."""
        db = getattr(action_store, "database", None)
        if not getattr(action_store, "enabled", False) or db is None:
            return None
        return cls(db)

    # -- skills ------------------------------------------------------------

    def sync_policy_skills(
        self, policy: Any, *, protected_rules: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Upsert one skill per policy rule; return the episode's ``skills_used``."""
        try:
            now = _utcnow()
            active = policy.status == "accepted"
            names = [skill_name(policy.task_key, rule) for rule in policy.rules]
            protected = set(protected_rules)
            self.skills.bulk_write(
                [
                    UpdateOne(
                        {"name": name, "version": 1},
                        {
                            "$setOnInsert": {
                                "kind": "lesson",
                                "status": "active" if active else "candidate",
                                "description": rule,
                                "scope": {"task_key": policy.task_key},
                                "uses": 0,
                                "wins": 0,
                                "lift": None,
                                "retirement_candidate": False,
                                "created_at": now,
                            },
                            "$set": {
                                "updated_at": now,
                                "protected": rule in protected,
                            },
                        },
                        upsert=True,
                    )
                    for name, rule in zip(names, policy.rules)
                ],
                ordered=False,
            )
            if active:
                # A rule that made it into an accepted policy is proven enough.
                self.skills.update_many(
                    {"name": {"$in": names}, "status": "candidate"},
                    {"$set": {"status": "active", "updated_at": now}},
                )
            docs = self.skills.find({"name": {"$in": names}, "version": 1}, {"_id": 1})
            return [{"skill_id": d["_id"], "version": 1} for d in docs]
        except Exception as exc:
            _warn("sync_policy_skills", exc)
            return []

    def update_lift(self) -> None:
        """Recompute lift and flag weak learned skills for replay ablation."""
        win = {"$cond": [{"$eq": ["$outcome", "success"]}, 1, 0]}
        rate_without = {
            "$divide": [
                {"$subtract": ["$t.w", "$per.w"]},
                {"$subtract": ["$t.n", "$per.n"]},
            ]
        }
        pipeline = [
            {"$match": {"outcome": {"$in": list(VERIFIED_OUTCOMES)}, "task_key": {"$ne": None}}},
            {"$set": {"win": win}},
            {
                "$facet": {
                    "totals": [{"$group": {"_id": "$task_key", "n": {"$sum": 1}, "w": {"$sum": "$win"}}}],
                    "per": [
                        {"$unwind": "$skills_used"},
                        {
                            "$group": {
                                "_id": {"skill": "$skills_used.skill_id", "task_key": "$task_key"},
                                "n": {"$sum": 1},
                                "w": {"$sum": "$win"},
                            }
                        },
                    ],
                }
            },
            {"$unwind": "$per"},
            {
                "$set": {
                    "t": {
                        "$first": {
                            "$filter": {
                                "input": "$totals",
                                "cond": {"$eq": ["$$this._id", "$per._id.task_key"]},
                            }
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": "$per._id.skill",
                    "lift": {
                        "$cond": [
                            {"$gt": [{"$subtract": ["$t.n", "$per.n"]}, 0]},
                            {"$subtract": [{"$divide": ["$per.w", "$per.n"]}, rate_without]},
                            None,  # every episode used it: no baseline yet
                        ]
                    },
                }
            },
            {
                "$merge": {
                    "into": "skills",
                    "on": "_id",
                    "whenMatched": [{"$set": {"lift": "$$new.lift", "updated_at": "$$NOW"}}],
                    "whenNotMatched": "discard",
                }
            },
        ]
        try:
            self.episodes.aggregate(pipeline)
            self.skills.update_many(
                {
                    "status": {"$ne": "retired"},
                    "protected": {"$ne": True},
                    "uses": {"$gte": RETIRE_MIN_USES},
                    "lift": {"$lt": RETIRE_BELOW_LIFT},
                },
                {
                    "$set": {
                        "retirement_candidate": True,
                        "retirement_reason": "negative observational lift; replay ablation required",
                        "updated_at": _utcnow(),
                    }
                },
            )
            self.skills.update_many(
                {
                    "status": {"$ne": "retired"},
                    "$or": [
                        {"protected": True},
                        {"lift": {"$gte": RETIRE_BELOW_LIFT}},
                    ],
                },
                {
                    "$set": {"retirement_candidate": False, "updated_at": _utcnow()},
                    "$unset": {"retirement_reason": ""},
                },
            )
        except Exception as exc:
            _warn("update_lift", exc)

    def retire_skill_after_ablation(
        self, skill_id: Any, cases: Sequence[ReplayCase]
    ) -> dict[str, Any]:
        """Retire one unprotected candidate only after replay proves removal safe."""

        try:
            skill = self.skills.find_one({"_id": skill_id})
            if not skill:
                return {"retired": False, "reason": "skill not found"}
            if skill.get("protected"):
                return {"retired": False, "reason": "protected skills cannot be retired"}
            if not skill.get("retirement_candidate"):
                return {"retired": False, "reason": "skill is not a pruning candidate"}
            decision = evaluate_pruning_ablation(cases)
            if not decision.accepted:
                return {"retired": False, "reason": "; ".join(decision.reasons)}
            now = _utcnow()
            result = self.skills.update_one(
                {
                    "_id": skill_id,
                    "protected": {"$ne": True},
                    "retirement_candidate": True,
                    "status": {"$ne": "retired"},
                },
                {
                    "$set": {
                        "status": "retired",
                        "retired_at": now,
                        "updated_at": now,
                        "ablation": {
                            "cases": len(cases),
                            "success_gain": decision.success_gain,
                            "token_delta": decision.token_delta,
                        },
                    }
                },
            )
            retired = getattr(result, "matched_count", 0) == 1
            return {
                "retired": retired,
                "reason": "replay ablation passed" if retired else "skill changed concurrently",
            }
        except Exception as exc:
            _warn("retire_skill_after_ablation", exc)
            return {"retired": False, "reason": "retirement persistence failed"}

    # -- episodes ----------------------------------------------------------

    def record_episode(
        self,
        *,
        run_id: str,
        prompt: str,
        task_key: str,
        model: str,
        outcome: str,
        started_at: datetime,
        policy_version: int | None = None,
        metrics: Mapping[str, Any] | None = None,
        skills_used: Sequence[Mapping[str, Any]] = (),
        llm_calls: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        recovery: Mapping[str, Any] | None = None,
        site: str | None = None,
        mode: str = "computer_use",
    ) -> None:
        """Write one episode, update its site and skills, recompute lift.

        *recovery* holds the run's failure-signal counts from
        :class:`~recursive_computer_use.recovery.RecoveryMonitor`.
        *site* defaults to the first URL host in *prompt*. *mode* is
        ``computer_use`` or ``api_recipe``.
        """
        now = _utcnow()
        site = site or site_from_prompt(prompt, task_key)
        try:
            self.episodes.insert_one(
                {
                    "run_id": run_id,
                    "task": _safe_summary(prompt) or task_key,
                    "task_key": task_key,
                    "site": site,
                    "model": model,
                    "outcome": outcome,
                    "mode": mode,
                    "held_out": False,
                    "policy_version": policy_version,
                    "metrics": dict(metrics) if metrics else None,
                    "skills_used": list(skills_used),
                    "llm_calls": llm_calls,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "recovery": dict(recovery) if recovery else None,
                    "duration_ms": int((now - started_at).total_seconds() * 1000),
                    "started_at": started_at,
                    "finished_at": now,
                }
            )
            self.sites.update_one(
                {"domain": site},
                {
                    "$setOnInsert": {"created_at": now, "held_out": False, "platform": None},
                    "$set": {"last_seen": now},
                    "$inc": {"episodes": 1},
                },
                upsert=True,
            )
        except Exception as exc:
            _warn("record_episode", exc)
            return

        if outcome not in VERIFIED_OUTCOMES or not skills_used:
            return
        try:
            self.skills.update_many(
                {"_id": {"$in": [s["skill_id"] for s in skills_used]}},
                {"$inc": {"uses": 1, "wins": int(outcome == "success")}},
            )
        except Exception as exc:
            _warn("skill $inc", exc)
        self.update_lift()

    # -- retrieval (Voyage via Atlas Automated Embedding) ------------------

    def similar_skills(
        self, text: str, *, task_key: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Non-retired skills whose description means something like *text*."""
        flt: dict[str, Any] = {"status": {"$in": ["candidate", "active"]}}
        if task_key:
            flt["scope.task_key"] = task_key
        return self._vector_search(self.skills, "skill_auto", "description", text, flt, limit)

    def ranked_skills(
        self, text: str, *, task_key: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Rank live skills with ``$rankFusion``: meaning, words and proven lift.

        - ``semantic``: Voyage vector search on ``description``.
        - ``keyword``: Atlas Search on ``description``.
        - ``lift``: active skills with positive lift, highest first.

        Falls back to :meth:`similar_skills` when fusion is unavailable.
        """
        n = max(20, limit * 10)
        live = ["candidate", "active"]
        vflt: dict[str, Any] = {"status": {"$in": live}}
        sflt: list[dict[str, Any]] = [{"in": {"path": "status", "value": live}}]
        # Only positive lift earns a boost; rank fusion ignores magnitudes.
        mflt: dict[str, Any] = {"status": "active", "lift": {"$gt": 0}}
        if task_key:
            vflt["scope.task_key"] = task_key
            sflt.append({"equals": {"path": "scope.task_key", "value": task_key}})
            mflt["scope.task_key"] = task_key
        pipelines = {
            "semantic": [
                {
                    "$vectorSearch": {
                        "index": "skill_auto",
                        "path": "description",
                        "query": {"text": text},
                        "model": EMBEDDING_MODEL,
                        "numCandidates": n * 5,
                        "limit": n,
                        "filter": vflt,
                    }
                }
            ],
            "keyword": [
                {
                    "$search": {
                        "index": "skill_text",
                        "compound": {
                            "must": [{"text": {"query": text, "path": "description"}}],
                            "filter": sflt,
                        },
                    }
                },
                {"$limit": n},
            ],
            "lift": [{"$match": mflt}, {"$sort": {"lift": -1}}, {"$limit": n}],
        }
        try:
            return list(
                self.skills.aggregate(
                    [
                        {
                            "$rankFusion": {
                                "input": {"pipelines": pipelines},
                                "combination": {"weights": SKILL_FUSION_WEIGHTS},
                            }
                        },
                        {"$limit": limit},
                        {"$set": {"score": {"$meta": "score"}}},
                    ]
                )
            )
        except Exception as exc:
            _warn("ranked_skills", exc)
            return self.similar_skills(text, task_key=task_key, limit=limit)

    def similar_episodes(
        self, text: str, *, site: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Past episodes whose task means something like *text*."""
        flt = {"site": site} if site else None
        return self._vector_search(self.episodes, "episode_auto", "task", text, flt, limit)

    @staticmethod
    def _vector_search(
        coll: Any,
        index: str,
        path: str,
        text: str,
        flt: Mapping[str, Any] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        stage: dict[str, Any] = {
            "index": index,
            "path": path,
            "query": {"text": text},
            "model": EMBEDDING_MODEL,
            "numCandidates": max(20, limit * 10),
            "limit": limit,
        }
        if flt:
            stage["filter"] = dict(flt)
        try:
            return list(
                coll.aggregate(
                    [
                        {"$vectorSearch": stage},
                        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
                    ]
                )
            )
        except Exception as exc:
            _warn(f"vector search {index}", exc)
            return []
