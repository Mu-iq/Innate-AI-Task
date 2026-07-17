"""Reads: run history, a specific run, the latest run, and one venue.

The database is the single source of truth. `?run=<key>` selects a specific run;
omitting it returns the most recent. When there are no runs yet, an empty payload
is returned (not a 404) so the frontend can render a clean "no runs yet" state.

If the database is not configured or unreachable, these endpoints say so
explicitly rather than inventing data.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ResultsPayload, RunSummary, VenueResult
from app.services import repository, storage

router = APIRouter(prefix="/api", tags=["results"])


def _require_db() -> None:
    if not repository.available():
        raise HTTPException(
            status_code=503,
            detail=(
                "The results database is not available. It is not configured, or the "
                "server could not reach it."
            ),
        )


@router.get("/runs", response_model=list[RunSummary])
def list_runs(limit: int = Query(25, ge=1, le=100)) -> list[RunSummary]:
    """Run history, newest first. Empty list when there are no runs yet."""
    return repository.list_runs(limit=limit)


@router.get("/results", response_model=ResultsPayload)
def get_results(
    run: str | None = Query(None, description="run_key; omit for the latest run"),
) -> ResultsPayload:
    """One run's accepted venues, decision trails and rejections.

    Latest run when `run` is omitted. An empty payload when the database has no
    runs yet, so the UI can render a first-run prompt rather than an error.
    """
    if (payload := repository.get_run(run)) is not None:
        return payload

    if run:
        _require_db()
        raise HTTPException(status_code=404, detail=f"No run with key {run}")

    # No specific run requested and none exist yet.
    _require_db()
    return ResultsPayload(source="database")


@router.delete("/runs/{run_key}")
def delete_run(run_key: str) -> dict[str, object]:
    """Delete a run: its row, its results, and its images in the bucket.

    Images go first. If the row went first and the bucket call then failed, the
    files would be orphaned with nothing left pointing at them — unreachable, and
    still billed for. This order can at worst leave a run with missing images,
    which is visible and fixable.

    `venues` are never touched: a venue is a fact about London, not this run's
    output, and other runs' results still reference it.
    """
    _require_db()

    removed = storage.delete_run_objects(run_key)
    if not repository.delete_run(run_key):
        raise HTTPException(status_code=404, detail=f"No run with key {run_key}")

    return {"deleted": run_key, "objects_removed": removed}


@router.get("/results/{venue_id}", response_model=VenueResult)
def get_venue(venue_id: str, run: str | None = Query(None)) -> VenueResult:
    payload = repository.get_run(run)
    if payload is None:
        _require_db()
        raise HTTPException(status_code=404, detail="No results yet")
    for venue in payload.venues:
        if venue.id == venue_id:
            return venue
    raise HTTPException(status_code=404, detail=f"No accepted venue with id {venue_id}")
