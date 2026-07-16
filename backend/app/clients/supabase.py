"""Supabase client: one connection, service-role, optional.

Persistence is deliberately optional. Every caller here is written so that a
missing or unreachable Supabase degrades the pipeline to exactly what it was
before — a run that writes results.json and renders from it. A database outage
must never be the reason a reviewer sees a blank page, which is the same
principle the static-snapshot frontend is built on.

This module holds the **service_role** key, which bypasses Row Level Security.
It must never be handed to a browser. The anon key (read-only under RLS) is the
one that is safe to expose.
"""

from __future__ import annotations

from typing import Any

from app.config import (
    SUPABASE_BUCKET,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    supabase_enabled,
)
from app.utils.logging import get_logger

log = get_logger("clients.supabase")

_client: Any | None = None
_unavailable_reason: str | None = None


def get_client() -> Any | None:
    """The shared service-role client, or None if persistence is not configured.

    Returns None rather than raising: callers treat "no database" as a normal
    mode, not an error.
    """
    global _client, _unavailable_reason

    if _client is not None:
        return _client
    if _unavailable_reason is not None:
        return None

    if not supabase_enabled():
        _unavailable_reason = "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"
        log.info("supabase not configured — results will be written to results.json only")
        return None

    try:
        from supabase import create_client
    except ImportError:
        _unavailable_reason = "supabase package not installed (pip install -r requirements.txt)"
        log.warning("supabase package missing — persistence disabled")
        return None

    try:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception as exc:
        _unavailable_reason = str(exc)
        log.warning("could not create supabase client (%s) — persistence disabled", exc)
        return None

    log.info("supabase connected: %s | bucket: %s", SUPABASE_URL, SUPABASE_BUCKET)
    return _client


def is_available() -> bool:
    return get_client() is not None


def unavailable_reason() -> str | None:
    get_client()
    return _unavailable_reason
