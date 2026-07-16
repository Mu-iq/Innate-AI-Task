"""FastAPI app: CORS, routers, datastore status.

The pipeline persists every run to the database and its images to the storage
bucket; the frontend reads runs and history back through this API. The database
is the source of truth for what a run produced.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.clients import supabase as supabase_client
from app.config import (
    CORS_LOCALHOST_REGEX,
    CORS_ORIGINS,
    DRY_RUN,
    GEMINI_API_KEY,
    GOOGLE_MAPS_API_KEY,
    MAX_VENUES,
    MAX_VENUES_HARD_CAP,
    SUPABASE_BUCKET,
    TARGET_ACCEPTED,
    TARGET_ACCEPTED_HARD_CAP,
)
from app.routers import pipeline, results
from app.utils.logging import get_logger

log = get_logger("main")

app = FastAPI(
    title="Storefront Capture & Visualisation",
    description=(
        "Finds independent London venues with bare frontages, photographs their real "
        "entrance, composites the client's actual planters onto it, and decides "
        "automatically whether the result is good enough to send to the owner."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    # Deployed origins, named explicitly via CORS_EXTRA_ORIGINS.
    allow_origins=list(CORS_ORIGINS),
    # Any localhost port, so a dev server that lands on 5174 instead of 5173 does
    # not fail with an opaque "Failed to fetch" and a 200 in the server log.
    allow_origin_regex=CORS_LOCALHOST_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(pipeline.router)
app.include_router(results.router)


@app.on_event("startup")
def _log_persistence_status() -> None:
    """Make the datastore state obvious at boot.

    The most confusing failure mode is starting the server before .env has the
    Supabase keys: uvicorn --reload restarts on code changes, not on .env
    changes, so the process keeps stale config and silently saves nothing. A loud
    line at startup means that is spotted immediately, not after a wasted run.
    """
    if supabase_client.is_available():
        log.info("PERSISTENCE: ON — runs are saved to the database (bucket: %s)", SUPABASE_BUCKET)
    else:
        log.warning(
            "PERSISTENCE: OFF — %s. Runs will NOT be saved. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in .env and restart this server.",
            supabase_client.unavailable_reason(),
        )


@app.get("/api/health")
def health() -> dict[str, object]:
    """Config visibility without leaking secrets.

    Reports whether each credential is *present*, never what it is. `persistence`
    is the one worth watching: the pipeline reads and writes the database, so a
    run only shows up in history when this is "on".
    """
    return {
        "status": "ok",
        "maps_key_configured": bool(GOOGLE_MAPS_API_KEY),
        "gemini_key_configured": bool(GEMINI_API_KEY),
        "dry_run": DRY_RUN,
        "persistence": "on" if supabase_client.is_available() else "off",
        "persistence_detail": supabase_client.unavailable_reason(),
        "bucket": SUPABASE_BUCKET if supabase_client.is_available() else None,
        # The knobs the UI exposes, with defaults and the ceilings it must not
        # let the user exceed. The server clamps regardless, but the UI reads
        # these so its inputs match what will actually be accepted.
        "settings": {
            "max_venues": {"default": MAX_VENUES, "min": 1, "max": MAX_VENUES_HARD_CAP},
            "target_accepted": {
                "default": TARGET_ACCEPTED,
                "min": 1,
                "max": TARGET_ACCEPTED_HARD_CAP,
            },
        },
    }
