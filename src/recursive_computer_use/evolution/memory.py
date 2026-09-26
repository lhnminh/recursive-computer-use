"""MongoDB-backed episodic memory with Atlas Vector Search retrieval."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .models import Experience, EvolutionValidationError, redact_text, validate_task_key

# $rankFusion weights for lesson retrieval. Tune here only.
FUSION_WEIGHTS = {"semantic": 2, "keyword": 1, "useful": 1}


class ExperienceMemory:
    """Persist and retrieve redacted experiences from a MongoDB collection.

    ``collection`` uses the small subset of the PyMongo collection protocol
    needed here, which also keeps the class straightforward to unit-test.
    """

    def __init__(
        self,
        collection: Any,
        *,
        vector_index: str = "experience_embedding",
        vector_path: str = "embedding",
        text_index: str = "experience_auto",
        text_path: str = "lesson",
        text_model: str = "voyage-4",
        keyword_index: str = "experience_text",
        policies: Any = None,
    ) -> None:
        self.collection = collection
        self.vector_index = vector_index
        self.vector_path = vector_path
        # Atlas Automated Embedding: Atlas embeds ``text_path`` with Voyage on
        # write and embeds the query text on search. See schema.SEARCH_INDEXES.
        self.text_index = text_index
        self.text_path = text_path
        self.text_model = text_model
        self.keyword_index = keyword_index
        # Needed only for the "useful" fusion pipeline. Defaults to the
        # sibling ``policies`` collection when ``collection`` is a real one.
        if policies is None:
            database = getattr(collection, "database", None)
            policies = database["policies"] if database is not None else None
        self.policies = policies

    def store(self, experience: Experience) -> Any:
        """Insert one already-validated, privacy-filtered experience."""

        return self.collection.insert_one(experience.to_document())

    def find_similar(
        self,
        task_key: str,
        *,
        embedding: Sequence[float] | None = None,
        limit: int = 3,
        query_text: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find relevant memories, falling back to recent task memories.

        With ``query_text``, Atlas Vector Search embeds it with Voyage and
        searches the auto-embedded ``lesson`` field. Otherwise a precomputed
        query vector is used when available. Local MongoDB, test doubles, and
        Atlas deployments without the index use a recent-first query.
        """

        task_key = validate_task_key(task_key)
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise EvolutionValidationError("limit must be an integer from 1 to 20")

        if query_text:
            fused = self.ranked_lessons(task_key, query_text, limit=limit)
            if fused:
                return fused
            try:
                matches = list(
                    self.collection.aggregate(
                        self._search_pipeline(
                            index=self.text_index,
                            path=self.text_path,
                            query={
                                "query": {"text": redact_text(query_text, field_name="query_text")},
                                "model": self.text_model,
                            },
                            task_key=task_key,
                            limit=limit,
                        )
                    )
                )
                if matches:
                    return matches
            except Exception:
                # Same contract as the vector path: never disable the harness.
                pass

        if embedding is not None:
            vector = [float(value) for value in embedding]
            if not vector or len(vector) > 4096:
                raise EvolutionValidationError("embedding must contain 1-4096 numbers")
            try:
                pipeline = self._search_pipeline(
                    index=self.vector_index,
                    path=self.vector_path,
                    query={"queryVector": vector},
                    task_key=task_key,
                    limit=limit,
                )
                matches = list(self.collection.aggregate(pipeline))
                if matches:
                    return matches
            except Exception:
                # A missing Atlas index, unsupported local Mongo operation, or
                # temporary query failure must not disable the local harness.
                pass

        return self._recent_for_task(task_key, limit)

    def ranked_lessons(
        self, task_key: str, query_text: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Rank experiences with ``$rankFusion`` over meaning, words and usefulness.

        - ``semantic``: Voyage vector search on ``lesson``.
        - ``keyword``: Atlas Search on ``summary`` and ``lesson``.
        - ``useful``: experiences whose policy version produced an accepted
          child policy, newest first. A lesson that led to a verified
          improvement outranks one that is only similar.

        Returns ``[]`` when fusion is unavailable; the caller falls back.
        """

        task_key = validate_task_key(task_key)
        text = redact_text(query_text, field_name="query_text")
        n = max(20, limit * 10)
        semantic = self._search_pipeline(
            index=self.text_index,
            path=self.text_path,
            query={"query": {"text": text}, "model": self.text_model},
            task_key=task_key,
            limit=n,
        )[:1]  # $rankFusion input pipelines may not reshape documents
        keyword = [
            {
                "$search": {
                    "index": self.keyword_index,
                    "compound": {
                        "must": [{"text": {"query": text, "path": ["summary", "lesson"]}}],
                        "filter": [{"equals": {"path": "task_key", "value": task_key}}],
                    },
                }
            },
            {"$limit": n},
        ]
        pipelines: dict[str, list[dict[str, Any]]] = {
            "semantic": semantic,
            "keyword": keyword,
        }
        proven = self._versions_with_accepted_child(task_key)
        if proven:
            pipelines["useful"] = [
                {"$match": {"task_key": task_key, "policy_version": {"$in": proven}}},
                {"$sort": {"created_at": -1}},
                {"$limit": n},
            ]
        weights = {name: FUSION_WEIGHTS[name] for name in pipelines}
        try:
            return list(
                self.collection.aggregate(
                    [
                        {
                            "$rankFusion": {
                                "input": {"pipelines": pipelines},
                                "combination": {"weights": weights},
                            }
                        },
                        {"$limit": limit},
                        {
                            "$project": {
                                "_id": 0,
                                "embedding": 0,
                                "score": {"$meta": "score"},
                            }
                        },
                    ]
                )
            )
        except Exception:
            return []

    def _versions_with_accepted_child(self, task_key: str) -> list[int]:
        """Policy versions whose child candidate was accepted by the evaluator."""

        policies = self.policies
        if policies is None:
            return []
        try:
            return sorted(
                {
                    doc["parent_version"]
                    for doc in policies.find(
                        {
                            "task_key": task_key,
                            "status": "accepted",
                            "parent_version": {"$ne": None},
                        },
                        {"parent_version": 1},
                    )
                }
            )
        except Exception:
            return []

    @staticmethod
    def _search_pipeline(
        *,
        index: str,
        path: str,
        query: Mapping[str, Any],
        task_key: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "$vectorSearch": {
                    "index": index,
                    "path": path,
                    **query,
                    "numCandidates": max(20, limit * 10),
                    "limit": limit,
                    "filter": {"task_key": task_key},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "embedding": 0,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]

    def _recent_for_task(self, task_key: str, limit: int) -> list[dict[str, Any]]:
        projection = {"_id": 0, "embedding": 0}
        try:
            cursor = self.collection.find({"task_key": task_key}, projection)
            if hasattr(cursor, "sort"):
                cursor = cursor.sort("created_at", -1)
            if hasattr(cursor, "limit"):
                cursor = cursor.limit(limit)
            return [dict(document) for document in cursor][:limit]
        except Exception:
            return []

    def latest_for_policy(
        self, task_key: str, policy_version: int
    ) -> dict[str, Any] | None:
        """Return the newest verified experience for one policy version."""

        try:
            return self.collection.find_one(
                {"task_key": validate_task_key(task_key), "policy_version": policy_version},
                {"_id": 0, "embedding": 0},
                sort=[("created_at", -1)],
            )
        except Exception:
            matches = self._recent_for_task(task_key, 20)
            return next(
                (
                    document
                    for document in matches
                    if document.get("policy_version") == policy_version
                ),
                None,
            )


def lessons_from(memories: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return unique non-empty lessons suitable for bounded prompt context."""

    lessons: list[str] = []
    for memory in memories:
        lesson = memory.get("lesson")
        if isinstance(lesson, str) and lesson and lesson not in lessons:
            lessons.append(lesson)
    return lessons
