"""Local login sessions for recipe replay.

A session file is a Playwright ``storage_state`` JSON saved after a recording.
It holds live login cookies (e.g. LinkedIn's ``li_at``). Treat it like a
password:

- it stays on local disk only, under ``.recordings/sessions/`` (mode 0600);
- it is never sent to MongoDB or to a model;
- ``.recordings/`` is gitignored, so it is never committed.

Recipes never contain cookies. The runner seeds its cookie jar from this file
at replay time, and ``Recipe.session`` vars are read from the same cookies.
"""

from __future__ import annotations

import json
import os
import re
import time
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

SESSION_DIR = Path(".recordings/sessions")

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def session_path(site: str) -> Path:
    """Path of the session file for one ``host[:port]``."""
    name = _UNSAFE_RE.sub("_", site.strip().lower())
    if not name.strip("._"):
        raise ValueError("site must be a host[:port]")
    return SESSION_DIR / f"{name}.json"


def save_session(context: Any, site: str) -> Path:
    """Write the Playwright context's storage state for *site*, owner-only."""
    path = session_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    context.storage_state(path=str(path))
    os.chmod(path, 0o600)
    return path


def load_storage_state(site: str) -> dict[str, Any] | None:
    """Parsed storage state for *site*, or None if no session is saved."""
    path = session_path(site)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def delete_session(site: str) -> None:
    """Remove the saved session for *site*, if any."""
    session_path(site).unlink(missing_ok=True)


def cookies_for(site: str, storage_state: dict[str, Any] | None = None) -> dict[str, str]:
    """Cookie name -> value for live cookies that apply to *site*'s host."""
    return {c.name: c.value or "" for c in _cookies(site, storage_state)}


def cookie_jar_for(site: str, storage_state: dict[str, Any] | None = None) -> CookieJar:
    """A cookie jar pre-seeded with *site*'s live session cookies."""
    jar = CookieJar()
    for cookie in _cookies(site, storage_state):
        jar.set_cookie(cookie)
    return jar


def _cookies(site: str, storage_state: dict[str, Any] | None) -> list[Cookie]:
    if storage_state is None:
        storage_state = load_storage_state(site)
    if not storage_state:
        return []
    host = _host(site)
    now = time.time()
    out: list[Cookie] = []
    for raw in storage_state.get("cookies") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        domain = str(raw.get("domain") or "").lower()
        if not _domain_matches(host, domain):
            continue
        expires = raw.get("expires")
        expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
        if expires is not None and expires <= now:
            continue
        dotted = domain.startswith(".")
        out.append(
            Cookie(
                version=0,
                name=str(raw["name"]),
                value=str(raw.get("value", "")),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=dotted,
                domain_initial_dot=dotted,
                path=str(raw.get("path") or "/"),
                path_specified=True,
                secure=bool(raw.get("secure")),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None} if raw.get("httpOnly") else {},
            )
        )
    return out


def _host(site: str) -> str:
    site = site.strip().lower()
    if site.startswith("["):  # [ipv6]:port
        return site[1 : site.index("]")] if "]" in site else site
    return site.rsplit(":", 1)[0] if site.count(":") == 1 else site


def _domain_matches(host: str, domain: str) -> bool:
    """Playwright marks domain cookies with a leading dot; others are host-only."""
    bare = domain.lstrip(".")
    if not bare:
        return False
    if not domain.startswith("."):
        return host == bare
    return host == bare or host.endswith("." + bare)
