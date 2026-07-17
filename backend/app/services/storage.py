"""Image storage in the Supabase bucket.

Owns the bucket's folder layout and nothing else. Paths are built here so that
no other module has to know how objects are arranged:

    products/plates/<slug>.png                auto-cropped product references
    runs/<run_key>/<place_id>/before.jpg      the real frontage as captured
    runs/<run_key>/<place_id>/after_a<n>.png  composite, one per attempt
    runs/<run_key>/<place_id>/prompt_a<n>.txt the exact prompt that produced it

Keyed by run first, then venue. A run is the unit you inspect, compare or bin,
so "everything from run X" is a single prefix. Every attempt is kept, not just
the winning one — a rejected generation sitting next to the prompt that produced
it is the whole point of the rejection log.

We store the object PATH in the database, never the URL. Buckets get renamed and
CDNs get put in front of them; a stored URL rots. The URL is derived at read time.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.clients import supabase as supabase_client
from app.config import SUPABASE_BUCKET
from app.utils.logging import get_logger

log = get_logger("storage")

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".txt": "text/plain",
}

# Google place ids are URL-safe already, but never trust an id you did not mint
# when it is about to become a filesystem-ish path.
_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe(segment: str) -> str:
    return _UNSAFE.sub("_", segment)[:80]


# --------------------------------------------------------------------------- #
# Path construction — the single source of truth for the bucket's layout
# --------------------------------------------------------------------------- #


def venue_prefix(run_key: str, place_id: str) -> str:
    return f"runs/{_safe(run_key)}/{_safe(place_id)}"


def frontage_path(run_key: str, place_id: str) -> str:
    return f"{venue_prefix(run_key, place_id)}/before.jpg"


def composite_path(run_key: str, place_id: str, attempt: int) -> str:
    return f"{venue_prefix(run_key, place_id)}/after_a{attempt}.png"


def prompt_path(run_key: str, place_id: str, attempt: int) -> str:
    return f"{venue_prefix(run_key, place_id)}/prompt_a{attempt}.txt"


def product_plate_path(slug: str) -> str:
    return f"products/plates/{_safe(slug)}.png"


# --------------------------------------------------------------------------- #
# Upload / read
# --------------------------------------------------------------------------- #


def upload_bytes(path: str, data: bytes, content_type: str | None = None) -> str | None:
    """Upload to the bucket, overwriting. Returns the path, or None on failure.

    Never raises. A storage failure must not lose a run that has already been
    paid for — the images are still on local disk and in results.json, so we log
    and carry on.
    """
    client = supabase_client.get_client()
    if client is None:
        return None

    if content_type is None:
        content_type = _CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")

    try:
        client.storage.from_(SUPABASE_BUCKET).upload(
            path=path,
            file=data,
            file_options={
                "content-type": content_type,
                # Re-running a run key must replace, not 409.
                "upsert": "true",
                # Composites are immutable once generated; let the CDN keep them.
                "cache-control": "public, max-age=31536000",
            },
        )
    except Exception as exc:
        log.warning("upload failed for %s: %s", path, str(exc)[:160])
        return None

    return path


def upload_file(path: str, local: Path) -> str | None:
    if not local.exists():
        log.warning("cannot upload missing file: %s", local)
        return None
    return upload_bytes(path, local.read_bytes())


def delete_run_objects(run_key: str) -> int:
    """Remove every object a run wrote. Returns how many were deleted.

    Called when a run is deleted: the database row going without its images would
    leave the bucket accumulating files nothing references and nobody can reach.

    Never raises — a storage hiccup should not stop the run row being removed.
    Worst case is orphaned files, which is what we were trying to avoid, but it
    is strictly better than a half-deleted run that still shows in the history.
    """
    client = supabase_client.get_client()
    if client is None:
        return 0

    prefix = f"runs/{_safe(run_key)}"
    bucket = client.storage.from_(SUPABASE_BUCKET)
    removed = 0

    try:
        # Objects live one directory per venue, so list the venue folders and
        # then their contents. Supabase's list() is not recursive.
        for venue_dir in bucket.list(prefix) or []:
            name = venue_dir.get("name")
            if not name:
                continue
            paths = [
                f"{prefix}/{name}/{f['name']}"
                for f in (bucket.list(f"{prefix}/{name}") or [])
                if f.get("name")
            ]
            if paths:
                bucket.remove(paths)
                removed += len(paths)
    except Exception as exc:
        log.warning("could not fully clean bucket for %s: %s", run_key, str(exc)[:160])

    if removed:
        log.info("removed %d object(s) for run %s", removed, run_key)
    return removed


def public_url(path: str | None) -> str | None:
    """Public URL for an object path. None if storage is off or path is empty.

    Derived at read time rather than stored, so renaming the bucket or fronting
    it with a CDN does not require rewriting every row.
    """
    if not path:
        return None
    client = supabase_client.get_client()
    if client is None:
        return None
    try:
        return client.storage.from_(SUPABASE_BUCKET).get_public_url(path)
    except Exception as exc:
        log.warning("could not build public url for %s: %s", path, str(exc)[:120])
        return None
