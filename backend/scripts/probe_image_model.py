"""Find out which models this API key can actually use.

    cd backend && python -m scripts.probe_image_model

Run this FIRST, before trusting any output.

Why this exists: **listing a model is not permission to call it.** `models.list()`
on a freshly-issued key still reports `gemini-2.5-flash`, and calling it answers

    404 — "This model is no longer available to new users"

So this script does not ask what exists. It sends a real request to each
candidate and reports which ones answer. The vision probe costs a fraction of a
penny; the image probe costs one small generation (~$0.04) per candidate tried.

Paste the winners into .env as GEMINI_VISION_MODEL / GEMINI_IMAGE_MODEL to skip
the probe at runtime (the pipeline caches the same answer to disk anyway).
"""

from __future__ import annotations

import sys

from app.clients import gemini
from app.config import (
    GEMINI_IMAGE_MODEL_CANDIDATES,
    GEMINI_VISION_MODEL_CANDIDATES,
)


def _classify(exc: Exception) -> str:
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "rate limited (429) — key is quota-limited, not broken"
    if "404" in text or "NOT_FOUND" in text:
        return "gone (404) — retired for this key"
    if "503" in text:
        return "overloaded (503) — transient, try again"
    if "403" in text or "PERMISSION" in text:
        return "forbidden (403) — not enabled on this key"
    return f"{type(exc).__name__}: {text[:120]}"


def _probe_vision() -> str | None:
    print("VISION MODELS (assess / measure / verify)")
    print("-" * 68)
    winner: str | None = None

    for candidate in GEMINI_VISION_MODEL_CANDIDATES:
        print(f"  {candidate:30} ", end="", flush=True)
        try:
            gemini._generate(
                candidate,
                ['Reply with only this JSON: {"ok":true}'],
                gemini.types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.0
                ),
            )
        except Exception as exc:
            print(f"no   — {_classify(exc)}")
            continue
        print("OK")
        winner = winner or candidate

    return winner


def _probe_image() -> str | None:
    print()
    print("IMAGE MODELS (composite) — each attempt is a billed generation")
    print("-" * 68)
    winner: str | None = None

    for candidate in GEMINI_IMAGE_MODEL_CANDIDATES:
        if winner:  # stop paying once we have an answer
            print(f"  {candidate:30} skipped (already found a working model)")
            continue
        print(f"  {candidate:30} ", end="", flush=True)
        try:
            data = gemini._probe_image_generation(candidate)
        except Exception as exc:
            print(f"no   — {_classify(exc)}")
            continue
        print(f"OK   — returned {len(data) / 1024:.1f} KB")
        winner = candidate

    return winner


def main() -> int:
    print("Probing models against the configured key.")
    print("Listing is not permission — every check below is a real request.\n")

    try:
        gemini.get_client()
    except RuntimeError as exc:
        print(f"  {exc}")
        return 2

    vision = _probe_vision()
    image = _probe_image()

    print()
    print("=" * 68)
    if vision:
        print(f"  GEMINI_VISION_MODEL={vision}")
    else:
        print("  No vision model answered. The pipeline cannot assess or verify.")
    if image:
        print(f"  GEMINI_IMAGE_MODEL={image}")
        if image == GEMINI_IMAGE_MODEL_CANDIDATES[-1]:
            print(
                "\n  Note: that is the fallback (Nano Banana 1). The 3.x image models\n"
                "  are not available on this key. Record it in design.md as an\n"
                "  availability constraint, not a design choice."
            )
    else:
        print("  No image model generated. Compositing cannot run.")
        print("  Image generation is often unavailable on a free-tier key — check billing.")
    print("=" * 68)
    print("\n  Paste the lines above into .env to skip probing at runtime.")

    return 0 if (vision and image) else 1


if __name__ == "__main__":
    sys.exit(main())
