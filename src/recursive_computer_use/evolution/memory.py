"""MongoDB-backed episodic memory with Atlas Vector Search retrieval."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .models import Experience, EvolutionValidationError, validate_task_key


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
    ) -> None:
        self.collection = collection
        self.vector_index = vector_index
        self.vector_path = vector_path

    def store(self, experience: Experience) -> Any:
        """Insert one already-validated, privacy-filtered experience."""

        return self.collection.insert_one(experience.to_document())

    def find_similar(
        self,
        task_key: str,
        *,
        embedding: Sequence[float] | None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Find relevant memories, falling back to recent task memories.

        Atlas Vector Search is attempted when a query vector is available.
        Local MongoDB, test doubles, and Atlas deployments without a configured
        vector index automatically use a deterministic recent-first query.
        """

        task_key = validate_task_key(task_key)
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise EvolutionValidationError("limit must be an integer from 1 to 20")

        if embedding is not None:
            vector = [float(value) for value in embedding]
            if not vector or len(vector) > 4096:
                raise EvolutionValidationError("embedding must contain 1-4096 numbers")
            try:
                pipeline = [
                    {
                        "$vectorSearch": {
                            "index": self.vector_index,
                            "path": self.vector_path,
                            "queryVector": vector,
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
                matches = list(self.collection.aggregate(pipeline))
                if matches:
                    return matches
            except Exception:
                # A missing Atlas index, unsupported local Mongo operation, or
                # temporary query failure must not disable the local harness.
                pass

        return self._recent_for_task(task_key, limit)

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


def lessons_from(memories: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return unique non-empty lessons suitable for bounded prompt context."""

    lessons: list[str] = []
    for memory in memories:
        lesson = memory.get("lesson")
        if isinstance(lesson, str) and lesson and lesson not in lessons:
            lessons.append(lesson)
    return lessons
