"""[6] verify -- decide whether this generation may ever reach a venue owner.

This stage is what makes stage 5's design defensible. We let an image model
re-render the client's product into a photograph of someone's real business,
which is a genuinely risky thing to do: the model can quietly redesign the
planter, rewrite the shop's signage, or stand a 2-metre pot in the doorway. None
of that is detectable from the composite alone, which is why this call receives
BOTH images and is asked to diff them.

Two rules shape the design:

1. **The model reports; the code decides.** Gemini is asked what it observes and
   for a verdict, but the returned verdict is not what we act on. We recompute
   the decision here from named constants, and we do our own arithmetic on scale
   rather than accepting "scale_plausible: true".

2. **Ambiguity rejects.** If the verifier cannot be gotten to answer, the venue
   fails. The asymmetry is deliberate: an unsent good image costs one lead, a
   sent bad image costs the client's credibility with a business they wanted to
   sell to.
"""

from __future__ import annotations

from pathlib import Path

from app.clients import gemini
from app.config import (
    GEMINI_COORD_SCALE,
    PRODUCTS_BY_SLUG,
    SCALE_TOLERANCE,
    STANDARD_DOOR_HEIGHT_M,
)
from app.schemas import (
    Composite,
    Measurement,
    Rejection,
    Verification,
    VerificationRaw,
    VenueCandidate,
)
from app.utils.images import load_image
from app.utils.logging import get_logger, short_error

log = get_logger("verify")


def _expected_norm(measurement: Measurement) -> float:
    """The expected vessel height on Gemini's 0-1000 grid.

    Scale is checked as a FRACTION of image height rather than in absolute
    pixels, because the image model may hand back a composite at a different
    resolution than the frontage it was given. Comparing a 0-1000 reading of the
    composite against a pixel measurement of the frontage would then report a
    scale error that is really just a resize.
    """
    if measurement.image_height <= 0:
        return 0.0
    return measurement.expected_planter_px / measurement.image_height * GEMINI_COORD_SCALE


def build_verify_prompt(measurement: Measurement, product_slug: str) -> str:
    product = PRODUCTS_BY_SLUG[product_slug]
    return f"""You are the final quality gate before a marketing image is sent to a real business owner. You are strict. If this image is wrong, a real business receives a doctored photograph of their own shop, and the sender loses the deal.

IMAGE 1 is the ORIGINAL photograph of the business.
IMAGE 2 is an EDITED version which should differ from IMAGE 1 in exactly one respect: a planter has been added, with its shadow.
IMAGE 3 is the reference photograph of the EXACT product that was supposed to be added.

Compare IMAGE 1 and IMAGE 2 closely and report what you find.

Return a single JSON object with exactly these keys:

- "building_unaltered" (boolean): true ONLY if the building, signage, lettering, house number, windows, door, glazing, brickwork, paintwork, road, pavement, sky, people, and vehicles are all unchanged between IMAGE 1 and IMAGE 2. Read the shop's SIGN in both images letter by letter — image models frequently rewrite or garble text, and a business whose name has been altered must never receive this. Set false if ANYTHING other than the added planter and its shadow has changed.

- "product_faithful_to_reference" (boolean): true if the planter in IMAGE 2 is recognisably the SAME product as IMAGE 3 — same shape, same proportions, same colour, same material, same finish, same kind of planting. Re-lighting and a different viewing angle are fine and expected. A different shape, a different colour, a different material, or a generic planter that is merely similar is false.

- "scale_plausible" (boolean): Does the planter look like a real object of the right size standing on that pavement, judged against the door and any people or vehicles in shot?

- "observed_planter_height_px" (number): The height of the planter's VESSEL in IMAGE 2 — the container alone, from its base to its rim, NOT including the plants growing out of it — expressed on a NORMALISED 0-1000 scale where 0 is the top edge of IMAGE 2 and 1000 is its bottom edge. So a vessel occupying a fifth of the image height is 200. Measure carefully; this is checked arithmetically. If there is more than one planter, measure the largest.

- "grounded_with_shadow" (boolean): true if the planter has a believable contact shadow that anchors it to the pavement AND that shadow falls in the same direction as other shadows in the scene. False if it has no shadow, floats, is sunk into the ground, or casts a shadow contradicting the scene's light.

- "planter_blocks_entrance" (boolean): true if the planter stands in, overlaps, or obstructs the door opening or the walkable path through it. A planter blocking the door is unusable regardless of how good it looks.

- "verdict" (string): "accept" or "reject".

- "reject_reasons" (array of strings): Specific, concrete faults. Empty if accepting. Write them as instructions the image generator could act on to fix the image.

For reference, the planter's vessel was intended to stand about {_expected_norm(measurement):.0f} on that same 0-1000 scale — it is a {product.body_height_m:.2f}m vessel, and the doorway in this scene is a {STANDARD_DOOR_HEIGHT_M:.2f}m door occupying {(measurement.door_height_px / measurement.image_height * 1000):.0f} of the same scale. Report what you actually observe rather than what was intended.

Return ONLY the JSON object."""


def verify_composite(
    venue: VenueCandidate,
    frontage_path: Path,
    composite: Composite,
    measurement: Measurement,
    product_plate: Path,
    run_id: str,
) -> Verification | Rejection:
    """Diff the composite against the original. Returns a code-owned verdict."""
    prompt = build_verify_prompt(measurement, composite.product_slug)

    before = load_image(frontage_path)
    after = load_image(Path(composite.image_path))
    reference = load_image(product_plate)

    try:
        raw: VerificationRaw = gemini.generate_json(
            model=gemini.resolve_vision_model(),
            contents=[before, after, reference, prompt],
            schema=VerificationRaw,
            run_id=run_id,
            stage="verify",
            venue_id=venue.id,
            prompt_for_trace=prompt,
            attempt=composite.attempt,
        )
    except Exception as exc:
        # Ambiguity rejects. We cannot confirm this image is safe to send, so
        # it is not safe to send.
        log.error("%s: verification call failed: %s", venue.name, exc)
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="verify",
            kind="error",
            reasons=[
                f"Verification could not be completed, so the composite cannot be "
                f"cleared to send. {short_error(exc)}"
            ],
            detail=(
                "Ambiguity rejects: an image we cannot confirm is safe is not safe. "
                "This is an infrastructure failure, not a quality judgement."
            ),
        )

    # --- Our own scale arithmetic. We do not take the model's word for it. ---
    #
    # Both sides are on the 0-1000 grid: the model reports the observed vessel as
    # a fraction of the composite's height, and _expected_norm() puts our
    # stage-4 expectation on the same scale. That makes the comparison immune to
    # the image model returning a different resolution than it was given.
    scale_ratio: float | None = None
    scale_within_tolerance: bool | None = None

    expected_norm = _expected_norm(measurement)
    if raw.observed_planter_height_px and expected_norm > 0:
        scale_ratio = raw.observed_planter_height_px / expected_norm
        scale_within_tolerance = abs(scale_ratio - 1.0) <= SCALE_TOLERANCE

    # --- The decision, recomputed from constants ----------------------------
    reasons: list[str] = list(raw.reject_reasons)

    if not raw.building_unaltered:
        reasons.append("Building, signage or street was altered — only the planter may be added")
    if not raw.product_faithful_to_reference:
        reasons.append("Rendered planter has drifted from the client's actual product")
    if raw.planter_blocks_entrance:
        reasons.append("Planter obstructs the entrance or the walkable path through it")
    if not raw.grounded_with_shadow:
        reasons.append("Planter is not convincingly grounded — shadow missing or inconsistent with the scene")
    if not raw.scale_plausible:
        reasons.append("Planter does not read as a real object of the right size in the scene")

    if scale_within_tolerance is False and scale_ratio is not None:
        reasons.append(
            f"Planter rendered at {scale_ratio:.0%} of the expected size "
            f"(observed {raw.observed_planter_height_px:.0f} vs {expected_norm:.0f} expected "
            f"on a 0-1000 scale, tolerance ±{SCALE_TOLERANCE:.0%})"
        )

    verdict = "accept" if not reasons else "reject"

    if verdict != raw.verdict:
        # Worth logging: the model's own verdict disagreed with the rules. Almost
        # always the model being lenient about its own generation.
        log.info(
            "%s: model said '%s', rules say '%s' — rules win",
            venue.name,
            raw.verdict,
            verdict,
        )

    log.info(
        "%s: verify attempt %d -> %s%s",
        venue.name,
        composite.attempt,
        verdict.upper(),
        f" ({reasons[0]})" if reasons else "",
    )

    return Verification(
        **{**raw.model_dump(), "verdict": verdict, "reject_reasons": reasons},
        scale_ratio=round(scale_ratio, 3) if scale_ratio is not None else None,
        scale_within_tolerance=scale_within_tolerance,
    )
