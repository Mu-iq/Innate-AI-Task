"""The orchestrator. The only module that knows the six stages exist.

Every stage is a pure function that takes typed inputs and returns either a
result or a Rejection. None of them import each other. This file is where the
order lives, where the retry policies live, and where the money is spent -- so
it is also where MAX_VENUES, TARGET_ACCEPTED and DRY_RUN are enforced.

The two retry loops, and why they sit at this level rather than inside a stage:

* **Framing retry** (capture -> assess -> capture). If assess rejects a photo for
  framing, capture re-shoots the same panorama at a nudged heading. Capture
  cannot own this because capture does not know the photo was bad; assess cannot
  own it because assess does not take photographs.
* **Composite retry** (composite -> verify -> composite). If verify rejects a
  generation, composite regenerates with the verifier's own reject reasons
  appended to the prompt. Same reasoning.

Both are bounded by named constants. Neither can loop.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.clients import gemini
from app.config import (
    DRY_RUN,
    FRONTAGE_BARE_THRESHOLD,
    HEADING_NUDGE_DEG,
    MAX_COMPOSITE_ATTEMPTS,
    MAX_PANO_DISTANCE_M,
    MAX_VENUES,
    PEOPLE_PROMINENCE_THRESHOLD,
    PRODUCTS_BY_SLUG,
    SCALE_TOLERANCE,
    STANDARD_DOOR_HEIGHT_M,
    TARGET_ACCEPTED,
)
from app.schemas import (
    Assessment,
    Capture,
    Composite,
    Funnel,
    Measurement,
    Rejection,
    ResultsPayload,
    RunCost,
    RunSettings,
    RunStatus,
    Thresholds,
    VenueCandidate,
    VenueResult,
    Verification,
)
from app.services import assess as assess_svc
from app.services import capture as capture_svc
from app.services import composite as composite_svc
from app.services import discovery as discovery_svc
from app.services import measure as measure_svc
from app.services import products as products_svc
from app.services import repository
from app.services import storage
from app.services import verify as verify_svc
from app.utils import metrics
from app.utils.logging import get_logger, run_dir

log = get_logger("pipeline")

# In-memory run registry. The brief explicitly rules out a database, and status
# only needs to outlive the request, not the process.
_RUNS: dict[str, RunStatus] = {}


def get_status(run_id: str) -> RunStatus | None:
    return _RUNS.get(run_id)


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _capture_and_assess(
    venue: VenueCandidate,
    out_dir: Path,
    run_id: str,
) -> tuple[Capture, Assessment] | Rejection:
    """Photograph the frontage and judge it, with one re-shoot on a bad framing.

    Returns the accepted (capture, assessment) pair, or a Rejection carrying the
    reasons from the final attempt.
    """
    cap = capture_svc.capture_frontage(venue, out_dir, attempt=1)
    if isinstance(cap, Rejection):
        return cap

    assessment = assess_svc.assess_frontage(venue, cap, run_id, attempt=1)
    if isinstance(assessment, Rejection):
        return assessment
    if assessment.accepted:
        return cap, assessment

    # One re-shoot, only if a different angle could plausibly help, and only on
    # Street View -- a Places photo has no heading to nudge.
    if assess_svc.is_framing_failure(assessment) and cap.image_source == "streetview":
        log.info("%s: reshooting at %+.0f deg after framing rejection", venue.name, HEADING_NUDGE_DEG)

        cap2 = capture_svc.capture_frontage(
            venue, out_dir, heading_nudge=HEADING_NUDGE_DEG, attempt=2
        )
        if isinstance(cap2, Capture):
            assessment2 = assess_svc.assess_frontage(venue, cap2, run_id, attempt=2)
            if isinstance(assessment2, Assessment) and assessment2.accepted:
                log.info("%s: re-shoot fixed the framing", venue.name)
                return cap2, assessment2
            if isinstance(assessment2, Assessment):
                assessment = assessment2  # report the second attempt's reasons

    return Rejection(
        venue_id=venue.id,
        venue_name=venue.name,
        address=venue.address,
        stage="assess",
        reasons=assessment.reject_reasons
        or ["Frontage assessed as unusable"],
        detail=(
            f"bareness={assessment.frontage_bare_score}/10, "
            f"entrance_visible={assessment.entrance_visible}, "
            f"framing_usable={assessment.framing_usable}, "
            f"source={cap.image_source}"
        ),
    )


def _composite_and_verify(
    venue: VenueCandidate,
    cap: Capture,
    measurement: Measurement,
    product_slug: str,
    plates: dict[str, Path],
    out_dir: Path,
    run_id: str,
    model: str,
) -> tuple[Composite, Verification] | Rejection:
    """Generate and verify, retrying once with the verifier's own complaints.

    This is the loop the brief calls the differentiator. A generation that fails
    twice is abandoned: a third billed attempt on a frontage the model has
    already failed twice is worse value than the next venue.
    """
    reject_reasons: list[str] = []
    last_rejection: Rejection | None = None

    for attempt in range(1, MAX_COMPOSITE_ATTEMPTS + 1):
        comp = composite_svc.composite_frontage(
            venue=venue,
            capture=cap,
            measurement=measurement,
            product_slug=product_slug,
            plates=plates,
            out_dir=out_dir,
            run_id=run_id,
            model=model,
            attempt=attempt,
            reject_reasons=reject_reasons or None,
        )
        if isinstance(comp, Rejection):
            return comp

        verification = verify_svc.verify_composite(
            venue=venue,
            frontage_path=Path(cap.image_path),
            composite=comp,
            measurement=measurement,
            product_plate=plates[product_slug],
            run_id=run_id,
        )
        if isinstance(verification, Rejection):
            last_rejection = verification
            break

        if verification.verdict == "accept":
            return comp, verification

        reject_reasons = verification.reject_reasons
        last_rejection = Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="verify",
            reasons=reject_reasons,
            detail=f"Failed verification on attempt {attempt} of {MAX_COMPOSITE_ATTEMPTS}.",
        )
        if attempt < MAX_COMPOSITE_ATTEMPTS:
            log.info("%s: retrying composite with %d correction(s)", venue.name, len(reject_reasons))

    return last_rejection or Rejection(
        venue_id=venue.id,
        venue_name=venue.name,
        address=venue.address,
        stage="verify",
        reasons=[f"No acceptable composite after {MAX_COMPOSITE_ATTEMPTS} attempts"],
    )


def _publish_images(
    venue: VenueCandidate,
    run_id: str,
    cap: Capture,
    comp: Composite,
) -> tuple[str, str, str | None, str | None]:
    """Upload the before/after (and the prompt) to the storage bucket.

    The bucket is the single home for generated imagery. Local disk under
    backend/outputs is only ephemeral scratch used while a stage runs; it is not
    served to anyone. The database row stores the bucket object PATHS, and the
    public URLs are what the frontend renders.

    Returns (url_before, url_after, path_before, path_after) — URLs for the API
    payload, paths for the database. Any of these may be empty/None if storage
    is unavailable, which is logged but does not fail the run.
    """
    path_before = storage.upload_file(
        storage.frontage_path(run_id, venue.id), Path(cap.image_path)
    )
    path_after = storage.upload_file(
        storage.composite_path(run_id, venue.id, comp.attempt), Path(comp.image_path)
    )
    # The prompt travels with the image. A composite we cannot reproduce is not
    # defensible, and the bucket is the copy that outlives the container.
    storage.upload_bytes(
        storage.prompt_path(run_id, venue.id, comp.attempt),
        comp.prompt.encode("utf-8"),
        content_type="text/plain",
    )

    url_before = storage.public_url(path_before) or ""
    url_after = storage.public_url(path_after) or ""
    return url_before, url_after, path_before, path_after


def _process_venue(
    venue: VenueCandidate,
    plates: dict[str, Path],
    out_dir: Path,
    run_id: str,
    model: str,
    funnel: Funnel,
    db_run_id: str | None,
) -> VenueResult | Rejection:
    """One venue, all six stages. Returns a result or the reason it failed.

    Accepted venues are persisted here, because this is the only scope that holds
    the bucket paths. Rejections are persisted by the caller, which is the only
    scope that still holds the VenueCandidate a rejection needs.
    """
    ca = _capture_and_assess(venue, out_dir, run_id)
    if isinstance(ca, Rejection):
        if ca.stage == "capture":
            return ca
        funnel.capture_ok += 1
        return ca

    cap, assessment = ca
    funnel.capture_ok += 1
    funnel.assess_ok += 1

    measurement = measure_svc.measure_frontage(
        venue, cap, assessment.product_slug, run_id, attempt=cap.attempt
    )
    if isinstance(measurement, Rejection):
        return measurement
    funnel.measure_ok += 1

    cv = _composite_and_verify(
        venue, cap, measurement, assessment.product_slug, plates, out_dir, run_id, model
    )
    if isinstance(cv, Rejection):
        return cv

    comp, verification = cv
    funnel.composite_ok += 1

    url_before, url_after, path_before, path_after = _publish_images(venue, run_id, cap, comp)

    product = PRODUCTS_BY_SLUG[assessment.product_slug]

    result = VenueResult(
        id=venue.id,
        name=venue.name,
        address=venue.address,
        postcode=venue.postcode,
        lat=venue.lat,
        lng=venue.lng,
        area=venue.area,
        image_source=cap.image_source,
        heading_used=cap.heading_used,
        pano_distance_m=cap.pano_distance_m,
        product_slug=assessment.product_slug,
        product_description=product.description,
        assessment=assessment,
        measurement=measurement,
        verification=verification,
        # Absolute bucket URLs — the frontend renders these directly.
        frontage_url=url_before,
        composite_url=url_after,
        attempts=comp.attempt,
    )

    # Persist while the bucket paths are in scope.
    repository.record_accepted(
        db_run_id, repository.upsert_venue(venue), result, path_before, path_after
    )
    return result


def _write_trace(payload: ResultsPayload, out_dir: Path) -> None:
    """Write a copy of the run payload into the run's scratch dir for debugging.

    backend/outputs is gitignored ephemeral scratch — this is a convenience for
    inspecting a run on disk, not a served artifact. The database is the record
    the frontend reads; nothing here is published.
    """
    try:
        (out_dir / "results.json").write_text(
            json.dumps(payload.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
    except Exception as exc:  # debugging convenience must never fail a run
        log.debug("could not write trace results.json: %s", exc)


def run_pipeline(
    run_id: str | None = None,
    max_venues: int | None = None,
    target_accepted: int | None = None,
) -> ResultsPayload:
    """Run the whole thing. Blocking; the router calls it as a background task.

    max_venues / target_accepted override the config defaults for this run only.
    The router has already clamped them to the hard caps; here they simply take
    effect. Falling back to config keeps the CLI entrypoint working unchanged.
    """
    run_id = run_id or new_run_id()
    max_venues = MAX_VENUES if max_venues is None else max_venues
    target_accepted = TARGET_ACCEPTED if target_accepted is None else target_accepted

    status = _RUNS.setdefault(run_id, RunStatus(run_id=run_id))
    out_dir = run_dir(run_id)
    funnel = Funnel()

    # Fresh cost counters for this run. Clients increment them on real (uncached)
    # billable calls; we read the total at the end.
    run_metrics = metrics.start_run()

    rejections: list[Rejection] = []
    results: list[VenueResult] = []

    # Bound before the try so they are always defined for the payload below, even
    # if setup throws. Reported as-is: these are the models that actually ran,
    # whether resolved by probe or pinned in .env.
    model = ""
    vision_model = ""

    # Durable history. Optional: with Supabase unconfigured this returns None and
    # every repository call below becomes a no-op. Persistence upgrades a run; it
    # must never be able to fail one.
    db_run_id = repository.create_run(
        run_id, dry_run=DRY_RUN, max_venues=max_venues, target_accepted=target_accepted
    )

    try:
        # [0] Resolve the image model and prepare product plates once per run.
        status.stage = "setup"
        repository.update_run(db_run_id, stage="setup")
        vision_model = gemini.resolve_vision_model()
        model = gemini.resolve_image_model()
        plates = products_svc.prepare_product_plates(run_id)
        log.info(
            "vision: %s | image: %s | product plates: %d", vision_model, model, len(plates)
        )
        repository.update_run(db_run_id, vision_model=vision_model, image_model=model)

        # [1] Discover.
        status.stage = "discover"
        repository.update_run(db_run_id, stage="discover")
        candidates, discovery_rejections, all_candidates = discovery_svc.discover_venues(funnel)
        rejections.extend(discovery_rejections)
        status.rejected = len(rejections)

        # Persist the discovery filter's decisions. These are the bulk of the
        # rejection log and the clearest evidence the selection was automated.
        for rej in discovery_rejections:
            if (cand := all_candidates.get(rej.venue_id)) is not None:
                repository.record_rejected(db_run_id, repository.upsert_venue(cand), rej)

        if not candidates:
            raise RuntimeError("Discovery returned no candidates — check the Maps API key")

        # Cap what enters the paid stages. Discovery still pulled the full set,
        # so the funnel stays honest.
        shortlist = candidates[:max_venues]
        funnel.entered_pipeline = len(shortlist)
        log.info(
            "%d candidates survived discovery; %d enter the paid stages (max_venues=%d)",
            len(candidates),
            len(shortlist),
            max_venues,
        )

        # [2-6] Per venue.
        for venue in shortlist:
            if len(results) >= target_accepted:
                log.info("reached target_accepted=%d — stopping early", target_accepted)
                break

            status.stage = f"processing {venue.name}"
            repository.update_run(db_run_id, stage=status.stage)

            outcome = _process_venue(venue, plates, out_dir, run_id, model, funnel, db_run_id)
            status.processed += 1

            if isinstance(outcome, Rejection):
                rejections.append(outcome)
                status.rejected = len(rejections)
                # Persisted here rather than in _process_venue: this is the only
                # scope that still holds the VenueCandidate the row needs.
                repository.record_rejected(db_run_id, repository.upsert_venue(venue), outcome)
            else:
                results.append(outcome)
                status.accepted = len(results)

            # Push counters after every venue so the UI's poll reflects real
            # progress rather than jumping from 0 to done.
            repository.update_run(
                db_run_id,
                processed=status.processed,
                accepted=status.accepted,
                rejected=status.rejected,
            )

        funnel.accepted = len(results)
        status.stage = "done"

    except Exception as exc:
        log.exception("pipeline failed")
        status.error = str(exc)
        status.stage = "failed"

    finally:
        status.done = True
        status.finished_at = datetime.now(timezone.utc).isoformat()

    payload = ResultsPayload(
        run_id=run_id,
        dry_run=DRY_RUN,
        vision_model=vision_model,
        image_model=model,
        # Ship the bar these decisions were judged against, so the UI and
        # design.md quote the live constants rather than a stale copy.
        thresholds=Thresholds(
            frontage_bare_threshold=FRONTAGE_BARE_THRESHOLD,
            standard_door_height_m=STANDARD_DOOR_HEIGHT_M,
            scale_tolerance=SCALE_TOLERANCE,
            max_composite_attempts=MAX_COMPOSITE_ATTEMPTS,
            max_pano_distance_m=MAX_PANO_DISTANCE_M,
            people_prominence_threshold=PEOPLE_PROMINENCE_THRESHOLD,
            heading_nudge_deg=HEADING_NUDGE_DEG,
        ),
        settings=RunSettings(max_venues=max_venues, target_accepted=target_accepted),
        cost=RunCost(**run_metrics.as_dict()),
        funnel=funnel,
        venues=results,
        rejected=rejections,
        source="database",
    )
    _write_trace(payload, out_dir)
    repository.finish_run(db_run_id, payload, status.error)

    if not db_run_id:
        log.warning(
            "run %s finished but was NOT saved — the database is not configured or "
            "was unreachable. Set SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY and restart.",
            run_id,
        )
    log.info(
        "run %s complete: %d discovered -> %d accepted, %d rejected · est. $%.3f%s",
        run_id,
        funnel.discovered,
        len(results),
        len(rejections),
        run_metrics.total_cost(),
        " (saved to database)" if db_run_id else " (NOT saved — no database)",
    )
    return payload
