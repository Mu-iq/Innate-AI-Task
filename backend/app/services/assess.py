"""[3] assess -- is this frontage worth pitching, and is this photo usable?

Two questions in one vision call, because they are answered from the same pixels:

  1. Is the photograph usable at all? (entrance visible, framing usable)
  2. Is the frontage actually bare enough that a planter improves it?

The model scores; the *code* decides. Gemini never sees FRONTAGE_BARE_THRESHOLD
and is never asked "should we accept this venue?" -- it is asked to describe what
it sees, and the accept/reject rule is applied here in Python against a named
constant. That keeps the decision boundary in the repo where it can be quoted and
argued with, instead of inside a model's judgement where it cannot.

This call also returns `frontage_palette`, which drives automatic product
selection (see config.PRODUCT_MATCH_RULES). Choosing a planter by hand would be
the manual curation the brief forbids, and the model is already looking at the
frontage, so the choice costs nothing extra.
"""

from __future__ import annotations

from pathlib import Path

from app.clients import gemini
from app.config import (
    DEFAULT_PRODUCT_SLUG,
    FRONTAGE_BARE_THRESHOLD,
    PEOPLE_PROMINENCE_THRESHOLD,
    PRODUCT_MATCH_RULES,
)
from app.schemas import Assessment, AssessmentRaw, Capture, Rejection, VenueCandidate
from app.utils.images import load_image
from app.utils.logging import get_logger, short_error

log = get_logger("assess")

ASSESS_PROMPT = f"""You are assessing a photograph of a London business frontage for an outdoor planter company. They want to send the owner a realistic visual of their own doorway dressed with planters.

Look at the image and report ONLY what you can actually see. Do not speculate about what might be out of frame.

Return a single JSON object with exactly these keys:

- "entrance_visible" (boolean): Is the venue's main pedestrian entrance/doorway clearly visible in this image? False if the image shows an interior, a close-up of food, a logo, a sign, a menu, the road, or a building with no identifiable door.

- "framing_usable" (boolean): Is the doorway AND the ground/pavement directly in front of it both in frame, sharp enough, and unobstructed enough to composite a planter onto? False if the entrance is cut off at the frame edge, severely oblique (viewed from so sharp an angle the facade is a sliver), heavily occluded, too dark to read, or if the pavement in front of the door is not visible.

- "frontage_bare_score" (integer 0-10): How BARE and under-dressed is the entrance area?
    10 = completely bare: blank pavement, no plants, no greenery, no planters, nothing but hard surface at the door.
    7-9 = essentially bare, perhaps a doormat, a sign, or a bin.
    4-6 = partially dressed: some greenery, a hanging basket, or one small pot.
    1-3 = well dressed already: multiple planters, window boxes, established greenery.
    0 = lush, nothing could be added.
  Score what is at and immediately around the ENTRANCE, not the wider street. Street trees belonging to the council are not the venue's dressing; ignore them.

- "frontage_palette" (string): The dominant colour/material of the SHOPFRONT itself (not the street, not the sky). Exactly one of:
    "dark" = black, charcoal, navy, dark green or other dark painted joinery/fascia
    "light" = white, cream, pale grey render or paintwork
    "warm_brick" = exposed red/orange/buff brick, terracotta, warm stone
    "mixed" = genuinely no dominant one of the above

- "people_prominence" (integer 0-10): How prominent are identifiable people in the frame? 0 = none. 10 = a person is a main subject, close to camera, dominating the shot. Judge prominence, not count.

- "obstructions" (array of strings): Things physically between the camera and the doorway, or standing where a planter would go. E.g. ["parked car", "scaffolding", "A-board", "wheelie bin"]. Empty array if none.

- "reject_reasons" (array of strings): Short, specific, human-readable reasons this image is NOT usable. Empty array if it is usable. Write these as if explaining to a colleague why you binned it.

Be strict. A borderline image wastes an expensive image generation downstream.
Return ONLY the JSON object."""


def _select_product(palette: str) -> str:
    """Map the observed shopfront palette to one of the client's three planters.

    Rule: contrast against the facade, because a planter that disappears into
    the shopfront sells nothing. Encoded as a table in config, applied here.
    """
    return PRODUCT_MATCH_RULES.get(palette, DEFAULT_PRODUCT_SLUG)


def assess_frontage(
    venue: VenueCandidate,
    capture: Capture,
    run_id: str,
    attempt: int = 1,
) -> Assessment | Rejection:
    """Judge a captured frontage. Returns an Assessment (accepted or not) or a
    Rejection if the call itself could not be completed.

    Note the distinction: a model verdict of "unusable" is still an Assessment
    with accepted=False -- that is a successful assessment. A Rejection here
    means we failed to get an answer at all.
    """
    image = load_image(Path(capture.image_path))

    try:
        raw: AssessmentRaw = gemini.generate_json(
            model=gemini.resolve_vision_model(),
            contents=[image, ASSESS_PROMPT],
            schema=AssessmentRaw,
            run_id=run_id,
            stage="assess",
            venue_id=venue.id,
            prompt_for_trace=ASSESS_PROMPT,
            attempt=attempt,
        )
    except Exception as exc:
        # An API failure is NOT a judgement about this venue. It is marked as an
        # error so the rejected list stays honest about what was actually decided.
        log.error("%s: assess call failed: %s", venue.name, exc)
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="assess",
            kind="error",
            reasons=[f"Vision assessment could not be completed. {short_error(exc)}"],
            detail="The venue was never assessed — this is an infrastructure failure, not a rejection.",
        )

    # --- The decision. Model scores, code decides. --------------------------
    reasons: list[str] = list(raw.reject_reasons)

    if not raw.entrance_visible:
        reasons.append("No entrance visible in the captured image")
    if not raw.framing_usable:
        reasons.append("Framing unusable — entrance and pavement not both cleanly in frame")
    if raw.frontage_bare_score < FRONTAGE_BARE_THRESHOLD:
        reasons.append(
            f"Frontage already dressed (bareness {raw.frontage_bare_score}/10 "
            f"< threshold {FRONTAGE_BARE_THRESHOLD}) — planters would not visibly improve it"
        )
    if raw.people_prominence >= PEOPLE_PROMINENCE_THRESHOLD:
        reasons.append(
            f"People prominent in frame ({raw.people_prominence}/10 "
            f">= {PEOPLE_PROMINENCE_THRESHOLD}) — UK GDPR risk and poor outreach material"
        )

    accepted = not reasons
    product_slug = _select_product(raw.frontage_palette)

    log.info(
        "%s: bare=%d/10 entrance=%s framing=%s palette=%s -> %s",
        venue.name,
        raw.frontage_bare_score,
        raw.entrance_visible,
        raw.framing_usable,
        raw.frontage_palette,
        "ACCEPT" if accepted else f"REJECT ({len(reasons)} reasons)",
    )

    return Assessment(
        **raw.model_dump(),
        accepted=accepted,
        product_slug=product_slug,
    )


def is_framing_failure(assessment: Assessment) -> bool:
    """Would a different camera angle plausibly fix this?

    Framing problems are worth one re-shoot at a nudged heading. A frontage that
    is simply not bare is not a framing problem -- no heading fixes an entrance
    that already has planters -- and re-shooting it would just spend another
    billed Street View call to reach the same conclusion.
    """
    if assessment.accepted:
        return False
    return not assessment.framing_usable or not assessment.entrance_visible
