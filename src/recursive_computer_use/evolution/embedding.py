"""Small deterministic local embeddings for privacy-preserving demo retrieval."""

from __future__ import annotations

import hashlib
import math
import re


EMBEDDING_DIMENSIONS = 64
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def embed_text(text: str, *, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Create a normalized hashing-vector embedding without a cloud call."""

    vector = [0.0] * dimensions
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude:
        vector = [value / magnitude for value in vector]
    return vector
