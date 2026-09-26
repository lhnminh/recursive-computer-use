"""Metrics history and policy learning-curve queries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


METRIC_FIELDS = (
    "success_rate",
    "wrong_clicks",
    "wrong_field_entries",
    "policy_violations",
    "action_count",
    "duration_ms",
)


def record_metrics(
    db: Any,
    task_key: str,
    policy_version: int,
    metrics: Mapping[str, int | float],
) -> Any:
    """Insert a verified run's measurements in the run_metrics time series."""

    unknown = set(metrics) - set(METRIC_FIELDS)
    if unknown:
        raise ValueError(f"unsupported run metric(s): {', '.join(sorted(unknown))}")
    measurements = {name: metrics[name] for name in METRIC_FIELDS if name in metrics}
    return db["run_metrics"].insert_one(
        {
            "ts": datetime.now(timezone.utc),
            "meta": {"task_key": task_key, "policy_version": policy_version},
            **measurements,
        }
    )


def learning_curve(db: Any, task_key: str) -> list[dict[str, Any]]:
    """Aggregate run means and evaluation decisions by policy version."""

    pipeline = [
        {"$match": {"meta.task_key": task_key}},
        {
            "$group": {
                "_id": "$meta.policy_version",
                "runs": {"$sum": 1},
                "mean_success_rate": {"$avg": "$success_rate"},
                "mean_action_count": {"$avg": "$action_count"},
                "total_policy_violations": {"$sum": "$policy_violations"},
            }
        },
        {
            "$lookup": {
                "from": "evaluations",
                "let": {"version": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$task_key", task_key]},
                                    {"$eq": ["$candidate_policy_version", "$$version"]},
                                ]
                            }
                        }
                    },
                    {"$sort": {"created_at": -1}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "decision": 1}},
                ],
                "as": "evaluation",
            }
        },
        {
            "$project": {
                "_id": 0,
                "policy_version": "$_id",
                "runs": 1,
                "mean_success_rate": 1,
                "mean_action_count": 1,
                "total_policy_violations": 1,
                "decision": {"$ifNull": [{"$first": "$evaluation.decision"}, None]},
            }
        },
        {"$sort": {"policy_version": 1}},
    ]
    return list(db["run_metrics"].aggregate(pipeline))


def policy_lineage(db: Any, task_key: str) -> list[dict[str, Any]]:
    """Return the latest accepted policy and its ancestors from v1 onward."""

    pipeline = [
        {"$match": {"task_key": task_key, "status": "accepted"}},
        {"$sort": {"version": -1}},
        {"$limit": 1},
        {
            "$graphLookup": {
                "from": "policies",
                "startWith": "$parent_version",
                "connectFromField": "parent_version",
                "connectToField": "version",
                "restrictSearchWithMatch": {"task_key": task_key},
                "as": "ancestors",
            }
        },
        {
            "$project": {
                "_id": 0,
                "chain": {"$concatArrays": ["$ancestors", ["$$ROOT"]]},
            }
        },
        {"$unwind": "$chain"},
        {"$replaceRoot": {"newRoot": "$chain"}},
        {"$sort": {"version": 1}},
        {"$project": {"_id": 0, "task_key": 1, "version": 1, "parent_version": 1, "status": 1}},
    ]
    return list(db["policies"].aggregate(pipeline))
