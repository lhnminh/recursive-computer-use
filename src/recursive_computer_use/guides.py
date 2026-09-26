"""
guides.py — Fast-path coordinate guides for the computer-use agent.

A *guide* is a reusable, ordered sequence of desktop actions (with captured
coordinates) for a specific ``(site, task)`` on a specific machine environment.
Once the agent has completed a task once via slow free-navigation, we store the
coordinates it used so the next run can replay them directly — no screenshot, no
model reasoning — which is dramatically faster.

Storage is two-tier (see fast-web-browsing-design.md):

  * **MongoDB primary** — shared source of truth when reachable.
  * **Local JSON fallback** — ``~/.recursive_computer_use/guides.json``. Keeps a
    local copy of captured guides when MongoDB is unavailable.

Everything here is **non-fatal**: a persistence failure must never break a
desktop session. If both tiers fail, the store degrades to in-memory / no-op and
the agent simply free-navigates.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOCAL_PATH = Path.home() / ".recursive_computer_use" / "guides.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Environment fingerprint
# --------------------------------------------------------------------------- #


@dataclass
class Env:
    """The machine environment a guide's coordinates were captured in."""

    fingerprint: str
    os: str
    screen_width: int
    screen_height: int
    scaling: float
    browser: str = "unknown"

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)


def detect_env(browser: str = "unknown") -> Env:
    """
    Build an :class:`Env` for the current machine.

    Screen size comes from pyautogui; scaling is derived from the ratio of the
    true pixel resolution to the logical size when available (Retina = 2.0).
    Falls back to 1.0 if it can't be determined.
    """
    os_name = platform.system().lower()  # "darwin", "windows", "linux"
    width, height = _screen_size()
    scaling = _scaling_factor(width, height)
    fingerprint = f"{os_name}-{width}x{height}@{scaling:g}x"
    return Env(
        fingerprint=fingerprint,
        os=os_name,
        screen_width=width,
        screen_height=height,
        scaling=scaling,
        browser=browser,
    )


def _screen_size() -> tuple[int, int]:
    try:
        import pyautogui

        size = pyautogui.size()
        return int(size.width), int(size.height)
    except Exception:
        return (0, 0)


def _scaling_factor(logical_w: int, logical_h: int) -> float:
    """
    Best-effort display scaling factor. On macOS Retina displays the logical
    size reported by pyautogui differs from the physical pixel size; we compare
    against the physical size when we can read it.
    """
    if logical_w <= 0:
        return 1.0
    try:
        if platform.system() == "Darwin":
            from AppKit import NSScreen  # type: ignore

            screen = NSScreen.mainScreen()
            factor = float(screen.backingScaleFactor())
            return factor if factor > 0 else 1.0
    except Exception:
        pass
    return 1.0


# --------------------------------------------------------------------------- #
# Guide schema
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One replayable action in a guide."""

    seq: int
    kind: str  # launch_app | click | doubleClick | rightClick | write | press | hotkey | scroll | moveTo
    x: int | None = None
    y: int | None = None
    nx: float | None = None  # normalized x (x / screen_width) — future-proofing
    ny: float | None = None  # normalized y (y / screen_height)
    text: str | None = None  # for write/press/hotkey
    args: dict[str, Any] = field(default_factory=dict)
    target_text: str | None = None  # cheap verification anchor
    description: str | None = None
    checkpoint: bool = False  # verify before firing this step
    screenshot_ref: str | None = None
    parameter: str | None = None  # prompt-supplied value, e.g. navigation_url or prompt_text

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Step":
        known = {f: doc.get(f) for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        known["args"] = doc.get("args") or {}
        return cls(**known)


@dataclass
class Guide:
    """A reusable coordinate playbook for a ``(site, task, env)``."""

    site: str
    task: str
    env: Env
    steps: list[Step] = field(default_factory=list)
    guide_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str | None = None
    source_run_id: str | None = None
    success_count: int = 0
    fail_count: int = 0
    model: str | None = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def key(self) -> tuple[str, str, str]:
        return (self.site, self.task, self.env.fingerprint)

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        return doc

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Guide":
        env_doc = doc.get("env") or {}
        env = Env(**{f: env_doc.get(f) for f in Env.__dataclass_fields__})  # type: ignore[attr-defined]
        steps = [Step.from_doc(s) for s in doc.get("steps", [])]
        return cls(
            site=doc["site"],
            task=doc["task"],
            env=env,
            steps=steps,
            guide_id=doc.get("guide_id", uuid.uuid4().hex),
            title=doc.get("title"),
            source_run_id=doc.get("source_run_id"),
            success_count=doc.get("success_count", 0),
            fail_count=doc.get("fail_count", 0),
            model=doc.get("model"),
            created_at=doc.get("created_at", _utcnow_iso()),
            updated_at=doc.get("updated_at", _utcnow_iso()),
        )


def _guide_key_str(site: str, task: str, fingerprint: str) -> str:
    """Stable string key for the local JSON dict."""
    return f"{site}\u241f{task}\u241f{fingerprint}"


# --------------------------------------------------------------------------- #
# Local JSON backend
# --------------------------------------------------------------------------- #


class LocalGuideBackend:
    """
    JSON-file guide store. Keyed by ``(site, task, fingerprint)``.

    Non-fatal: on any IO/parse error it warns and behaves as empty. Writes are
    best-effort; a failed write never raises.
    """

    def __init__(self, path: Path | None = None) -> None:
        env_path = os.environ.get("GUIDES_LOCAL_PATH")
        self.path = Path(env_path) if env_path else (path or DEFAULT_LOCAL_PATH)

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return {}
            return json.loads(self.path.read_text() or "{}")
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] local load failed ({exc}); treating as empty",
                  file=sys.stderr)
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)  # atomic on POSIX
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] local save failed ({exc}); guide not persisted "
                  "locally", file=sys.stderr)

    def find(self, site: str, task: str, fingerprint: str) -> Guide | None:
        data = self._load()
        doc = data.get(_guide_key_str(site, task, fingerprint))
        return Guide.from_doc(doc) if doc else None

    def upsert(self, guide: Guide) -> None:
        data = self._load()
        guide.updated_at = _utcnow_iso()
        data[_guide_key_str(*guide.key())] = guide.to_doc()
        self._save(data)

    def bump(self, site: str, task: str, fingerprint: str, ok: bool) -> None:
        data = self._load()
        key = _guide_key_str(site, task, fingerprint)
        doc = data.get(key)
        if not doc:
            return
        field_name = "success_count" if ok else "fail_count"
        doc[field_name] = doc.get(field_name, 0) + 1
        doc["updated_at"] = _utcnow_iso()
        self._save(data)


# --------------------------------------------------------------------------- #
# Mongo backend (thin; reuses a provided collection)
# --------------------------------------------------------------------------- #


class MongoGuideBackend:
    """Guide store backed by a MongoDB collection. Non-fatal on write errors."""

    def __init__(self, collection: Any) -> None:
        self.col = collection
        try:
            from pymongo import ASCENDING

            self.col.create_index(
                [("site", ASCENDING), ("task", ASCENDING),
                 ("env.fingerprint", ASCENDING)],
                unique=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] mongo index creation failed: {exc}", file=sys.stderr)

    def find(self, site: str, task: str, fingerprint: str) -> Guide | None:
        try:
            doc = self.col.find_one(
                {"site": site, "task": task, "env.fingerprint": fingerprint}
            )
            return Guide.from_doc(doc) if doc else None
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] mongo find failed: {exc}", file=sys.stderr)
            return None

    def upsert(self, guide: Guide) -> None:
        try:
            guide.updated_at = _utcnow_iso()
            self.col.replace_one(
                {"site": guide.site, "task": guide.task,
                 "env.fingerprint": guide.env.fingerprint},
                guide.to_doc(),
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] mongo upsert failed: {exc}", file=sys.stderr)

    def bump(self, site: str, task: str, fingerprint: str, ok: bool) -> None:
        try:
            field_name = "success_count" if ok else "fail_count"
            self.col.update_one(
                {"site": site, "task": task, "env.fingerprint": fingerprint},
                {"$inc": {field_name: 1}, "$set": {"updated_at": _utcnow_iso()}},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[guides] mongo bump failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Two-tier GuideStore
# --------------------------------------------------------------------------- #


class GuideStore:
    """
    Two-tier guide store: MongoDB primary + local JSON fallback.

    * Reads prefer Mongo (when present), falling back to local on a miss.
    * Writes go **write-through** to both, so the local file is always a warm
      copy usable when Mongo is down.
    * When Mongo is absent, everything transparently uses the local file only.

    Build via :meth:`open`.
    """

    def __init__(
        self,
        mongo: MongoGuideBackend | None,
        local: LocalGuideBackend,
        *,
        verbose: bool = False,
    ) -> None:
        self._mongo = mongo
        self._local = local
        self._verbose = verbose

    @classmethod
    def open(
        cls,
        mongo_collection: Any | None = None,
        *,
        local_path: Path | None = None,
        verbose: bool = False,
    ) -> "GuideStore":
        """
        Build a GuideStore. ``mongo_collection`` may be ``None`` (Mongo down or
        disabled) — the store then runs local-only.
        """
        mongo = MongoGuideBackend(mongo_collection) if mongo_collection is not None else None
        local = LocalGuideBackend(local_path)
        if verbose:
            tier = "mongo+local" if mongo else "local-only"
            print(f"[guides] store ready ({tier}); local={local.path}",
                  file=sys.stderr)
        return cls(mongo, local, verbose=verbose)

    # -- read --------------------------------------------------------------

    def find_guide(self, site: str, task: str, fingerprint: str) -> Guide | None:
        if self._mongo is not None:
            guide = self._mongo.find(site, task, fingerprint)
            if guide is not None:
                if self._verbose:
                    print("[guides] loaded guide from MongoDB", file=sys.stderr)
                return guide
        guide = self._local.find(site, task, fingerprint)
        if guide is not None and self._verbose:
            print(f"[guides] loaded guide from local cache ({self._local.path})",
                  file=sys.stderr)
        return guide

    # -- write (write-through) --------------------------------------------

    def upsert_guide(self, guide: Guide) -> None:
        if self._mongo is not None:
            self._mongo.upsert(guide)
        self._local.upsert(guide)  # always keep a warm local copy

    def bump_guide_stats(
        self, site: str, task: str, fingerprint: str, *, ok: bool
    ) -> None:
        if self._mongo is not None:
            self._mongo.bump(site, task, fingerprint, ok)
        self._local.bump(site, task, fingerprint, ok)
