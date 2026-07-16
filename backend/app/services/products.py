"""Product reference plates -- the client's real planters, isolated.

The three supplied reference photos are lifestyle shots, not product plates.
Each one contains a whole storefront behind the planter, and planter_3 has
motion-blurred pedestrians walking across the front of it. Handing those to an
image model as "this is the product" invites it to read the background as part
of the instruction: the composite comes back with the Dutch shopfront from
planter_3 pasted onto a Shoreditch cafe, and the verifier correctly rejects it
as "building altered".

So before compositing we crop each reference down to the planter itself, using
one vision call per product. This runs once and caches -- three calls, forever.

This is product preparation, not venue curation: it is the same automatic
operation applied to whatever product photography the client supplies, and it
adds no hand-picking to the pipeline. If the crop looks implausible we keep the
full photo rather than risk cropping the product in half.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.clients import gemini
from app.config import (
    GEMINI_COORD_SCALE,
    MIN_PRODUCT_CROP_AREA_FRAC,
    PRODUCT_PLATES_DIR,
    PRODUCT_SPECS,
    PRODUCTS_BY_SLUG,
    PRODUCTS_DIR,
    ProductSpec,
)
from app.utils import cache
from app.utils.images import crop_to_bbox, load_image
from app.utils.logging import get_logger
from pydantic import BaseModel, Field

log = get_logger("products")

PLATE_PROMPT = """This is a product photograph of an outdoor planter, shot in a real location. I need to isolate the HERO planter — the single planter this photo is selling, which is the largest, sharpest, most central, foreground one.

Return a single JSON object with exactly these keys:

- "bbox" (array of 4 numbers): [x1, y1, x2, y2] on a NORMALISED 0-1000 grid where (0,0) is the top-left corner of the image and (1000,1000) is the bottom-right, tightly bounding the hero planter INCLUDING the plants growing out of it. If the product is clearly a matched pair or a set shown together in the foreground, bound the whole group.
- "confident" (boolean): true if you are confident you have found the hero product; false if the image is ambiguous or you cannot tell which planter is the subject.

Ignore: planters in the soft-focus background, planters belonging to neighbouring shops, people, and street furniture.

Return ONLY the JSON object."""


class _PlateBox(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    confident: bool = True


def _plate_path(spec: ProductSpec) -> Path:
    return PRODUCT_PLATES_DIR / f"{spec.slug}.png"


def _crop_one(spec: ProductSpec, run_id: str) -> Path:
    """Return a path to the cleanest available reference image for this product.

    Falls back to the original photo on any doubt. A wrong crop is worse than no
    crop: a planter cut off at the rim teaches the model the product is a bowl.
    """
    source = PRODUCTS_DIR / spec.filename
    if not source.exists():
        raise FileNotFoundError(
            f"Product reference {source} is missing. The three client photos must be "
            f"in {PRODUCTS_DIR}."
        )

    plate = _plate_path(spec)
    # v2: v1 plates were cropped treating Gemini's 0-1000 grid as pixels, so any
    # crop it produced is wrong. Bumping the namespace discards them rather than
    # silently serving a mis-cropped product to the compositor forever.
    key = cache.cache_key("product_plate_v2", spec.slug, source.stat().st_size)

    if plate.exists() and cache.get_json("product_plate", key) is not None:
        return plate

    image = load_image(source)

    try:
        box = gemini.generate_json(
            model=gemini.resolve_vision_model(),
            contents=[image, PLATE_PROMPT],
            schema=_PlateBox,
            run_id=run_id,
            stage="product_plate",
            venue_id=spec.slug,
            prompt_for_trace=PLATE_PROMPT,
        )
    except Exception as exc:
        log.warning("%s: plate crop failed (%s) — using full reference photo", spec.slug, exc)
        return source

    if not box.confident:
        log.info("%s: model unsure of hero product — using full reference photo", spec.slug)
        return source

    # The bbox is on Gemini's normalised 0-1000 grid, not in pixels. Cropping
    # with the raw numbers would take a box measured against a 1000x1000 frame
    # out of a 990x1426 photograph — roughly right horizontally by coincidence,
    # badly wrong vertically, and the product would come back sliced.
    w, h = image.size
    sx, sy = w / GEMINI_COORD_SCALE, h / GEMINI_COORD_SCALE
    bbox_px = [
        box.bbox[0] * sx,
        box.bbox[1] * sy,
        box.bbox[2] * sx,
        box.bbox[3] * sy,
    ]

    x1, y1, x2, y2 = bbox_px
    area_frac = (max(0.0, x2 - x1) * max(0.0, y2 - y1)) / float(w * h)

    if area_frac < MIN_PRODUCT_CROP_AREA_FRAC:
        log.info(
            "%s: proposed crop is only %.1f%% of the frame — implausible for the hero "
            "product, using full reference photo",
            spec.slug,
            area_frac * 100,
        )
        return source

    cropped: Image.Image = crop_to_bbox(image, bbox_px)
    PRODUCT_PLATES_DIR.mkdir(parents=True, exist_ok=True)
    cropped.save(plate, format="PNG")
    cache.put_json(
        "product_plate",
        key,
        {"bbox_norm": box.bbox, "bbox_px": bbox_px, "area_frac": area_frac},
    )

    log.info(
        "%s: cropped reference to %dx%d (%.0f%% of frame)",
        spec.slug,
        cropped.size[0],
        cropped.size[1],
        area_frac * 100,
    )
    return plate


def prepare_product_plates(run_id: str) -> dict[str, Path]:
    """Prepare all three product plates. Returns {slug: path}. Cached."""
    plates: dict[str, Path] = {}
    for spec in PRODUCT_SPECS:
        try:
            plates[spec.slug] = _crop_one(spec, run_id)
        except FileNotFoundError as exc:
            log.error(str(exc))
            raise
    return plates


def load_product_images(plates: dict[str, Path], primary_slug: str) -> list[Image.Image]:
    """Load the reference images for a composite call, primary first.

    All three are passed on every call -- the brief requires the model to be
    conditioned on the client's real products, and the model tier accepts up to
    14 reference images, so there is no reason to withhold two of them. Order
    matters: the primary goes first, and the prompt names it explicitly.
    """
    ordered = [primary_slug] + [s for s in PRODUCTS_BY_SLUG if s != primary_slug]
    return [load_image(plates[slug]) for slug in ordered if slug in plates]
