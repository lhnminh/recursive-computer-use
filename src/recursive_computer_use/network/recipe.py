"""
recipe.py — The API recipe: a validated chain of HTTP requests for one task.

A recipe is stored in the ``skills`` collection with ``kind: "api_recipe"``.
The shape is the contract in ``goals/GOALS.md``. The learner writes recipes,
``recipes.RecipeStore`` stores and ranks them, and ``network.runner`` replays
them.

Templates: ``{{name}}`` in a step's URL, header values or body strings is
replaced by a param or by a var extracted from an earlier step's response.
:func:`render` does the substitution for the runner.

:meth:`Recipe.validate` is the safety gate. A recipe that fails it is never
stored or replayed:

- every step and the verify URL stay on ``scope.site``;
- no cookie or authorization headers (the runner's cookie jar owns sessions);
- every ``{{var}}`` is defined before use;
- bounded size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

KIND = "api_recipe"
STATUSES = ("candidate", "active", "retired")
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
EXTRACT_SOURCES = frozenset({"json", "header", "cookie", "regex"})
BODY_FORMATS = frozenset({"json", "form", "text"})
FORBIDDEN_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization", "host", "content-length"}
)
MAX_STEPS = 20
MAX_PARAMS = 20

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_WHOLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


class RecipeError(ValueError):
    """Raised when a recipe is malformed, unsafe, or cannot be rendered."""


# -- templates ---------------------------------------------------------------


def template_vars(value: Any) -> set[str]:
    """Return every ``{{var}}`` name used anywhere inside *value*."""
    if isinstance(value, str):
        return set(_VAR_RE.findall(value))
    if isinstance(value, Mapping):
        return set().union(*(template_vars(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(template_vars(v) for v in value)) if value else set()
    return set()


def render(template: Any, values: Mapping[str, Any]) -> Any:
    """Replace ``{{var}}`` in strings inside *template* with *values*.

    A string that is exactly one ``{{var}}`` becomes the value itself, so a
    JSON body can carry numbers or booleans. Other strings get ``str(value)``
    spliced in. Dict keys are never templated. A missing var raises
    :class:`RecipeError`.
    """
    if isinstance(template, str):
        whole = _WHOLE_VAR_RE.match(template)
        if whole:
            return _lookup(values, whole.group(1))
        return _VAR_RE.sub(lambda m: str(_lookup(values, m.group(1))), template)
    if isinstance(template, Mapping):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def _lookup(values: Mapping[str, Any], name: str) -> Any:
    if name not in values:
        raise RecipeError(f"no value for {{{{{name}}}}}")
    return values[name]


# -- types ---------------------------------------------------------------------


@dataclass
class Param:
    """A value the task supplies, e.g. the guest's name."""

    name: str
    description: str = ""
    example: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Param":
        return cls(name=d["name"], description=d.get("description", ""), example=d.get("example"))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "example": self.example}


@dataclass
class Extract:
    """Read one value from a step's response into a var for later steps.

    ``source``: ``json`` (dotted ``path``, list indexes allowed: ``items.0.id``),
    ``header`` (header name), ``cookie`` (cookie name), or ``regex`` (first
    group on the response body).
    """

    var: str
    source: str
    path: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Extract":
        return cls(var=d["var"], source=d["from"], path=d["path"])

    def to_dict(self) -> dict[str, Any]:
        return {"var": self.var, "from": self.source, "path": self.path}


@dataclass
class Step:
    """One HTTP request."""

    id: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_format: str = "json"  # json | form | text
    expect_status: int = 200
    extract: list[Extract] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Step":
        return cls(
            id=d["id"],
            method=str(d["method"]).upper(),
            url=d["url"],
            headers=dict(d.get("headers") or {}),
            body=d.get("body"),
            body_format=d.get("body_format", "json"),
            expect_status=int(d.get("expect_status", 200)),
            extract=[Extract.from_dict(e) for e in d.get("extract") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "url": self.url,
            "headers": dict(self.headers),
            "body": self.body,
            "body_format": self.body_format,
            "expect_status": self.expect_status,
            "extract": [e.to_dict() for e in self.extract],
        }


@dataclass
class Recipe:
    """A validated chain of HTTP requests that completes one kind of task."""

    name: str
    description: str
    scope: dict[str, Any]  # {"site": "host[:port]", "task_key": "..."}
    steps: list[Step]
    params: list[Param] = field(default_factory=list)
    verify: dict[str, Any] | None = None  # {"url": "..."}
    status: str = "candidate"
    version: int = 1
    parent_id: Any = None
    id: Any = None  # MongoDB _id once stored
    kind: str = KIND

    @property
    def site(self) -> str:
        return str(self.scope.get("site", "")).lower()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Recipe":
        try:
            return cls(
                name=d["name"],
                description=d.get("description", ""),
                scope=dict(d.get("scope") or {}),
                steps=[Step.from_dict(s) for s in d.get("steps") or []],
                params=[Param.from_dict(p) for p in d.get("params") or []],
                verify=dict(d["verify"]) if d.get("verify") else None,
                status=d.get("status", "candidate"),
                version=int(d.get("version", 1)),
                parent_id=d.get("parent_id"),
                id=d.get("_id", d.get("id")),
                kind=d.get("kind", KIND),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RecipeError(f"malformed recipe: {exc!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        """Document for the ``skills`` collection (without ``_id``)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "version": self.version,
            "parent_id": self.parent_id,
            "description": self.description,
            "scope": dict(self.scope),
            "params": [p.to_dict() for p in self.params],
            "steps": [s.to_dict() for s in self.steps],
            "verify": dict(self.verify) if self.verify else None,
        }

    # -- validation ------------------------------------------------------

    def validate(self) -> "Recipe":
        """Raise :class:`RecipeError` unless the recipe is safe to store and run."""
        if self.kind != KIND:
            raise RecipeError(f"kind must be {KIND!r}")
        if not self.name or len(self.name) > 200:
            raise RecipeError("name must be 1-200 characters")
        if not self.description:
            raise RecipeError("description must not be empty")
        if self.status not in STATUSES:
            raise RecipeError(f"status must be one of {STATUSES}")
        if not isinstance(self.version, int) or self.version < 1:
            raise RecipeError("version must be a positive integer")
        site = self.site
        if not site or "/" in site or "{{" in site:
            raise RecipeError("scope.site must be a host[:port]")
        if not 1 <= len(self.steps) <= MAX_STEPS:
            raise RecipeError(f"a recipe needs 1-{MAX_STEPS} steps")
        if len(self.params) > MAX_PARAMS:
            raise RecipeError(f"at most {MAX_PARAMS} params")

        defined: set[str] = set()
        for param in self.params:
            if not _NAME_RE.match(param.name):
                raise RecipeError(f"bad param name {param.name!r}")
            if param.name in defined:
                raise RecipeError(f"duplicate param {param.name!r}")
            defined.add(param.name)

        step_ids: set[str] = set()
        for step in self.steps:
            where = f"step {step.id!r}"
            if not _NAME_RE.match(step.id) or step.id in step_ids:
                raise RecipeError(f"{where}: id must be a unique identifier")
            step_ids.add(step.id)
            if step.method not in METHODS:
                raise RecipeError(f"{where}: method {step.method!r} not allowed")
            self._check_url(step.url, where)
            if step.body_format not in BODY_FORMATS:
                raise RecipeError(f"{where}: body_format must be one of {sorted(BODY_FORMATS)}")
            if not 100 <= step.expect_status <= 599:
                raise RecipeError(f"{where}: expect_status out of range")
            for name in step.headers:
                if name.lower() in FORBIDDEN_HEADERS:
                    raise RecipeError(f"{where}: header {name!r} is not allowed in a recipe")
            used = template_vars(step.url) | template_vars(step.headers) | template_vars(step.body)
            missing = used - defined
            if missing:
                raise RecipeError(f"{where}: undefined vars {sorted(missing)}")
            for ex in step.extract:
                if not _NAME_RE.match(ex.var):
                    raise RecipeError(f"{where}: bad extract var {ex.var!r}")
                if ex.source not in EXTRACT_SOURCES:
                    raise RecipeError(f"{where}: extract from {ex.source!r} not allowed")
                if not ex.path:
                    raise RecipeError(f"{where}: extract {ex.var!r} needs a path")
                if ex.source == "regex":
                    try:
                        if re.compile(ex.path).groups < 1:
                            raise RecipeError(f"{where}: regex for {ex.var!r} needs a group")
                    except re.error as exc:
                        raise RecipeError(f"{where}: bad regex for {ex.var!r}: {exc}") from exc
                defined.add(ex.var)

        if self.verify is not None:
            url = self.verify.get("url")
            if not isinstance(url, str) or template_vars(url):
                raise RecipeError("verify.url must be a plain URL")
            self._check_url(url, "verify")
        return self

    def _check_url(self, url: str, where: str) -> None:
        if not isinstance(url, str):
            raise RecipeError(f"{where}: url must be a string")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise RecipeError(f"{where}: url must be http or https")
        if "{{" in parts.netloc or parts.username or parts.password:
            raise RecipeError(f"{where}: url host must be literal, without credentials")
        if parts.netloc.lower() != self.site:
            raise RecipeError(f"{where}: host {parts.netloc!r} is outside scope.site {self.site!r}")
