"""[2] capture -- get a real photograph of a real entrance.

The naive version of this stage requests a Street View image at the venue's
lat/lng with a guessed heading and hopes. That produces a confident photograph
of the road, the sky, or the shop opposite, and nothing downstream can tell,
because the wrong shopfront is still a shopfront.

What we do instead:

  1. Call the FREE Street View metadata endpoint. It says whether coverage
     exists at all, and -- the important part -- where the panorama camera
     actually stands, which is never the venue's own coordinates.
  2. Compute the heading as the great-circle bearing FROM that camera position
     TO the venue. This is the only way the camera looks at the right building.
  3. Reject the panorama if it is further than MAX_PANO_DISTANCE_M from the
     venue, where the frontage is too small and too oblique to composite onto.
  4. Fall back to the venue's own Places photos if any of that fails.

The fallback chain is the honest answer to "what happens when the imagery faces
the wrong way": we do not try to salvage it, we change source.

This module is also the swap point for the imagery-rights position in design.md.
Everything downstream consumes a `Capture` and knows nothing about where the
pixels came from, so replacing Street View with licensed or first-party imagery
means reimplementing this one function.
"""

from __future__ import annotations

from pathlib import Path

from app.clients import google_maps
from app.config import (
    MAX_PANO_DISTANCE_M,
    STREETVIEW_FOV,
    STREETVIEW_PITCH,
)
from app.schemas import Capture, Rejection, VenueCandidate
from app.utils.geo import haversine_m, initial_bearing
from app.utils.images import image_size, is_blank_or_placeholder, save_image
from app.utils.logging import get_logger

log = get_logger("capture")


def _capture_streetview(
    venue: VenueCandidate,
    out_dir: Path,
    heading_nudge: float = 0.0,
    attempt: int = 1,
) -> Capture | list[str]:
    """Try Street View. Returns a Capture, or a list of reasons it was not usable."""
    meta = google_maps.streetview_metadata(venue.lat, venue.lng)
    status = meta.get("status", "UNKNOWN")

    if status != "OK":
        return [f"Street View metadata returned {status} — no panorama at this location"]

    pano_loc = meta.get("location") or {}
    pano_lat, pano_lng = pano_loc.get("lat"), pano_loc.get("lng")
    pano_id = meta.get("pano_id")

    if pano_lat is None or pano_lng is None or not pano_id:
        return ["Street View metadata returned OK but no panorama position or id"]

    # How far is the camera from the shopfront?
    distance = haversine_m(float(pano_lat), float(pano_lng), venue.lat, venue.lng)
    if distance > MAX_PANO_DISTANCE_M:
        return [
            f"Nearest panorama is {distance:.1f}m away (limit {MAX_PANO_DISTANCE_M:.0f}m) — "
            "frontage would be too small and too oblique to composite onto"
        ]

    # The heading that makes this stage work: camera -> venue, not venue -> camera.
    heading = initial_bearing(float(pano_lat), float(pano_lng), venue.lat, venue.lng)
    if heading_nudge:
        from app.utils.geo import nudge_heading

        heading = nudge_heading(heading, heading_nudge)

    try:
        data = google_maps.streetview_image(
            pano_id=pano_id, heading=heading, fov=STREETVIEW_FOV, pitch=STREETVIEW_PITCH
        )
    except Exception as exc:
        return [f"Street View static request failed: {exc}"]

    if is_blank_or_placeholder(data):
        return ["Street View returned a blank or 'no imagery' tile"]

    path = out_dir / f"{venue.id}_before.jpg"
    save_image(data, path)
    w, h = image_size(data)

    return Capture(
        venue_id=venue.id,
        image_path=str(path),
        image_source="streetview",
        width=w,
        height=h,
        heading_used=round(heading, 2),
        pano_id=pano_id,
        pano_lat=float(pano_lat),
        pano_lng=float(pano_lng),
        pano_distance_m=round(distance, 1),
        pano_date=meta.get("date"),
        fov=STREETVIEW_FOV,
        pitch=STREETVIEW_PITCH,
        heading_nudge_applied=heading_nudge or None,
        attempt=attempt,
    )


def _capture_places_photo(
    venue: VenueCandidate,
    out_dir: Path,
    attempt: int = 1,
) -> Capture | list[str]:
    """Fall back to the venue's own Google Business photography.

    Weaker than Street View for our purposes -- these are frequently interiors,
    food shots or logos rather than the doorway -- but it is real imagery of the
    real venue, and the assess stage will throw it out if it does not show an
    entrance. That is exactly the division of labour we want: capture supplies
    candidates, assess decides usability. We never assume a fallback is good.
    """
    if not venue.photo_names:
        return ["No Street View coverage and no Places photos available"]

    reasons: list[str] = []
    # Try the first few; Places orders them roughly by usefulness.
    for photo_name in venue.photo_names[:3]:
        try:
            data = google_maps.get_place_photo(photo_name)
        except Exception as exc:
            reasons.append(f"Places photo fetch failed: {exc}")
            continue

        if is_blank_or_placeholder(data):
            reasons.append("Places photo was blank")
            continue

        path = out_dir / f"{venue.id}_before.jpg"
        save_image(data, path)
        w, h = image_size(data)

        return Capture(
            venue_id=venue.id,
            image_path=str(path),
            image_source="places_photo",
            width=w,
            height=h,
            attempt=attempt,
        )

    return reasons or ["All Places photos were unusable"]


def capture_from_places_photos(
    venue: VenueCandidate,
    out_dir: Path,
    attempt: int = 1,
) -> Capture | Rejection:
    """Capture from the venue's own Google Business photos, deliberately.

    Distinct from the fallback inside capture_frontage(), which only fires when
    Street View has no coverage at all. This one is for the other half of the
    brief's instruction: "if Street View coverage is poor OR FACES THE WRONG WAY,
    fall back to another source".

    A panorama can exist, be close, and still be useless -- a parked van across
    the pavement, a lamppost through the doorway, a survey car that drove past at
    a raking angle. No heading fixes any of those, because the obstruction is in
    the world, not in the framing. The venue's own photos usually are the
    frontage, shot deliberately, at eye level, on a clear day.

    They are still only candidates: assess decides, exactly as it does for Street
    View. We never assume a fallback is good.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _capture_places_photo(venue, out_dir, attempt)

    if isinstance(result, Capture):
        log.info("%s: trying the venue's own Places photo instead", venue.name)
        return result

    return Rejection(
        venue_id=venue.id,
        venue_name=venue.name,
        address=venue.address,
        stage="capture",
        reasons=result,
    )


def capture_frontage(
    venue: VenueCandidate,
    out_dir: Path,
    heading_nudge: float = 0.0,
    attempt: int = 1,
) -> Capture | Rejection:
    """Capture this venue's frontage, Street View first, Places photos second.

    `heading_nudge` re-shoots an existing panorama at an offset heading; it is
    passed by the orchestrator when assess rejected the first framing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    sv_result = _capture_streetview(venue, out_dir, heading_nudge, attempt)
    if isinstance(sv_result, Capture):
        log.info(
            "%s: street view @ heading %.1f (pano %.1fm away)%s",
            venue.name,
            sv_result.heading_used or 0.0,
            sv_result.pano_distance_m or 0.0,
            f" [nudged {heading_nudge:+.0f}deg]" if heading_nudge else "",
        )
        return sv_result

    sv_reasons = sv_result

    # A nudge only makes sense on a panorama. If Street View is unusable, the
    # nudge is meaningless and we go straight to the fallback source.
    pp_result = _capture_places_photo(venue, out_dir, attempt)
    if isinstance(pp_result, Capture):
        log.info("%s: fell back to Places photo (%s)", venue.name, sv_reasons[0])
        return pp_result

    log.warning("%s: no usable imagery from any source", venue.name)
    return Rejection(
        venue_id=venue.id,
        venue_name=venue.name,
        address=venue.address,
        stage="capture",
        reasons=sv_reasons + pp_result,
        detail="Both the Street View and Places Photos capture paths failed.",
    )
