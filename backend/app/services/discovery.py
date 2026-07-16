"""[1] discover -- find independent, street-facing London venues.

The brief's hardest rule applies here first: no hardcoded venue lists, no
hand-picked candidates. This module is given a set of *areas* and *categories*
and everything after that is a filter the code applies, so the venue list is an
output of the pipeline rather than an input to it.

Each filter drops candidates for a stated reason, and every drop is recorded as
a Rejection. The funnel in design.md is assembled from exactly these numbers.
"""

from __future__ import annotations

from typing import Any

from app.config import (
    CHAIN_BLOCKLIST,
    DISCOVERY_QUERY_TEMPLATES,
    INDOOR_CONTEXT_TERMS,
    LONDON_AREAS,
    MIN_USER_RATINGS,
    PLACES_PAGE_SIZE,
)
from app.clients import google_maps
from app.schemas import Funnel, Rejection, VenueCandidate
from app.utils.images import extract_postcode
from app.utils.logging import get_logger

log = get_logger("discover")


def _is_chain(name: str) -> str | None:
    """Return the matched blocklist term, or None.

    Chains are excluded because outreach needs an owner who can say yes to a
    planter; a branch manager cannot. This matches on name substring, which is
    a category rule -- not a curated list of specific venues we dislike.
    """
    lowered = name.lower()
    for term in CHAIN_BLOCKLIST:
        if term in lowered:
            return term
    return None


def _is_indoor_context(name: str, address: str) -> str | None:
    """Return the matched term if the venue looks to be inside a container.

    A unit in a food court or shopping centre has no street frontage to dress,
    so there is nothing for this product to improve.
    """
    haystack = f"{name} {address}".lower()
    for term in INDOOR_CONTEXT_TERMS:
        if term in haystack:
            return term
    return None


def _to_candidate(place: dict[str, Any], area: str) -> VenueCandidate | None:
    """Map a Places (New) result onto our model. None if it lacks essentials."""
    place_id = place.get("id")
    name = (place.get("displayName") or {}).get("text", "")
    location = place.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")

    if not place_id or not name or lat is None or lng is None:
        return None

    address = place.get("formattedAddress", "")
    photos = [p.get("name", "") for p in (place.get("photos") or []) if p.get("name")]

    return VenueCandidate(
        id=place_id,
        name=name,
        address=address,
        postcode=extract_postcode(address),
        lat=float(lat),
        lng=float(lng),
        types=place.get("types", []) or [],
        primary_type=place.get("primaryType", "") or "",
        business_status=place.get("businessStatus", "") or "",
        rating=place.get("rating"),
        user_ratings_total=place.get("userRatingCount"),
        photo_names=photos,
        area=area,
    )


def _search_all_areas() -> list[VenueCandidate]:
    """Cross every area with every category template and de-duplicate by place id.

    Venues surface under more than one query (a place can be both cafe and
    restaurant), so the raw union is deduped before any filtering runs.
    """
    seen: dict[str, VenueCandidate] = {}

    for area in LONDON_AREAS:
        for template in DISCOVERY_QUERY_TEMPLATES:
            query = template.format(area=area)
            try:
                places = google_maps.search_text(query, max_results=PLACES_PAGE_SIZE)
            except Exception as exc:  # a dead query must not kill the run
                log.warning("discovery query failed: %s -- %s", query, exc)
                continue

            for place in places:
                candidate = _to_candidate(place, area)
                if candidate and candidate.id not in seen:
                    seen[candidate.id] = candidate

    return list(seen.values())


def discover_venues(
    funnel: Funnel,
) -> tuple[list[VenueCandidate], list[Rejection], dict[str, VenueCandidate]]:
    """Find candidate venues and record why each rejected one was dropped.

    Returns (survivors, rejections, all_by_id). The third element is every raw
    candidate keyed by place id — the orchestrator needs the full record to
    persist a rejected venue, and a Rejection only carries its name and address.

    Mutates `funnel` with the stage counts that design.md quotes.
    """
    raw = _search_all_areas()
    funnel.discovered = len(raw)
    log.info("discovered %d unique candidates across %d areas", len(raw), len(LONDON_AREAS))

    rejections: list[Rejection] = []
    survivors: list[VenueCandidate] = []

    for c in raw:
        reasons: list[str] = []

        if (chain := _is_chain(c.name)) is not None:
            reasons.append(f"Chain or group brand (name matches '{chain}') — no owner to pitch to")

        if (indoor := _is_indoor_context(c.name, c.address)) is not None:
            reasons.append(f"Not street-facing (address/name indicates '{indoor}')")

        if c.business_status and c.business_status != "OPERATIONAL":
            reasons.append(f"business_status is {c.business_status}, not OPERATIONAL")

        if c.user_ratings_total is not None and c.user_ratings_total < MIN_USER_RATINGS:
            reasons.append(
                f"Only {c.user_ratings_total} reviews (< {MIN_USER_RATINGS}) — "
                "likely closed, relocated, or a ghost kitchen with no frontage"
            )

        if reasons:
            rejections.append(
                Rejection(
                    venue_id=c.id,
                    venue_name=c.name,
                    address=c.address,
                    stage="discover",
                    reasons=reasons,
                )
            )
        else:
            survivors.append(c)

    # Funnel counts, computed from the recorded reasons so they always reconcile.
    chain_drops = sum(1 for r in rejections if any("Chain" in x or "street-facing" in x for x in r.reasons))
    funnel.after_chain_filter = len(raw) - chain_drops
    funnel.after_status_filter = len(survivors)

    log.info(
        "discovery funnel: %d found -> %d after chain/indoor -> %d operational survivors",
        funnel.discovered,
        funnel.after_chain_filter,
        len(survivors),
    )

    # Best-reviewed first. This is an ordering, not a selection: it decides which
    # venues meet the expensive stages first under MAX_VENUES, and every one of
    # them still has to survive the vision gates on its own merit.
    survivors.sort(key=lambda v: (v.user_ratings_total or 0), reverse=True)
    return survivors, rejections, {c.id: c for c in raw}
