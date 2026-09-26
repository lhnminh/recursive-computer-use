"""
store.py — Persist computer-use runs and actions to MongoDB.

Two collections:
  - ``runs``    : one document per harness invocation.
  - ``actions`` : one document per captured desktop action (e.g. a click).

Design principles
-----------------
* **Non-fatal.** Logging must never break a desktop session. If MongoDB is
  unreachable or a write fails, we log a warning and continue. Callers get a
  no-op-ish store instead of an exception.
* **Env-configured.** ``MONGODB_URI`` (default ``mongodb://localhost:27017``)
  and ``MONGODB_DB`` (default ``recursive_computer_use``), loaded via the same
  ``.env`` flow the rest of the app uses.
"""

from __future__ import annotations

import os
import sys
import uuid
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "recursive_computer_use"

# Short server-selection timeout so an unreachable Mongo fails fast instead of
# hanging the whole harness.
_CONNECT_TIMEOUT_MS = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_uri(uri: str) -> str:
    """
    Return a Mongo URI safe to log: the ``user:password@`` credentials portion
    is replaced with ``***@`` so secrets never reach logs.
    """
    import re

    return re.sub(r"://[^/@]*@", "://***@", uri)


def _safe_summary(value: str | None, limit: int = 500) -> str | None:
    """Redact common credentials and bound text before remote persistence."""

    if value is None:
        return None
    import re

    text = str(value).strip()[:limit]
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]", text, flags=re.I)
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b", "[KEY]", text)
    text = re.sub(
        r"(?i)\b(password|passwd|api[_ -]?key|token|secret)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text


@dataclass
class Action:
    """A single captured desktop action."""

    run_id: str
    turn: int
    seq: int
    kind: str  # "click", "doubleClick", "write", "scroll", ...
    x: int | None = None
    y: int | None = None
    args: dict[str, Any] = field(default_factory=dict)
    screenshot_ref: str | None = None
    ts: datetime = field(default_factory=_utcnow)

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)


class ActionStore:
    """
    MongoDB-backed store for runs and actions.

    Use :meth:`connect` to build one; it never raises on connection failure —
    it returns a store whose ``enabled`` flag reflects whether persistence is
    actually available. All write methods are safe to call regardless.
    """

    def __init__(
        self,
        client: MongoClient | None,
        db_name: str,
        *,
        enabled: bool,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._seq = 0  # monotonic action counter within the process/run
        self._action_count = 0
        self._policy_violations = 0

        if client is not None:
            db = client[db_name]
            self.database = db
            self.runs = db["runs"]
            self.actions = db["actions"]
        else:
            self.database = None
            self.runs = None
            self.actions = None

    # -- construction ------------------------------------------------------

    @classmethod
    def disabled(cls, db_name: str | None = None) -> "ActionStore":
        """Return an explicitly disabled store without attempting a connection."""

        return cls(None, db_name or DEFAULT_DB, enabled=False)

    @classmethod
    def connect(
        cls,
        uri: str | None = None,
        db_name: str | None = None,
        *,
        verbose: bool = False,
    ) -> "ActionStore":
        """
        Connect to MongoDB, verifying reachability with a ping.

        Never raises: on failure returns a disabled store (``enabled == False``)
        so the harness keeps running without logging.
        """
        uri = uri or os.environ.get("MONGODB_URI", DEFAULT_URI)
        db_name = db_name or os.environ.get("MONGODB_DB", DEFAULT_DB)

        try:
            client: MongoClient = MongoClient(
                uri, serverSelectionTimeoutMS=_CONNECT_TIMEOUT_MS
            )
            client.admin.command("ping")  # forces a real connection attempt
            store = cls(client, db_name, enabled=True)
            store._ensure_indexes()
            if verbose:
                print(f"[store] connected to MongoDB at {_redact_uri(uri)} (db={db_name})",
                      file=sys.stderr)
            return store
        except PyMongoError as exc:
            print(
                f"[store] MongoDB unavailable ({exc.__class__.__name__}): "
                f"actions will NOT be logged. URI={_redact_uri(uri)}",
                file=sys.stderr,
            )
            return cls(None, db_name, enabled=False)

    def _ensure_indexes(self) -> None:
        try:
            self.actions.create_index([("run_id", ASCENDING), ("seq", ASCENDING)])
            self.actions.create_index([("ts", ASCENDING)])
            self.runs.create_index([("run_id", ASCENDING)], unique=True)
        except PyMongoError as exc:
            print(f"[store] index creation failed: {exc}", file=sys.stderr)

    # -- properties --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- run lifecycle -----------------------------------------------------

    def start_run(self, prompt: str, model: str) -> str:
        """Create a run document and return its ``run_id`` (always returned)."""
        run_id = uuid.uuid4().hex
        self._seq = 0
        self._action_count = 0
        self._policy_violations = 0
        if not self._enabled:
            return run_id
        try:
            self.runs.insert_one(
                {
                    "run_id": run_id,
                    "prompt_summary": _safe_summary(prompt),
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "model": model,
                    "status": "running",
                    "started_at": _utcnow(),
                    "finished_at": None,
                    "final_summary": None,
                }
            )
        except PyMongoError as exc:
            print(f"[store] start_run failed: {exc}", file=sys.stderr)
        return run_id

    def record_action(self, action: Action) -> None:
        """Persist one action. Assigns a monotonic ``seq`` if not already set."""
        self._action_count += 1
        if action.kind == "policy_violation":
            self._policy_violations += 1
        if not self._enabled:
            return
        try:
            self.actions.insert_one(action.to_doc())
        except PyMongoError as exc:
            print(f"[store] record_action failed: {exc}", file=sys.stderr)

    def next_seq(self) -> int:
        """Return and advance the per-run action sequence counter."""
        self._seq += 1
        return self._seq

    @property
    def action_count(self) -> int:
        return self._action_count

    @property
    def policy_violation_count(self) -> int:
        return self._policy_violations

    def finish_run(
        self, run_id: str, status: str, final_text: str | None = None
    ) -> None:
        """Mark a run finished with a terminal ``status``."""
        if not self._enabled:
            return
        try:
            self.runs.update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "status": status,
                        "final_summary": _safe_summary(final_text),
                        "finished_at": _utcnow(),
                    }
                },
            )
        except PyMongoError as exc:
            print(f"[store] finish_run failed: {exc}", file=sys.stderr)

    def attach_verification(
        self,
        run_id: str,
        *,
        task_key: str,
        policy_version: int,
        metrics: dict[str, Any],
        evolution_result: dict[str, Any] | None = None,
    ) -> None:
        if not self._enabled:
            return
        try:
            self.runs.update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "task_key": task_key,
                        "policy_version": policy_version,
                        "verified_metrics": metrics,
                        "evolution_result": evolution_result,
                    }
                },
            )
        except PyMongoError as exc:
            print(f"[store] attach_verification failed: {exc}", file=sys.stderr)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
