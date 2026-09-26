"""
auth.py — Resolve OpenAI client credentials.

Priority:
  1. OPENAI_API_KEY in environment (set via .env or shell)
  2. Codex OAuth via local codex-as-api proxy (reads ~/.codex/auth.json automatically)

The codex-as-api proxy handles the Cloudflare challenge and Codex CLI request
shape so we don't have to replicate it. Start it with:

    npx codex-as-api          # or: pip install codex-as-api && codex-as-api

It listens on http://127.0.0.1:18080 by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple


CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"

# Local codex-as-api proxy — handles Codex OAuth transparently
CODEX_PROXY_BASE_URL = "http://127.0.0.1:18080/v1"
CODEX_PROXY_DUMMY_KEY = "unused"  # proxy doesn't check the key


class ClientConfig(NamedTuple):
    api_key: str
    base_url: str | None  # None = use OpenAI default


def resolve() -> ClientConfig:
    """
    Return the (api_key, base_url) pair to pass to ``openai.OpenAI()``.

    Raises ``RuntimeError`` if no credentials are found.
    """
    # 1. Explicit API key in environment (wins over everything)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return ClientConfig(api_key=api_key, base_url=None)

    # 2. Codex OAuth present → route through local proxy
    if _has_codex_oauth():
        return ClientConfig(api_key=CODEX_PROXY_DUMMY_KEY, base_url=CODEX_PROXY_BASE_URL)

    raise RuntimeError(
        "No OpenAI credentials found.\n"
        "Options:\n"
        "  • Set OPENAI_API_KEY in your .env file, OR\n"
        "  • Log in with the Codex CLI: codex login\n"
        "    then start the proxy:      npx codex-as-api"
    )


def _has_codex_oauth() -> bool:
    """Return True if ~/.codex/auth.json contains chatgpt OAuth tokens."""
    if not CODEX_AUTH_PATH.exists():
        return False
    try:
        data = json.loads(CODEX_AUTH_PATH.read_text())
        return (
            data.get("auth_mode") == "chatgpt"
            and bool(data.get("tokens", {}).get("access_token"))
        )
    except Exception:
        return False
