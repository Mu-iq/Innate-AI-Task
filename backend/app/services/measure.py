"""[4] measure -- turn the doorway into a ruler.

A photograph has no scale. To render a 0.70m planter at the right size we need
to know how many pixels a metre is, and the only object in a shopfront photo
whose real size we can assume with a straight face is the door.

    px_per_metre      = door_height_px / STANDARD_DOOR_HEIGHT_M   (2.03m)
    expected_planter_px = px_per_metre * product.body_height_m

The model is asked ONLY for pixel observations -- where the door is, where the
ground is, where the light comes from. Every metre-denominated number is computed
here in Python. Asking a vision model "how tall is this in metres?" invites a
hallucinated number that we would then have to defend on a call; asking it "where
is the door in this image?" is a question about pixels, which is what it can
actually see.

The output feeds two places: the composite prompt (render it this big) and the
verifier (check it came back this big). Same number, both ends.
"""

from __future__ import annotations

from pathlib import Path

from app.clients import gemini
from app.config import (
    GEMINI_COORD_SCALE,
    MAX_DOOR_HEIGHT_FRAC,
    MIN_DOOR_HEIGHT_FRAC,
    PRODUCTS_BY_SLUG,
    STANDARD_DOOR_HEIGHT_M,
)
from app.schemas import Capture, Measurement, MeasurementRaw, Rejection, VenueCandidate
from app.utils.images import load_image
from app.utils.logging import get_logger, short_error

log = get_logger("measure")

MEASURE_PROMPT = """You are measuring a photograph of a shopfront so that a planter can be composited into it at the correct real-world scale.

COORDINATE SYSTEM — follow this exactly:
Report all coordinates on a NORMALISED 0-1000 grid, regardless of the image's real pixel size. The origin (0,0) is the TOP-LEFT corner. x runs 0 (left edge) to 1000 (right edge). y runs 0 (top edge) to 1000 (bottom edge). No coordinate may be below 0 or above 1000. Do not report real-world distances anywhere.

Return a single JSON object with exactly these keys:

- "door_bbox" (array of 4 numbers): [x1, y1, x2, y2] on the 0-1000 grid, bounding the venue's main pedestrian ENTRANCE — the door opening itself, from the very top of the door frame down to where the door meets the ground/threshold. Measure the DOOR, not the whole shopfront, not the window, and not the fascia sign above it. If a glazed transom light sits above the door, exclude it: measure to the top of the door leaf.

- "door_height_px" (number): The vertical extent of that door on the 0-1000 grid, i.e. y2 - y1. This is the single most important number in this response — the entire composite is scaled from it. Be precise.

- "ground_line_y" (number): The y coordinate (0-1000) of the ground plane at the base of the door, where the pavement meets the building's facade at the entrance. This is where a planter's base would sit.

- "light_direction" (string): Where the dominant light in the scene comes from, judged from existing shadows and the bright side of objects. Plain language, e.g. "from upper left", "from the right, low", "flat overcast, no strong direction".

- "placement_zones" (array of [x1,y1,x2,y2] arrays): One to three rectangles on the 0-1000 grid marking PAVEMENT where a planter could plausibly stand: flat, clear, in front of the facade, beside the door but NOT blocking the door opening or a wheelchair path through it. Best first. Empty array if there is genuinely nowhere.

Return ONLY the JSON object."""


def measure_frontage(
    venue: VenueCandidate,
    capture: Capture,
    product_slug: str,
    run_id: str,
    attempt: int = 1,
) -> Measurement | Rejection:
    """Derive the scale anchor for this frontage."""
    image = load_image(Path(capture.image_path))
    img_w, img_h = capture.width, capture.height

    try:
        raw: MeasurementRaw = gemini.generate_json(
            model=gemini.resolve_vision_model(),
            contents=[image, MEASURE_PROMPT],
            schema=MeasurementRaw,
            run_id=run_id,
            stage="measure",
            venue_id=venue.id,
            prompt_for_trace=MEASURE_PROMPT,
            attempt=attempt,
        )
    except Exception as exc:
        log.error("%s: measure call failed: %s", venue.name, exc)
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="measure",
            kind="error",
            reasons=[f"Scale measurement could not be completed. {short_error(exc)}"],
            detail="Infrastructure failure, not a judgement about this venue.",
        )

    # --- Sanity-check the measurement, in the space it was reported in ------
    #
    # The model answers on a 0-1000 grid (see config.GEMINI_COORD_SCALE), so the
    # checks run there too. Doing them after conversion would be checking a
    # number against the very dimension used to produce it.
    #
    # A bad door height silently corrupts every downstream number: the composite
    # prompt asks for the wrong size, and the verifier then checks against that
    # same wrong size and happily agrees. Caught here or not at all.
    door_frac = raw.door_height_px / GEMINI_COORD_SCALE

    reasons: list[str] = []
    if raw.door_height_px <= 0:
        reasons.append(f"Non-positive door height ({raw.door_height_px})")
    elif door_frac < MIN_DOOR_HEIGHT_FRAC:
        reasons.append(
            f"Measured door is only {door_frac:.0%} of image height "
            f"(min {MIN_DOOR_HEIGHT_FRAC:.0%}) — venue is too far away or the model "
            "measured something that is not the door"
        )
    elif door_frac > MAX_DOOR_HEIGHT_FRAC:
        reasons.append(
            f"Measured door is {door_frac:.0%} of image height "
            f"(max {MAX_DOOR_HEIGHT_FRAC:.0%}) — the model has measured the whole "
            "shopfront rather than the door opening"
        )

    if not (0 <= raw.ground_line_y <= GEMINI_COORD_SCALE):
        reasons.append(
            f"Ground line y={raw.ground_line_y} is off the 0-{GEMINI_COORD_SCALE:.0f} grid"
        )

    if reasons:
        log.warning("%s: measurement rejected — %s", venue.name, reasons[0])
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="measure",
            reasons=reasons,
            detail=f"Raw measurement (0-1000 grid): {raw.model_dump()}",
        )

    # --- Convert the 0-1000 grid to this image's real pixels ----------------
    sx = img_w / GEMINI_COORD_SCALE
    sy = img_h / GEMINI_COORD_SCALE

    door_height_px = raw.door_height_px * sy
    ground_line_y = raw.ground_line_y * sy
    door_bbox = [
        raw.door_bbox[0] * sx,
        raw.door_bbox[1] * sy,
        raw.door_bbox[2] * sx,
        raw.door_bbox[3] * sy,
    ]
    placement_zones = [
        [z[0] * sx, z[1] * sy, z[2] * sx, z[3] * sy]
        for z in raw.placement_zones
        if len(z) == 4
    ]

    # --- Our arithmetic, not the model's ------------------------------------
    product = PRODUCTS_BY_SLUG[product_slug]
    px_per_metre = door_height_px / STANDARD_DOOR_HEIGHT_M
    expected_planter_px = px_per_metre * product.body_height_m

    log.info(
        "%s: door %.0f/1000 -> %.0fpx of %dpx -> %.1f px/m -> %s should render %.0fpx",
        venue.name,
        raw.door_height_px,
        door_height_px,
        img_h,
        px_per_metre,
        product_slug,
        expected_planter_px,
    )

    return Measurement(
        door_bbox=[round(v, 1) for v in door_bbox],
        door_height_px=round(door_height_px, 1),
        ground_line_y=round(ground_line_y, 1),
        light_direction=raw.light_direction,
        placement_zones=[[round(v, 1) for v in z] for z in placement_zones],
        px_per_metre=round(px_per_metre, 2),
        expected_planter_px=round(expected_planter_px, 1),
        product_slug=product_slug,
        image_width=img_w,
        image_height=img_h,
    )
