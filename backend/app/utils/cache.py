"""Deterministic on-disk cache for every paid external call.

Every composite is a billed image generation, and this pipeline gets re-run
dozens of times while iterating on prompts and thresholds. A re-run with
unchanged inputs must cost nothing.

The key is a hash of everything that could change the response -- for composites
that is venue id + prompt + model + attempt number, per the brief. Change the
prompt and you get a new generation; change nothing and you get the bytes back
for free. Namespaces keep Street View, Places and composites in separate
directories so one can be cleared without nuking the others.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import CACHE_DIR, CACHE_ENABLED


def cache_key(*parts: Any) -> str:
    """Stable hash of the given parts. Order matters; types are stringified.

    sha256 over a null-separated join, so ("ab", "c") and ("a", "bc") differ.
    """
    joined = "\x00".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _path_for(namespace: str, key: str, suffix: str) -> Path:
    d = CACHE_DIR / namespace
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}{suffix}"


def get_bytes(namespace: str, key: str, suffix: str = ".bin") -> bytes | None:
    """Return cached bytes, or None on a miss or when caching is disabled."""
    if not CACHE_ENABLED:
        return None
    p = _path_for(namespace, key, suffix)
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes()
    return None


def put_bytes(namespace: str, key: str, data: bytes, suffix: str = ".bin") -> Path:
    """Write bytes to the cache and return the path. Always writes, even if
    caching is disabled for reads, so a forced regeneration still populates it."""
    p = _path_for(namespace, key, suffix)
    p.write_bytes(data)
    return p


def get_json(namespace: str, key: str) -> Any | None:
    """Return cached JSON, or None. Corrupt entries are treated as a miss rather
    than raising -- a half-written cache file must never break a run."""
    if not CACHE_ENABLED:
        return None
    p = _path_for(namespace, key, ".json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def put_json(namespace: str, key: str, data: Any) -> Path:
    p = _path_for(namespace, key, ".json")
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return p
