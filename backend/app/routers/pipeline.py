"""POST /api/run, GET /api/status/{run_id}.

The run is a background task because a full pass makes dozens of network calls
and several image generations; holding an HTTP connection open for that is how
you discover your host's proxy timeout.

**This endpoint is unauthenticated and it spends money.** That is a deliberate
decision, not an oversight: the brief rules out auth, and a reviewer has to be
able to press the button without being handed a credential. The mitigation is to
bound the damage rather than lock the door — one run at a time, a rolling hourly
cap, and a low MAX_VENUES on the deployed instance. See config.py for the
numbers and the worst-case arithmetic.

If this were carrying real traffic rather than a reviewer's curiosity, it would
be a signed webhook or a queue with a service token. For a demo that has to be
clickable by a stranger, capped-and-open beats locked-and-useless.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.config import (
    MAX_CONCURRENT_RUNS,
    MAX_RUNS_PER_HOUR,
    MAX_VENUES,
    MAX_VENUES_HARD_CAP,
    TARGET_ACCEPTED,
    TARGET_ACCEPTED_HARD_CAP,
)
from app.schemas import RunStatus
from app.services import pipeline as pipeline_svc
from app.services import repository
from app.utils.logging import get_logger

log = get_logger("routers.pipeline")

router = APIRouter(prefix="/api", tags=["pipeline"])

# Timestamps of recent run starts. In-memory is the right scope here: the cap
# exists to stop a single deployed instance burning its own key, and that
# instance is exactly what this list lives in. It resets on restart, which on a
# free tier that sleeps is a feature, not a leak.
_recent_starts: list[float] = []

_HOUR_S = 3600.0


def _prune() -> None:
    cutoff = time.time() - _HOUR_S
    _recent_starts[:] = [t for t in _recent_starts if t > cutoff]


def _active_runs() -> int:
    return sum(1 for s in pipeline_svc._RUNS.values() if not s.done)


class RunRequest(BaseModel):
    """Optional per-run settings. Omitted fields fall back to config defaults.

    The frontend sends the user's chosen values; the server is the authority on
    the ceiling, so both are clamped here regardless of what was requested. A
    knob a user can turn is only safe if the server, not the client, enforces the
    cap.
    """

    max_venues: int | None = Field(default=None, ge=1)
    target_accepted: int | None = Field(default=None, ge=1)
    # Default true = the historical behaviour. False skips venues an earlier run
    # already accepted.
    allow_duplicates: bool = True


def _clamp_settings(req: RunRequest) -> tuple[int, int]:
    """Resolve and clamp the requested settings to the hard caps."""
    max_venues = req.max_venues if req.max_venues is not None else MAX_VENUES
    max_venues = max(1, min(max_venues, MAX_VENUES_HARD_CAP))

    target = req.target_accepted if req.target_accepted is not None else TARGET_ACCEPTED
    target = max(1, min(target, TARGET_ACCEPTED_HARD_CAP))
    # Never chase more acceptances than venues we will even process.
    target = min(target, max_venues)
    return max_venues, target


@router.post("/run")
def start_run(background: BackgroundTasks, req: RunRequest | None = None) -> dict[str, object]:
    """Kick off a pipeline run. Returns immediately with a run_id to poll.

    Accepts an optional JSON body of run settings (max_venues, target_accepted),
    clamped server-side to the hard caps. 409 if a run is already going (with that
    run's id). 429 if the hourly cap is spent.
    """
    _prune()
    max_venues, target_accepted = _clamp_settings(req or RunRequest())

    # Already running? Hand back the in-flight run rather than starting a second.
    # A reviewer double-clicking the button should watch the first run, not pay
    # for two.
    if _active_runs() >= MAX_CONCURRENT_RUNS:
        in_flight = next((s for s in pipeline_svc._RUNS.values() if not s.done), None)
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A pipeline run is already in progress.",
                "run_id": in_flight.run_id if in_flight else None,
                "stage": in_flight.stage if in_flight else None,
            },
        )

    if len(_recent_starts) >= MAX_RUNS_PER_HOUR:
        oldest = min(_recent_starts)
        retry_in = int(_HOUR_S - (time.time() - oldest))
        log.warning("run rejected: hourly cap of %d reached", MAX_RUNS_PER_HOUR)
        raise HTTPException(
            status_code=429,
            detail={
                "message": (
                    f"Rate limited: {MAX_RUNS_PER_HOUR} runs per hour. This endpoint is "
                    f"open so it can be tried without a credential, and capped so it "
                    f"cannot be used to burn the project's API budget."
                ),
                "retry_after_seconds": max(retry_in, 0),
            },
        )

    allow_duplicates = (req or RunRequest()).allow_duplicates

    run_id = pipeline_svc.new_run_id()
    pipeline_svc._RUNS[run_id] = RunStatus(run_id=run_id, stage="queued")
    _recent_starts.append(time.time())
    background.add_task(
        pipeline_svc.run_pipeline, run_id, max_venues, target_accepted, allow_duplicates
    )

    log.info(
        "run %s queued (max_venues=%d, target=%d, duplicates=%s)",
        run_id,
        max_venues,
        target_accepted,
        "allowed" if allow_duplicates else "skipped",
    )
    return {
        "run_id": run_id,
        "max_venues": max_venues,
        "target_accepted": target_accepted,
        "allow_duplicates": allow_duplicates,
    }


@router.get("/status/{run_id}", response_model=RunStatus)
def get_status(run_id: str) -> RunStatus:
    """Live status.

    In-memory first (it is the freshest — the running task updates it between
    every database write), then the database. The fallback matters: the process
    holding _RUNS can restart mid-run, and without it a reviewer's page would
    poll a 404 forever and conclude the run vanished.
    """
    if (status := pipeline_svc.get_status(run_id)) is not None:
        return status
    if (persisted := repository.get_status(run_id)) is not None:
        return persisted
    raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
