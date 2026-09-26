"""
schema.py — Collections, ``$jsonSchema`` validators, indexes and Atlas search
indexes for everything the harness stores.

Learning layer (``learning.py``):
  - ``sites``          : one document per website or app, with its ``platform``.
  - ``page_templates`` : known page types, keyed by structural ``fingerprint``.
  - ``site_map``       : link edges between pages, walked with ``$graphLookup``.
  - ``skills``         : reusable rules and replay scripts, with lift.
  - ``episodes``       : one document per task attempt, with outcome and cost.
  - ``exam``           : held-out tasks and their checkers (grader-owned).

Harness (``store.py`` and ``evolution/``):
  - ``runs``, ``actions``                   : run and desktop-action telemetry.
  - ``experiences``, ``policies``, ``evaluations`` : policy evolution.

Support:
  - ``run_metrics`` : time series of verified metrics per run.
  - ``agent_state`` : per-agent change-stream resume tokens.

Text fields are embedded by Atlas Automated Embedding (Voyage ``voyage-4``),
so no embedding code runs in the harness. See ``SEARCH_INDEXES``.

Idempotent: run it again after an edit and validators are updated in place
with ``collMod``. Existing documents are kept. Search indexes are created when
missing and updated when their fields change.

Validators start in ``warn`` mode so a shape mismatch logs a warning instead of
failing a write. Flip ``VALIDATION_ACTION`` to ``"error"`` once shapes settle.

Usage::

    uv run python -m recursive_computer_use.schema
"""

from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.database import Database
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from .store import DEFAULT_DB, DEFAULT_URI, _redact_uri

VALIDATION_ACTION = "warn"  # "warn" while iterating, "error" when stable
VALIDATION_LEVEL = "moderate"  # don't re-check old docs that already mismatch

EMBEDDING_MODEL = "voyage-4"

SKILL_STATUSES = ["candidate", "active", "retired"]
SKILL_KINDS = ["replay", "pattern", "lesson"]
# "unverified": the model finished but no checker confirmed success.
EPISODE_OUTCOMES = ["success", "failure", "unverified", "timeout", "error", "interrupted"]
POLICY_STATUSES = ["candidate", "accepted", "rejected"]

_DATE = {"bsonType": "date"}
_OPT_DATE = {"bsonType": ["date", "null"]}
_STR = {"bsonType": "string"}
_OPT_STR = {"bsonType": ["string", "null"]}
_INT = {"bsonType": ["int", "long"], "minimum": 0}
_NUM = {"bsonType": ["int", "long", "double", "decimal"]}
_OBJ = {"bsonType": "object"}
_OPT_OBJ = {"bsonType": ["object", "null"]}

# A semantic target on a page: role plus accessible name, not a CSS path.
_TARGET = {
    "bsonType": "object",
    "required": ["name", "role"],
    "properties": {
        "name": _STR,  # our label for it, e.g. "add_to_cart"
        "role": _STR,  # ARIA role, e.g. "button"
        "label": _OPT_STR,  # accessible name, e.g. "Add to cart"
        "selector": _OPT_STR,  # optional fallback selector
    },
}

_METRICS = {
    "bsonType": "object",
    "properties": {
        "success_rate": _NUM,
        "wrong_clicks": _INT,
        "wrong_field_entries": _INT,
        "policy_violations": _INT,
        "action_count": _INT,
        "duration_ms": _INT,
    },
}


def _schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": properties,
        }
    }


COLLECTIONS: dict[str, dict[str, Any]] = {
    # -- learning layer ------------------------------------------------------
    "sites": {
        "validator": _schema(
            ["domain", "created_at"],
            {
                "domain": _STR,  # host[:port], or the task_key for desktop apps
                "platform": _OPT_STR,  # "shopify", "wordpress", ... or unknown
                "held_out": {"bsonType": "bool"},
                "episodes": _INT,
                "created_at": _DATE,
                "last_seen": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("domain", ASCENDING)], unique=True),
            IndexModel([("platform", ASCENDING)]),
        ],
    },
    "page_templates": {
        "validator": _schema(
            ["fingerprint", "created_at"],
            {
                "fingerprint": _STR,  # hash of DOM structure with text removed
                "summary": _STR,  # text description; Voyage embeds it
                "platform": _OPT_STR,
                "sites": {"bsonType": "array", "items": _STR},
                "url_pattern": _OPT_STR,
                "kind": _OPT_STR,  # "product", "checkout", "login", ...
                "targets": {"bsonType": "array", "items": _TARGET},
                "seen_count": _INT,
                "created_at": _DATE,
                "last_seen": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("fingerprint", ASCENDING)], unique=True),
            IndexModel([("platform", ASCENDING), ("kind", ASCENDING)]),
            IndexModel([("sites", ASCENDING)]),
        ],
    },
    "site_map": {
        # One edge per document: from page --via target--> to page.
        # Walk with $graphLookup(connectFromField="to", connectToField="from").
        "validator": _schema(
            ["site", "from", "to"],
            {
                "site": _STR,
                "from": _STR,  # URL path or template fingerprint
                "to": _STR,
                "via": _TARGET,
                "last_seen": _DATE,
            },
        ),
        "indexes": [
            IndexModel(
                [("site", ASCENDING), ("from", ASCENDING), ("to", ASCENDING)],
                unique=True,
            ),
            IndexModel([("from", ASCENDING)]),  # $graphLookup connectToField
        ],
    },
    "skills": {
        "validator": _schema(
            ["name", "kind", "status", "version", "created_at"],
            {
                "name": _STR,
                "kind": {"enum": SKILL_KINDS},
                "status": {"enum": SKILL_STATUSES},
                "version": {"bsonType": ["int", "long"], "minimum": 1},
                "description": _OPT_STR,  # Voyage embeds it
                "scope": {
                    "bsonType": "object",
                    "properties": {
                        "task_key": _OPT_STR,
                        "platform": _OPT_STR,
                        "site": _OPT_STR,
                        "template": _OPT_STR,  # page_templates.fingerprint
                    },
                },
                "steps": {"bsonType": "array"},  # free-form while the agent evolves
                "uses": _INT,
                "wins": _INT,
                "lift": {"bsonType": ["double", "int", "null"]},
                "protected": {"bsonType": "bool"},
                "retirement_candidate": {"bsonType": "bool"},
                "retirement_reason": _OPT_STR,
                "ablation": {
                    "bsonType": "object",
                    "properties": {
                        "cases": _INT,
                        "success_gain": _NUM,
                        "token_delta": {"bsonType": ["int", "long"]},
                    },
                },
                "parent_id": {"bsonType": ["objectId", "null"]},
                "created_at": _DATE,
                "updated_at": _DATE,
                "retired_at": _OPT_DATE,
                # Set only on unproven candidates; the TTL index deletes them.
                "expires_at": _OPT_DATE,
            },
        ),
        "indexes": [
            IndexModel([("name", ASCENDING), ("version", ASCENDING)], unique=True),
            IndexModel([("status", ASCENDING), ("lift", DESCENDING)]),
            IndexModel([("scope.task_key", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("scope.platform", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("scope.template", ASCENDING)]),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ],
    },
    "episodes": {
        "validator": _schema(
            ["task", "site", "outcome", "started_at"],
            {
                "run_id": _OPT_STR,  # links to runs.run_id
                "task": _STR,  # redacted prompt summary; Voyage embeds it
                "task_key": _OPT_STR,
                "site": _STR,
                "model": _OPT_STR,
                "outcome": {"enum": EPISODE_OUTCOMES},
                "held_out": {"bsonType": "bool"},
                "policy_version": {"bsonType": ["int", "long", "null"]},
                "metrics": {"bsonType": ["object", "null"]},
                "steps": {"bsonType": "array"},
                "skills_used": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["skill_id"],
                        "properties": {
                            "skill_id": {"bsonType": "objectId"},
                            "version": {"bsonType": ["int", "long"]},
                        },
                    },
                },
                "llm_calls": _INT,
                "tokens_in": _INT,
                "tokens_out": _INT,
                "cost_usd": _NUM,
                # RecoveryMonitor counts: how the run got stuck and recovered.
                "recovery": {
                    "bsonType": ["object", "null"],
                    "properties": {
                        "nudges": _INT,
                        "no_effect": _INT,
                        "repeated_code": _INT,
                        "repeated_errors": _INT,
                        "tool_errors": _INT,
                        "verifier_retries": _INT,
                        "abort_reason": _OPT_STR,
                    },
                },
                "duration_ms": _INT,
                "started_at": _DATE,
                "finished_at": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("site", ASCENDING), ("started_at", DESCENDING)]),
            IndexModel([("skills_used.skill_id", ASCENDING), ("outcome", ASCENDING)]),
            IndexModel([("outcome", ASCENDING), ("started_at", DESCENDING)]),
            IndexModel([("task_key", ASCENDING), ("started_at", DESCENDING)]),
            IndexModel([("run_id", ASCENDING)], sparse=True),
        ],
    },
    "exam": {
        # Grader-owned. The agent gets a read-only role on this collection.
        "validator": _schema(
            ["task_id", "site", "task", "checker"],
            {
                "task_id": _STR,
                "site": _STR,
                "task": _STR,
                "held_out": {"bsonType": "bool"},
                "checker": {
                    "bsonType": "object",
                    "required": ["type"],
                    "properties": {
                        "type": _STR,  # "url_matches", "db_row", "text_present", ...
                    },
                },
            },
        ),
        "indexes": [
            IndexModel([("task_id", ASCENDING)], unique=True),
            IndexModel([("site", ASCENDING)]),
        ],
    },
    # -- harness telemetry (store.py) ----------------------------------------
    "runs": {
        "validator": _schema(
            ["run_id", "status", "started_at"],
            {
                "run_id": _STR,
                "prompt_summary": _OPT_STR,
                "prompt_sha256": _OPT_STR,
                "model": _OPT_STR,
                "status": {"enum": ["running", "completed", "failed", "interrupted"]},
                "started_at": _DATE,
                "finished_at": _OPT_DATE,
                "final_summary": _OPT_STR,
                "task_key": _OPT_STR,
                "policy_version": {"bsonType": ["int", "long", "null"]},
                "verified_metrics": {"bsonType": ["object", "null"]},
                "evolution_result": _OPT_OBJ,
            },
        ),
        "indexes": [
            IndexModel([("run_id", ASCENDING)], unique=True),
            IndexModel([("task_key", ASCENDING), ("started_at", DESCENDING)]),
        ],
    },
    "actions": {
        "validator": _schema(
            ["run_id", "turn", "seq", "kind", "ts"],
            {
                "run_id": _STR,
                "turn": _INT,
                "seq": _INT,
                "kind": _STR,
                "x": {"bsonType": ["int", "long", "double", "null"]},
                "y": {"bsonType": ["int", "long", "double", "null"]},
                "args": _OBJ,
                "screenshot_ref": _OPT_STR,
                "ts": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("run_id", ASCENDING), ("seq", ASCENDING)]),
            IndexModel([("ts", ASCENDING)]),
        ],
    },
    # -- policy evolution (evolution/) ---------------------------------------
    "experiences": {
        "validator": _schema(
            ["task_key", "outcome", "summary", "lesson", "policy_version", "created_at"],
            {
                "task_key": _STR,
                "outcome": {"enum": ["success", "failure"]},
                "failure_tags": {"bsonType": "array", "items": _STR},
                "summary": _STR,
                "lesson": _STR,  # Voyage embeds it
                "policy_version": {"bsonType": ["int", "long"], "minimum": 1},
                "metrics": _METRICS,
                # Legacy 64-dim hash vector; kept only as an offline fallback.
                "embedding": {"bsonType": "array", "items": {"bsonType": "double"}},
                "created_at": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("task_key", ASCENDING), ("created_at", ASCENDING)]),
            IndexModel([("task_key", ASCENDING), ("policy_version", ASCENDING)]),
        ],
    },
    "policies": {
        "validator": _schema(
            ["task_key", "version", "status", "rules", "limits", "created_at"],
            {
                "task_key": _STR,
                "version": {"bsonType": ["int", "long"], "minimum": 1},
                "parent_version": {"bsonType": ["int", "long", "null"]},
                "status": {"enum": POLICY_STATUSES},
                "rules": {"bsonType": "array", "items": _STR, "maxItems": 24},
                "limits": _OBJ,
                "reason": _STR,
                "created_at": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("task_key", ASCENDING), ("version", ASCENDING)], unique=True),
            IndexModel([("task_key", ASCENDING), ("status", ASCENDING), ("version", DESCENDING)]),
        ],
    },
    "evaluations": {
        "validator": _schema(
            [
                "task_key",
                "baseline_policy_version",
                "candidate_policy_version",
                "decision",
                "created_at",
            ],
            {
                "task_key": _STR,
                "baseline_policy_version": {"bsonType": ["int", "long"]},
                "candidate_policy_version": {"bsonType": ["int", "long"]},
                "baseline_metrics": _METRICS,
                "candidate_metrics": _METRICS,
                "decision": {"enum": ["accepted", "rejected"]},
                "reason": _STR,
                "baseline_policy_sha256": _OPT_STR,
                "candidate_policy_sha256": _OPT_STR,
                "evidence_sha256": _OPT_STR,
                "created_at": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("task_key", ASCENDING), ("created_at", ASCENDING)]),
        ],
    },
    "replay_evaluations": {
        "validator": _schema(
            [
                "suite_id",
                "task_key",
                "baseline_policy_version",
                "candidate_policy_version",
                "decision",
                "cases",
                "created_at",
            ],
            {
                "suite_id": _STR,
                "task_key": _STR,
                "baseline_policy_version": {"bsonType": ["int", "long"]},
                "candidate_policy_version": {"bsonType": ["int", "long"]},
                "decision": {"enum": ["accepted", "rejected"]},
                "reason": _STR,
                "evolve_tasks": _INT,
                "regression_tasks": _INT,
                "holdout_tasks_ignored": _INT,
                "success_gain": _NUM,
                "token_delta": {"bsonType": ["int", "long"]},
                "cases": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": [
                            "task_id",
                            "split",
                            "baseline_metrics",
                            "candidate_metrics",
                        ],
                        "properties": {
                            "task_id": _STR,
                            "split": {"enum": ["evolve", "regression", "holdout"]},
                            "baseline_metrics": _METRICS,
                            "candidate_metrics": _METRICS,
                            "baseline_tokens": _INT,
                            "candidate_tokens": _INT,
                        },
                    },
                },
                "created_at": _DATE,
            },
        ),
        "indexes": [
            IndexModel([("suite_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("task_key", ASCENDING),
                    ("candidate_policy_version", ASCENDING),
                ]
            ),
        ],
    },
    # -- support ---------------------------------------------------------------
    "agent_state": {
        # One document per agent: {_id: agent_id, resume_token, updated_at}.
        "validator": _schema(
            ["_id", "updated_at"],
            {
                "_id": _STR,
                "resume_token": _OPT_OBJ,
                "updated_at": _DATE,
            },
        ),
        "indexes": [],
    },
}

# Time series collections. Each point:
#   {ts, meta: {task_key, policy_version, run_id}, success_rate, wrong_clicks,
#    wrong_field_entries, policy_violations, action_count, duration_ms}
TIMESERIES: dict[str, dict[str, Any]] = {
    "run_metrics": {"timeField": "ts", "metaField": "meta", "granularity": "minutes"},
}


def _auto_embed(path: str, *filters: str) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {"type": "autoEmbed", "modality": "text", "path": path, "model": EMBEDDING_MODEL}
    ]
    fields += [{"type": "filter", "path": f} for f in filters]
    return {"type": "vectorSearch", "definition": {"fields": fields}}


# Atlas Search / Vector Search indexes: {collection: {index_name: spec}}.
# autoEmbed indexes make Atlas call Voyage on every write and every query.
SEARCH_INDEXES: dict[str, dict[str, dict[str, Any]]] = {
    "experiences": {
        "experience_auto": _auto_embed("lesson", "task_key", "outcome"),
        "experience_text": {
            "type": "search",
            "definition": {
                "mappings": {
                    "dynamic": False,
                    "fields": {
                        "summary": {"type": "string"},
                        "lesson": {"type": "string"},
                        "task_key": {"type": "token"},
                    },
                }
            },
        },
    },
    "skills": {
        "skill_auto": _auto_embed("description", "status", "scope.task_key"),
        "skill_text": {
            "type": "search",
            "definition": {
                "mappings": {
                    "dynamic": False,
                    "fields": {
                        "description": {"type": "string"},
                        "status": {"type": "token"},
                        "scope": {
                            "type": "document",
                            "fields": {"task_key": {"type": "token"}},
                        },
                    },
                }
            },
        },
    },
    "episodes": {"episode_auto": _auto_embed("task", "site", "outcome")},
    "page_templates": {"template_auto": _auto_embed("summary", "platform", "kind")},
}


def _same_fields(wanted: dict[str, Any], live: dict[str, Any] | None) -> bool:
    """True when every field we define appears unchanged in the live definition.

    Atlas fills in defaults (dimensions, similarity, ...), so compare only the
    keys we set.
    """
    if not live:
        return False
    if "fields" in wanted:
        live_fields = live.get("fields", [])
        return len(live_fields) == len(wanted["fields"]) and all(
            any(all(lf.get(k) == v for k, v in wf.items()) for lf in live_fields)
            for wf in wanted["fields"]
        )
    return wanted.get("mappings") == live.get("mappings")


def ensure_search_indexes(db: Database) -> None:
    """Create missing search indexes and update changed ones. Does not wait."""
    for coll_name, indexes in SEARCH_INDEXES.items():
        coll = db[coll_name]
        try:
            live = {i["name"]: i for i in coll.list_search_indexes()}
        except OperationFailure as exc:
            print(f"[schema] search indexes unavailable on {coll_name}: {exc}", file=sys.stderr)
            continue
        for name, spec in indexes.items():
            current = live.get(name)
            if current is None:
                coll.create_search_index(
                    SearchIndexModel(name=name, type=spec["type"], definition=spec["definition"])
                )
                print(f"[schema] creating search index {coll_name}.{name}")
            elif not _same_fields(spec["definition"], current.get("latestDefinition")):
                coll.update_search_index(name, spec["definition"])
                print(f"[schema] updating search index {coll_name}.{name}")
            else:
                print(f"[schema] search index {coll_name}.{name}: {current.get('status')}")


def ensure_schema(db: Database) -> None:
    """Create or update every collection's validator and indexes."""
    existing = set(db.list_collection_names())
    for name, spec in COLLECTIONS.items():
        opts = {
            "validator": spec["validator"],
            "validationAction": VALIDATION_ACTION,
            "validationLevel": VALIDATION_LEVEL,
        }
        if name in existing:
            db.command("collMod", name, **opts)
            verb = "updated"
        else:
            db.create_collection(name, **opts)
            verb = "created"
        if spec["indexes"]:
            db[name].create_indexes(spec["indexes"])
        print(f"[schema] {verb} {name} ({len(spec['indexes'])} indexes)")

    for name, ts in TIMESERIES.items():
        if name in existing:
            print(f"[schema] exists {name} (time series)")
        else:
            db.create_collection(name, timeseries=ts)
            print(f"[schema] created {name} (time series)")

    ensure_search_indexes(db)


def main() -> None:
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    uri = os.environ.get("MONGODB_URI", DEFAULT_URI)
    db_name = os.environ.get("MONGODB_DB", DEFAULT_DB)
    print(f"[schema] {_redact_uri(uri)} db={db_name}", file=sys.stderr)
    client: MongoClient = MongoClient(uri)
    try:
        ensure_schema(client[db_name])
    finally:
        client.close()


if __name__ == "__main__":
    main()
