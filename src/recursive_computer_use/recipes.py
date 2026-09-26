"""
recipes.py — Store, rank and evolve API recipes in the ``skills`` collection.

Lifecycle of a recipe (``kind: "api_recipe"``):

    save_candidate ──► candidate ──first verified success──► active
                           │                                   │
                      first failure                 2 failures in a row
                           ▼                                   ▼
                        retired ◄──────── supersede(old, new v2) ──┘

Every replay is recorded as an episode (``mode: "api_recipe"``) through
``learning.LearningStore``, so ``uses``, ``wins`` and ``lift`` use the same
aggregation as every other skill.

Retrieval (:meth:`RecipeStore.find_for_task`) is one ``$rankFusion`` over
Voyage meaning, keywords, positive lift and "already proven" (active).

Saving a recipe also writes its call chain to ``site_map`` as ``api_call``
edges (request → request, via the extracted var), so ``$graphLookup`` can
show which call feeds which.

:meth:`RecipeStore.watch` streams newly active recipes to other agents with a
resumable change stream (resume token in ``agent_state``).
"""

from __future__ import annotations

import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from .learning import EMBEDDING_MODEL, LearningStore
from .network.recipe import KIND, Recipe, RecipeError, template_vars

LIVE = ["candidate", "active"]
RETIRE_AFTER_FAILURES = 2
# $rankFusion weights for recipe retrieval. Tune here only.
RECIPE_FUSION_WEIGHTS = {"semantic": 2, "keyword": 1, "lift": 1, "proven": 1}
# Relevance gate: only recipes whose Voyage score is within this margin of the
# best match may be returned. Lift and "proven" re-order relevant recipes;
# they must never pull in a recipe for a different task.
RELEVANCE_MARGIN = 0.05
# Atlas embeds a new recipe a few seconds after the write (measured 6-11 s).
# When vector search sees nothing for the site, recent recipes count as relevant.
EMBED_LAG = timedelta(minutes=2)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _endpoint(method: str, url: str) -> str:
    return f"{method} {urlsplit(url).path or '/'}"


class RecipeStore:
    """API recipes on top of the ``skills`` collection."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.skills = db["skills"]
        self.site_map = db["site_map"]
        self.learning = LearningStore(db)

    # -- write -------------------------------------------------------------

    def save_candidate(self, recipe: Recipe, *, recording_id: ObjectId | None = None) -> ObjectId:
        """Insert *recipe* as a new ``candidate``. Bumps the version if the name exists."""
        recipe.validate()
        latest = self.skills.find_one(
            {"name": recipe.name}, {"version": 1}, sort=[("version", DESCENDING)]
        )
        if latest is not None:
            recipe.version = int(latest["version"]) + 1
            recipe.parent_id = recipe.parent_id or latest["_id"]
        recipe.status = "candidate"
        now = _utcnow()
        recipe_document = recipe.to_dict()
        for parameter in recipe_document.get("params", []):
            parameter["example"] = None
        doc = {
            **recipe_document,
            "uses": 0,
            "wins": 0,
            "lift": None,
            "fail_streak": 0,
            "recording_id": recording_id,
            "created_at": now,
            "updated_at": now,
        }
        recipe_id = self.skills.insert_one(doc).inserted_id
        recipe.id = recipe_id
        self._write_edges(recipe, recipe_id)
        return recipe_id

    def supersede(self, old_id: ObjectId, new_recipe: Recipe) -> ObjectId:
        """Save *new_recipe* as the next version of *old_id* and retire the old one."""
        old = self.skills.find_one({"_id": old_id, "kind": KIND})
        if old is None:
            raise RecipeError(f"no recipe {old_id}")
        new_recipe.name = old["name"]
        new_recipe.parent_id = old_id
        new_id = self.save_candidate(new_recipe)
        self._retire(old_id, reason=f"superseded by version {new_recipe.version}")
        return new_id

    def record_result(
        self,
        recipe_id: ObjectId,
        *,
        ok: bool,
        run_ms: int,
        steps: Sequence[Mapping[str, Any]] = (),
        task: str | None = None,
        llm_calls: int = 0,
    ) -> str:
        """Record one verified replay; promote or retire. Returns the new status."""
        doc = self.skills.find_one({"_id": recipe_id, "kind": KIND})
        if doc is None:
            raise RecipeError(f"no recipe {recipe_id}")
        scope = doc.get("scope") or {}
        now = _utcnow()
        self.learning.record_episode(
            run_id=f"recipe-{uuid.uuid4().hex}",
            prompt=f"API recipe workflow: {doc['name']}",
            task_key=scope.get("task_key") or doc["name"],
            site=scope.get("site"),
            model="api_recipe",
            mode="api_recipe",
            outcome="success" if ok else "failure",
            started_at=now - timedelta(milliseconds=max(0, int(run_ms))),
            skills_used=[{"skill_id": recipe_id, "version": int(doc["version"])}],
            metrics={
                "success_rate": 1.0 if ok else 0.0,
                "action_count": len(steps),
                "duration_ms": int(run_ms),
            },
            llm_calls=llm_calls,
        )
        if ok:
            self.skills.update_one(
                {"_id": recipe_id},
                {"$set": {"fail_streak": 0, "updated_at": now}},
            )
            promoted = self.skills.update_one(
                {"_id": recipe_id, "status": "candidate"},
                {"$set": {"status": "active", "updated_at": now}},
            )
            return "active" if promoted.modified_count or doc["status"] == "active" else doc["status"]

        after = self.skills.find_one_and_update(
            {"_id": recipe_id},
            {"$inc": {"fail_streak": 1}, "$set": {"updated_at": now}},
            return_document=True,
        )
        # A candidate that fails its first replay was learned wrong. An active
        # recipe gets one more chance (the site may have hiccuped).
        if after["status"] == "candidate" or (
            after["status"] == "active" and after["fail_streak"] >= RETIRE_AFTER_FAILURES
        ):
            self._retire(recipe_id, reason=f"{after['fail_streak']} failed replay(s)")
            return "retired"
        return after["status"]

    def _retire(self, recipe_id: ObjectId, *, reason: str) -> None:
        now = _utcnow()
        self.skills.update_one(
            {"_id": recipe_id, "status": {"$ne": "retired"}},
            {"$set": {"status": "retired", "retired_at": now, "updated_at": now, "retirement_reason": reason}},
        )

    def _write_edges(self, recipe: Recipe, recipe_id: ObjectId) -> None:
        """Upsert ``site_map`` edges: each step links to the next, via the vars it hands over."""
        produced: dict[str, str] = {}  # var -> endpoint that extracts it
        now = _utcnow()
        prev = None
        try:
            for step in recipe.steps:
                here = _endpoint(step.method, step.url)
                used = template_vars(step.url) | template_vars(step.headers) | template_vars(step.body)
                sources: dict[str, list[str]] = {}
                for var in sorted(used):
                    if var in produced:
                        sources.setdefault(produced[var], []).append(var)
                if prev is not None:
                    sources.setdefault(prev, [])
                for src, vars_ in sources.items():
                    self.site_map.update_one(
                        {"site": recipe.site, "from": src, "to": here},
                        {
                            "$set": {
                                "kind": "api_call",
                                "via": {"vars": vars_} if vars_ else None,
                                "recipe_id": recipe_id,
                                "last_seen": now,
                            }
                        },
                        upsert=True,
                    )
                for ex in step.extract:
                    produced[ex.var] = here
                prev = here
        except PyMongoError as exc:
            print(f"[recipes] site_map edges failed: {exc}", file=sys.stderr)

    # -- read --------------------------------------------------------------

    def get(self, recipe_id: ObjectId) -> Recipe | None:
        doc = self.skills.find_one({"_id": recipe_id, "kind": KIND})
        return Recipe.from_dict(doc) if doc else None

    def find_for_task(self, task: str, *, site: str | None = None, limit: int = 3) -> list[Recipe]:
        """Best live recipes for *task*: ``$rankFusion`` of meaning, words, lift, proven.

        Results are gated by relevance first (see ``RELEVANCE_MARGIN``).
        """
        n = max(20, limit * 10)
        vflt: dict[str, Any] = {"kind": KIND, "status": {"$in": LIVE}}
        sflt: list[dict[str, Any]] = [
            {"equals": {"path": "kind", "value": KIND}},
            {"in": {"path": "status", "value": LIVE}},
        ]
        mflt: dict[str, Any] = {"kind": KIND}
        if site:
            vflt["scope.site"] = site
            sflt.append({"equals": {"path": "scope.site", "value": site}})
            mflt["scope.site"] = site
        semantic_stage = {
            "$vectorSearch": {
                "index": "skill_auto",
                "path": "description",
                "query": {"text": task},
                "model": EMBEDDING_MODEL,
                "numCandidates": n * 5,
                "limit": n,
                "filter": vflt,
            }
        }
        relevant = self._relevant_ids(semantic_stage)
        pipelines = {
            "semantic": [semantic_stage],
            "keyword": [
                {
                    "$search": {
                        "index": "skill_text",
                        "compound": {
                            "must": [{"text": {"query": task, "path": "description"}}],
                            "filter": sflt,
                        },
                    }
                },
                {"$limit": n},
            ],
            "lift": [
                {"$match": {**mflt, "status": "active", "lift": {"$gt": 0}}},
                {"$sort": {"lift": -1}},
                {"$limit": n},
            ],
            "proven": [
                {"$match": {**mflt, "status": "active"}},
                {"$sort": {"wins": -1, "version": -1}},
                {"$limit": n},
            ],
        }
        try:
            docs = list(
                self.skills.aggregate(
                    [
                        {
                            "$rankFusion": {
                                "input": {"pipelines": pipelines},
                                "combination": {"weights": RECIPE_FUSION_WEIGHTS},
                            }
                        },
                        {"$limit": n},
                    ]
                )
            )
        except PyMongoError as exc:
            print(f"[recipes] rank fusion failed, using fallback: {exc}", file=sys.stderr)
            docs = []
        if relevant is not None:
            if not relevant:
                # Nothing on this site is embedded yet; a recipe saved seconds
                # ago may be the right one.
                relevant = self._unembedded_ids(mflt)
            if not relevant:
                return []
            docs = [d for d in docs if d["_id"] in relevant]
        docs = docs[:limit]
        if not docs:
            # Index still building, or the relevant recipe is not embedded yet:
            # newest live recipes for the site, active first.
            query: dict[str, Any] = {**mflt, "status": {"$in": LIVE}}
            if relevant is not None:
                query["_id"] = {"$in": list(relevant)}
            docs = list(
                self.skills.find(query)
                .sort([("status", 1), ("version", -1)])  # "active" < "candidate"
                .limit(limit)
            )
        return [Recipe.from_dict(d) for d in docs]

    def _unembedded_ids(self, match: dict[str, Any]) -> set[ObjectId]:
        """Live recipes written within ``EMBED_LAG``: vector search may not see them yet."""
        since = _utcnow() - EMBED_LAG
        query = {**match, "status": {"$in": LIVE}, "created_at": {"$gte": since}}
        try:
            return {d["_id"] for d in self.skills.find(query, {"_id": 1})}
        except PyMongoError:
            return set()

    def _relevant_ids(self, semantic_stage: dict[str, Any]) -> set[ObjectId] | None:
        """IDs within ``RELEVANCE_MARGIN`` of the best Voyage score; None if unavailable."""
        try:
            scored = list(
                self.skills.aggregate(
                    [semantic_stage, {"$project": {"score": {"$meta": "vectorSearchScore"}}}]
                )
            )
        except PyMongoError:
            return None
        if not scored:
            return set()
        best = max(d["score"] for d in scored)
        return {d["_id"] for d in scored if d["score"] >= best - RELEVANCE_MARGIN}

    def call_chain(self, site: str, start: str) -> list[dict[str, Any]]:
        """Every endpoint reachable from *start* ("METHOD /path") via ``$graphLookup``."""
        pipeline = [
            {"$match": {"site": site, "from": start, "kind": "api_call"}},
            {"$limit": 1},
            {
                "$graphLookup": {
                    "from": "site_map",
                    "startWith": "$to",
                    "connectFromField": "to",
                    "connectToField": "from",
                    "as": "chain",
                    "depthField": "depth",
                    "restrictSearchWithMatch": {"site": site, "kind": "api_call"},
                }
            },
        ]
        first = next(self.site_map.aggregate(pipeline), None)
        if first is None:
            return []
        edges = [first] + sorted(first["chain"], key=lambda e: e["depth"])
        return [{"from": e["from"], "to": e["to"], "via": e.get("via")} for e in edges]

    # -- live updates ------------------------------------------------------

    def watch(
        self,
        on_recipe: Callable[[Recipe], None],
        *,
        agent_id: str = "recipe-watcher",
        site: str | None = None,
    ) -> Callable[[], None]:
        """Call *on_recipe* for every recipe that becomes ``active``. Returns ``stop()``.

        The resume token is saved in ``agent_state`` after each event, so a
        restarted watcher first receives the recipes it missed.
        """
        match: dict[str, Any] = {
            "operationType": {"$in": ["insert", "update", "replace"]},
            "fullDocument.kind": KIND,
            "fullDocument.status": "active",
        }
        if site:
            match["fullDocument.scope.site"] = site
        state = self.db["agent_state"]
        stop_event = threading.Event()
        saved = state.find_one({"_id": agent_id}) or {}
        opened = threading.Event()

        def run() -> None:
            try:
                with self.skills.watch(
                    [{"$match": match}],
                    full_document="updateLookup",
                    resume_after=saved.get("resume_token"),
                ) as stream:
                    opened.set()
                    while not stop_event.is_set() and stream.alive:
                        event = stream.try_next()
                        if event is None:
                            stop_event.wait(0.2)
                            continue
                        try:
                            on_recipe(Recipe.from_dict(event["fullDocument"]))
                        finally:
                            state.update_one(
                                {"_id": agent_id},
                                {"$set": {"resume_token": stream.resume_token, "updated_at": _utcnow()}},
                                upsert=True,
                            )
            except PyMongoError as exc:
                print(f"[recipes] watch stopped: {exc}", file=sys.stderr)
            finally:
                opened.set()

        thread = threading.Thread(target=run, name=f"recipe-watch-{agent_id}", daemon=True)
        thread.start()
        opened.wait(10)

        def stop() -> None:
            stop_event.set()
            thread.join(5)

        return stop
