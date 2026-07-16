"""Persistence. Every database read and write lives here.

Design rules:

1. **Writes fail soft.** A run costs real money in image generation. If the
   database is unreachable, losing the run would be worse than losing the
   history, so every write logs and returns rather than raising. results.json is
   still written, the images are still on disk, and the frontend still renders.

2. **Reads return None/empty, never raise.** The API falls back to results.json.

3. **No SQL outside this module.** Services deal in pydantic models; this is the
   only place that knows the schema exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.clients import supabase as supabase_client
from app.schemas import (
    Rejection,
    ResultsPayload,
    RunCost,
    RunSettings,
    RunStatus,
    RunSummary,
    Thresholds,
    VenueCandidate,
    VenueResult,
)
from app.services import storage
from app.utils.logging import get_logger

log = get_logger("repository")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def available() -> bool:
    return supabase_client.is_available()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def create_run(
    run_key: str,
    *,
    dry_run: bool,
    max_venues: int,
    target_accepted: int,
) -> str | None:
    """Insert a queued run. Returns its uuid, or None if persistence is off."""
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("runs")
            .insert(
                {
                    "run_key": run_key,
                    "status": "running",
                    "stage": "setup",
                    "dry_run": dry_run,
                    "max_venues": max_venues,
                    "target_accepted": target_accepted,
                }
            )
            .execute()
        )
        return res.data[0]["id"] if res.data else None
    except Exception as exc:
        log.warning("could not create run row: %s", str(exc)[:160])
        return None


def update_run(run_id: str | None, **fields: Any) -> None:
    """Patch a run row. Silent no-op when persistence is off."""
    client = supabase_client.get_client()
    if client is None or not run_id:
        return
    try:
        client.table("runs").update(fields).eq("id", run_id).execute()
    except Exception as exc:
        log.warning("could not update run: %s", str(exc)[:160])


def upsert_venue(venue: VenueCandidate) -> str | None:
    """Insert or refresh a venue by place_id. Returns its uuid.

    Venues outlive runs, so this is an upsert on the natural key: the same cafe
    discovered next week updates its row rather than duplicating it, and
    first_seen_at is preserved by leaving it out of the payload.
    """
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("venues")
            .upsert(
                {
                    "place_id": venue.id,
                    "name": venue.name,
                    "address": venue.address,
                    "postcode": venue.postcode,
                    "lat": venue.lat,
                    "lng": venue.lng,
                    "area": venue.area,
                    "primary_type": venue.primary_type,
                    "types": venue.types,
                    "business_status": venue.business_status,
                    "rating": venue.rating,
                    "user_ratings_total": venue.user_ratings_total,
                    "last_seen_at": _now(),
                },
                on_conflict="place_id",
            )
            .execute()
        )
        return res.data[0]["id"] if res.data else None
    except Exception as exc:
        log.warning("could not upsert venue %s: %s", venue.name, str(exc)[:160])
        return None


def record_accepted(
    run_id: str | None,
    venue_uuid: str | None,
    result: VenueResult,
    frontage_path: str | None,
    composite_path: str | None,
) -> None:
    client = supabase_client.get_client()
    if client is None or not run_id or not venue_uuid:
        return
    try:
        client.table("run_results").upsert(
            {
                "run_id": run_id,
                "venue_id": venue_uuid,
                "outcome": "accepted",
                "stage": "verify",
                "kind": None,
                "reasons": [],
                "detail": "",
                "image_source": result.image_source,
                "heading_used": result.heading_used,
                "pano_distance_m": result.pano_distance_m,
                "product_slug": result.product_slug,
                "assessment": result.assessment.model_dump(mode="json"),
                "measurement": result.measurement.model_dump(mode="json"),
                "verification": result.verification.model_dump(mode="json"),
                "frontage_path": frontage_path,
                "composite_path": composite_path,
                "attempts": result.attempts,
            },
            on_conflict="run_id,venue_id",
        ).execute()
    except Exception as exc:
        log.warning("could not record accepted venue %s: %s", result.name, str(exc)[:200])


def record_rejected(
    run_id: str | None,
    venue_uuid: str | None,
    rejection: Rejection,
    frontage_path: str | None = None,
) -> None:
    client = supabase_client.get_client()
    if client is None or not run_id or not venue_uuid:
        return
    try:
        client.table("run_results").upsert(
            {
                "run_id": run_id,
                "venue_id": venue_uuid,
                "outcome": "rejected",
                "stage": rejection.stage,
                "kind": rejection.kind,
                # The DB constrains a rejection to carry at least one reason.
                # Honour that here rather than letting the insert bounce.
                "reasons": rejection.reasons or ["Rejected without a stated reason"],
                "detail": rejection.detail,
                "frontage_path": frontage_path,
            },
            on_conflict="run_id,venue_id",
        ).execute()
    except Exception as exc:
        log.warning("could not record rejection for %s: %s", rejection.venue_name, str(exc)[:200])


def finish_run(run_id: str | None, payload: ResultsPayload, error: str | None) -> None:
    """Write the final funnel and outcome."""
    if not run_id:
        return
    f = payload.funnel
    update_run(
        run_id,
        status="failed" if error else "succeeded",
        stage="failed" if error else "done",
        error=error,
        finished_at=_now(),
        vision_model=payload.vision_model or None,
        image_model=payload.image_model or None,
        thresholds=payload.thresholds.model_dump(mode="json") if payload.thresholds else {},
        funnel_discovered=f.discovered,
        funnel_after_chain_filter=f.after_chain_filter,
        funnel_after_status_filter=f.after_status_filter,
        funnel_entered_pipeline=f.entered_pipeline,
        funnel_capture_ok=f.capture_ok,
        funnel_assess_ok=f.assess_ok,
        funnel_measure_ok=f.measure_ok,
        funnel_composite_ok=f.composite_ok,
        funnel_accepted=f.accepted,
        accepted=len(payload.venues),
        rejected=len(payload.rejected),
        total_cost_usd=payload.cost.total_cost_usd if payload.cost else 0,
        metrics=payload.cost.model_dump(mode="json") if payload.cost else {},
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def list_runs(limit: int = 25) -> list[RunSummary]:
    """Run history, newest first. Empty list if persistence is off."""
    client = supabase_client.get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("run_summaries")
            .select("*")
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        log.warning("could not list runs: %s", str(exc)[:160])
        return []

    out: list[RunSummary] = []
    for row in res.data or []:
        try:
            out.append(
                RunSummary(
                    run_key=row["run_key"],
                    status=row["status"],
                    stage=row.get("stage") or "",
                    started_at=row["started_at"],
                    finished_at=row.get("finished_at"),
                    duration_s=row.get("duration_s"),
                    dry_run=bool(row.get("dry_run")),
                    vision_model=row.get("vision_model") or "",
                    image_model=row.get("image_model") or "",
                    max_venues=row.get("max_venues"),
                    target_accepted=row.get("target_accepted"),
                    total_cost_usd=float(row.get("total_cost_usd") or 0),
                    discovered=row.get("funnel_discovered") or 0,
                    entered_pipeline=row.get("funnel_entered_pipeline") or 0,
                    accepted=row.get("accepted") or 0,
                    rejected_decisions=row.get("rejected_decisions") or 0,
                    rejected_errors=row.get("rejected_errors") or 0,
                    error=row.get("error"),
                )
            )
        except Exception as exc:  # a malformed row must not break the list
            log.warning("skipping unreadable run row: %s", str(exc)[:120])
    return out


def _row_to_venue_result(row: dict[str, Any]) -> VenueResult | None:
    """Rebuild a VenueResult from a joined run_results row."""
    v = row.get("venues") or {}
    try:
        return VenueResult(
            id=v.get("place_id", ""),
            name=v.get("name", ""),
            address=v.get("address", ""),
            postcode=v.get("postcode", ""),
            lat=v.get("lat", 0.0),
            lng=v.get("lng", 0.0),
            area=v.get("area", ""),
            image_source=row.get("image_source") or "streetview",
            heading_used=row.get("heading_used"),
            pano_distance_m=row.get("pano_distance_m"),
            product_slug=row.get("product_slug") or "",
            product_description="",
            assessment=row["assessment"],
            measurement=row["measurement"],
            verification=row["verification"],
            # Absolute bucket URLs — the frontend uses them as-is.
            frontage_url=storage.public_url(row.get("frontage_path")) or "",
            composite_url=storage.public_url(row.get("composite_path")) or "",
            attempts=row.get("attempts") or 1,
        )
    except Exception as exc:
        log.warning("skipping unreadable result row: %s", str(exc)[:160])
        return None


def get_run(run_key: str | None = None) -> ResultsPayload | None:
    """Full payload for a run. `None` run_key means the most recent finished run.

    Returns None if persistence is off or the run does not exist, so the caller
    can fall back to results.json.
    """
    client = supabase_client.get_client()
    if client is None:
        return None

    try:
        q = client.table("runs").select("*")
        if run_key:
            q = q.eq("run_key", run_key)
        else:
            q = q.order("started_at", desc=True)
        res = q.limit(1).execute()
        if not res.data:
            return None
        run = res.data[0]

        rows = (
            client.table("run_results")
            .select("*, venues(*)")
            .eq("run_id", run["id"])
            .execute()
        ).data or []
    except Exception as exc:
        log.warning("could not read run %s: %s", run_key or "latest", str(exc)[:160])
        return None

    venues: list[VenueResult] = []
    rejected: list[Rejection] = []

    for row in rows:
        if row.get("outcome") == "accepted":
            if (vr := _row_to_venue_result(row)) is not None:
                venues.append(vr)
        else:
            v = row.get("venues") or {}
            rejected.append(
                Rejection(
                    venue_id=v.get("place_id", ""),
                    venue_name=v.get("name", ""),
                    address=v.get("address", ""),
                    stage=row.get("stage") or "discover",
                    kind=row.get("kind") or "decision",
                    reasons=row.get("reasons") or [],
                    detail=row.get("detail") or "",
                    at=row.get("created_at") or _now(),
                )
            )

    thresholds = None
    if run.get("thresholds"):
        try:
            thresholds = Thresholds.model_validate(run["thresholds"])
        except Exception:
            thresholds = None

    cost = None
    if run.get("metrics"):
        try:
            cost = RunCost.model_validate(run["metrics"])
        except Exception:
            cost = None

    settings = None
    if run.get("max_venues") is not None:
        settings = RunSettings(
            max_venues=run["max_venues"],
            target_accepted=run.get("target_accepted") or 0,
        )

    return ResultsPayload(
        run_id=run["run_key"],
        generated_at=run.get("finished_at") or run.get("started_at") or _now(),
        dry_run=bool(run.get("dry_run")),
        vision_model=run.get("vision_model") or "",
        image_model=run.get("image_model") or "",
        thresholds=thresholds,
        settings=settings,
        cost=cost,
        funnel={
            "discovered": run.get("funnel_discovered") or 0,
            "after_chain_filter": run.get("funnel_after_chain_filter") or 0,
            "after_status_filter": run.get("funnel_after_status_filter") or 0,
            "entered_pipeline": run.get("funnel_entered_pipeline") or 0,
            "capture_ok": run.get("funnel_capture_ok") or 0,
            "assess_ok": run.get("funnel_assess_ok") or 0,
            "measure_ok": run.get("funnel_measure_ok") or 0,
            "composite_ok": run.get("funnel_composite_ok") or 0,
            "accepted": run.get("funnel_accepted") or 0,
        },
        venues=venues,
        rejected=rejected,
        source="database",
    )


def get_status(run_key: str) -> RunStatus | None:
    """Live status straight from the DB, so polling survives a restart."""
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        res = client.table("runs").select("*").eq("run_key", run_key).limit(1).execute()
        if not res.data:
            return None
        r = res.data[0]
        return RunStatus(
            run_id=r["run_key"],
            stage=r.get("stage") or "",
            processed=r.get("processed") or 0,
            accepted=r.get("accepted") or 0,
            rejected=r.get("rejected") or 0,
            done=r.get("status") in ("succeeded", "failed"),
            error=r.get("error"),
            started_at=r.get("started_at") or _now(),
            finished_at=r.get("finished_at"),
        )
    except Exception as exc:
        log.warning("could not read status for %s: %s", run_key, str(exc)[:160])
        return None
