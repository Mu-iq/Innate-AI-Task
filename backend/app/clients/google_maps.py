"""Google Maps Platform client: Places (New) and Street View.

One place for every Maps HTTP call. Everything is timeout-bounded, retried a
fixed number of times, and cached to disk by a deterministic key.

Two things worth knowing:

* We use the **Places API (New)** v1 surface (`places:searchText`). The legacy
  `maps.googleapis.com/maps/api/place/*` endpoints are not enabled on keys
  issued to new customers, so building against them would produce a pipeline
  that works on nobody's key but a legacy one.
* The Street View **metadata** endpoint is free and unmetered. Calling it before
  the billed Static endpoint means we never pay for a venue with no coverage,
  and it is the only way to learn where the camera actually stands -- which is
  what the heading calculation needs.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import (
    HTTP_TIMEOUT_S,
    MAX_HTTP_RETRIES,
    PLACES_PAGE_SIZE,
    PLACES_PHOTO_MAX_WIDTH_PX,
    RETRY_BACKOFF_BASE_S,
    RETRY_BACKOFF_MAX_S,
    STREETVIEW_FOV,
    STREETVIEW_PITCH,
    STREETVIEW_SIZE,
    STREETVIEW_SOURCE,
    require_maps_key,
)
from app.utils import cache, metrics
from app.utils.logging import get_logger

log = get_logger("clients.maps")

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_PHOTO_URL = "https://places.googleapis.com/v1/{photo_name}/media"
STREETVIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
STREETVIEW_STATIC_URL = "https://maps.googleapis.com/maps/api/streetview"

# Ask only for what we use. Places (New) bills by field mask tier, so requesting
# the whole object would cost more per call for data we ignore.
PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.businessStatus",
        "places.rating",
        "places.userRatingCount",
        "places.photos",
    ]
)

# Retry only on transport errors and 5xx. A 4xx means the request is wrong and
# retrying it just spends the quota again to be told so a second time.
_RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)

_retry_http = retry(
    stop=stop_after_attempt(MAX_HTTP_RETRIES),
    wait=wait_exponential(multiplier=RETRY_BACKOFF_BASE_S, max=RETRY_BACKOFF_MAX_S),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)


def _raise_for_retryable_status(resp: httpx.Response) -> None:
    """Raise only on 5xx so tenacity retries those and leaves 4xx alone."""
    if resp.status_code >= 500:
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Places (New)
# --------------------------------------------------------------------------- #


@_retry_http
def _post_places_search(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": require_maps_key(),
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        resp = client.post(PLACES_SEARCH_URL, json=payload, headers=headers)
        _raise_for_retryable_status(resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Places searchText failed [{resp.status_code}]: {resp.text[:400]}"
            )
        return resp.json()


def search_text(query: str, max_results: int = PLACES_PAGE_SIZE) -> list[dict[str, Any]]:
    """Places (New) Text Search. Cached by query + result cap.

    Returns the raw `places` array; interpreting it is discovery's job, not the
    client's.
    """
    key = cache.cache_key("places_search_v1", query, max_results)
    if (hit := cache.get_json("places", key)) is not None:
        log.info("places search (cached): %s -> %d", query, len(hit))
        return hit

    payload = {
        "textQuery": query,
        "maxResultCount": min(max_results, 20),
        "languageCode": "en",
        "regionCode": "GB",
    }
    data = _post_places_search(payload)
    places = data.get("places", []) or []

    cache.put_json("places", key, places)
    metrics.record_places_search()  # a real billed call, not a cache hit
    log.info("places search: %s -> %d", query, len(places))
    return places


@_retry_http
def _get_photo_bytes(photo_name: str, max_width: int) -> bytes:
    url = PLACES_PHOTO_URL.format(photo_name=photo_name)
    params = {"maxWidthPx": max_width, "key": require_maps_key()}
    # The media endpoint 302s to the actual image host.
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        resp = client.get(url, params=params)
        _raise_for_retryable_status(resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Places photo failed [{resp.status_code}]: {resp.text[:200]}"
            )
        return resp.content


def get_place_photo(photo_name: str, max_width: int = PLACES_PHOTO_MAX_WIDTH_PX) -> bytes:
    """Fetch a Places photo. This is the fallback when Street View has no usable
    coverage. `photo_name` is the full `places/{id}/photos/{ref}` resource name."""
    key = cache.cache_key("places_photo_v1", photo_name, max_width)
    if (hit := cache.get_bytes("places_photo", key, ".jpg")) is not None:
        log.info("places photo (cached): %s", photo_name[:40])
        return hit

    data = _get_photo_bytes(photo_name, max_width)
    cache.put_bytes("places_photo", key, data, ".jpg")
    metrics.record_places_photo()
    return data


# --------------------------------------------------------------------------- #
# Street View
# --------------------------------------------------------------------------- #


@_retry_http
def _get_streetview_metadata(lat: float, lng: float) -> dict[str, Any]:
    params = {
        "location": f"{lat},{lng}",
        "source": STREETVIEW_SOURCE,
        "key": require_maps_key(),
    }
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        resp = client.get(STREETVIEW_METADATA_URL, params=params)
        _raise_for_retryable_status(resp)
        return resp.json()


def streetview_metadata(lat: float, lng: float) -> dict[str, Any]:
    """Free coverage check. Returns the raw metadata dict.

    `status` is one of OK / ZERO_RESULTS / NOT_FOUND / OVER_QUERY_LIMIT /
    REQUEST_DENIED. On OK, `location` is the panorama camera's true position --
    which is NOT the venue's position, and is the whole point of calling this
    before computing a heading.
    """
    key = cache.cache_key("sv_meta_v1", round(lat, 6), round(lng, 6), STREETVIEW_SOURCE)
    if (hit := cache.get_json("streetview_meta", key)) is not None:
        return hit

    data = _get_streetview_metadata(lat, lng)
    cache.put_json("streetview_meta", key, data)
    return data


@_retry_http
def _get_streetview_image(params: dict[str, Any]) -> bytes:
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        resp = client.get(STREETVIEW_STATIC_URL, params=params)
        _raise_for_retryable_status(resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Street View static failed [{resp.status_code}]: {resp.text[:200]}"
            )
        return resp.content


def streetview_image(
    pano_id: str,
    heading: float,
    fov: int = STREETVIEW_FOV,
    pitch: int = STREETVIEW_PITCH,
    size: str = STREETVIEW_SIZE,
) -> bytes:
    """Billed Static Street View fetch, addressed by panorama ID.

    We pass `pano` rather than `location` deliberately. The heading was computed
    as the bearing from *this specific panorama's* camera position to the venue;
    requesting by location would let Google pick a different (possibly nearer)
    panorama, and the heading would then be measured from the wrong origin --
    aiming a correctly-calculated bearing from the wrong place.

    `return_error_code=true` turns the "no imagery" case into a real HTTP error
    instead of a 200 with a grey tile.
    """
    key = cache.cache_key("sv_img_v1", pano_id, round(heading, 2), fov, pitch, size)
    if (hit := cache.get_bytes("streetview_img", key, ".jpg")) is not None:
        log.info("street view (cached): pano=%s heading=%.1f", pano_id[:12], heading)
        return hit

    params = {
        "size": size,
        "pano": pano_id,
        "heading": f"{heading:.2f}",
        "fov": fov,
        "pitch": pitch,
        "return_error_code": "true",
        "key": require_maps_key(),
    }
    data = _get_streetview_image(params)
    cache.put_bytes("streetview_img", key, data, ".jpg")
    metrics.record_streetview_static()
    log.info("street view: pano=%s heading=%.1f fov=%d", pano_id[:12], heading, fov)
    return data
