"""Deliver newly accepted policies to a running agent."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger(__name__)


class PolicyFeed:
    """Background change-stream listener with persisted resume state."""

    def __init__(self, db: Any, agent_id: str) -> None:
        self.db = db
        self.agent_id = agent_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warning_logged = False

    def watch(self, task_key: str, on_policy: Callable[[Mapping[str, Any]], None]) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("policy feed is already watching")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(task_key, on_policy),
            name=f"policy-feed-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                LOGGER.warning("Policy feed thread for %s did not stop in time", self.agent_id)

    def _run(self, task_key: str, on_policy: Callable[[Mapping[str, Any]], None]) -> None:
        policies = self.db["policies"]
        states = self.db["agent_state"]
        state = states.find_one({"_id": self.agent_id}) or {}
        token = state.get("resume_token")
        pipeline = [
            {
                "$match": {
                    "operationType": {"$in": ["insert", "replace", "update"]},
                    "fullDocument.task_key": task_key,
                    "fullDocument.status": "accepted",
                }
            }
        ]
        try:
            watch_args: dict[str, Any] = {
                "pipeline": pipeline,
                "full_document": "updateLookup",
                "max_await_time_ms": 1000,
            }
            if token is not None:
                watch_args["resume_after"] = token
            with policies.watch(**watch_args) as stream:
                while not self._stop.is_set():
                    event = stream.try_next()
                    if event is None:
                        continue
                    document = event.get("fullDocument")
                    if document is not None:
                        on_policy(document)
                    states.update_one(
                        {"_id": self.agent_id},
                        {
                            "$set": {
                                "resume_token": stream.resume_token,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        },
                        upsert=True,
                    )
        except Exception as exc:
            if _change_stream_unavailable(exc):
                if not self._warning_logged:
                    LOGGER.warning("MongoDB change streams unavailable; policy feed disabled: %s", exc)
                    self._warning_logged = True
                return
            if not self._stop.is_set():
                LOGGER.exception("Policy feed stopped after an error")


def _change_stream_unavailable(error: Exception) -> bool:
    name = type(error).__name__
    message = str(error).lower()
    return name in {"InvalidOperation", "ConfigurationError", "OperationFailure"} and (
        "replica set" in message
        or "change stream" in message
        or "not supported" in message
        or "standalone" in message
    )
