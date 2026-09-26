"""Fail the suite if a banned dependency appears anywhere in the repository.

The banned names are split so this file does not match itself.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = ("stream" + "lit",)
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".recordings", ".uv-cache", ".uv-python"}


def _files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            yield path


class BannedDependencyTests(unittest.TestCase):
    def test_no_banned_names_in_repo(self):
        hits = []
        for path in _files():
            rel = path.relative_to(ROOT).as_posix()
            try:
                text = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                text = ""
            for name in BANNED:
                if name in rel.lower() or name in text:
                    hits.append(f"{rel}: {name}")
        self.assertEqual(hits, [], "banned dependency found; see AGENTS.md")


if __name__ == "__main__":
    unittest.main()
