"""[5] composite -- put the client's real planters on the real frontage.

Why reference-conditioned generation rather than cutout-and-paste:

A cutout-and-paste of the product photo into the frontage is geometrically
honest -- the product is pixel-identical to the reference, which is the one thing
we most need -- but it looks pasted, every time. The product photos are lit from
their own direction, shot at their own focal length and camera height, and sat on
their own ground plane. Paste one onto a Street View frame and the shadow falls
the wrong way, the perspective of the vessel's rim disagrees with the perspective
of the pavement, the grain and colour temperature do not match, and the result is
unsendable to a venue owner. Fixing all of that is a relighting and perspective
problem, which is exactly the problem an image model already solves.

So we condition on the real photos and let the model re-render the product into
the scene's own light and perspective -- then we verify faithfulness afterwards
(stage 6) rather than trusting it. Generation is cheap; the verifier is the
control. That trade is only defensible *because* stage 6 exists.

Everything is cached by venue + prompt + model + attempt. Every generation is
billed, and this pipeline gets re-run many times while iterating on this prompt.
"""

from __future__ import annotations

from pathlib import Path

from app.clients import gemini
from app.config import (
    DRY_RUN,
    PRODUCTS_BY_SLUG,
    STANDARD_DOOR_HEIGHT_M,
)
from app.schemas import Capture, Composite, Measurement, Rejection, VenueCandidate
from app.services.products import load_product_images
from app.utils import cache
from app.utils.images import load_image, save_image
from app.utils.logging import get_logger, short_error

log = get_logger("composite")


def build_prompt(
    measurement: Measurement,
    capture: Capture,
    product_slug: str,
    reject_reasons: list[str] | None = None,
) -> str:
    """Assemble the composite prompt.

    Pure and deterministic: the same inputs always produce the same prompt, which
    is what makes the cache key meaningful and the run reproducible.
    """
    product = PRODUCTS_BY_SLUG[product_slug]
    n_refs = len(PRODUCTS_BY_SLUG)

    prompt = f"""You are editing a real photograph of a real business frontage to show the owner how the entrance would look with professionally installed outdoor planters.

IMAGE 1 is the frontage photograph. This is the image you are editing.
IMAGES 2-{n_refs + 1} are photographs of the EXACT physical products to be installed. They are a real manufacturer's actual products, not inspiration.

THE PRODUCT TO PLACE — this is IMAGE 2:
{product.description}

Reproduce that product faithfully. Its shape, proportions, colour, finish, material and planting must match IMAGE 2. You may re-light it to match the scene and you may show it from the angle the scene requires — but you must not redesign it. Do not substitute a generic planter. Do not change its colour or material. Do not invent a different plant scheme. If IMAGE 2 shows a matched pair, place a matched pair.

SCALE — this is the most important instruction:
The doorway in IMAGE 1 is {measurement.door_height_px:.0f} pixels tall and is a standard {STANDARD_DOOR_HEIGHT_M:.2f}m commercial door, which sets the scale of the scene at approximately {measurement.px_per_metre:.0f} pixels per metre.
The planter's vessel is {product.body_height_m:.2f}m tall in real life.
Therefore the planter's vessel — the container alone, from its base to its rim, NOT including the plants growing out of it — must be rendered approximately {measurement.expected_planter_px:.0f} pixels tall in the output image. The foliage will extend above that.
Getting this wrong is the most common failure. Measure it against the door: the vessel should stand roughly {(product.body_height_m / STANDARD_DOOR_HEIGHT_M):.0%} of the door's height.

PLACEMENT:
The ground plane at the entrance is at y = {measurement.ground_line_y:.0f} pixels from the top of the image. The planter's base must sit ON that ground line, in contact with the pavement, not floating and not sunk into it.
Place the planter on the pavement beside the entrance, against or near the facade.
Do NOT block the doorway. Do NOT stand it in the door opening. Leave a clear, walkable path through the entrance — this is a real business that real customers, including wheelchair users, must be able to walk into.
"""

    if measurement.placement_zones:
        zones = "; ".join(
            f"[{z[0]:.0f},{z[1]:.0f} to {z[2]:.0f},{z[3]:.0f}]"
            for z in measurement.placement_zones[:3]
        )
        prompt += f"Suitable clear pavement areas, best first: {zones}\n"

    prompt += f"""
LIGHT AND SHADOW:
The scene's light comes {measurement.light_direction}.
Give the planter a natural contact shadow consistent with that light and with the other shadows already in IMAGE 1. The shadow must fall in the same direction as the existing shadows in the scene, be soft or hard to the same degree, and anchor the planter to the ground. A planter with no contact shadow reads as pasted on and is unusable.

DO NOT ALTER ANYTHING ELSE — this is a photograph of a real business:
- Do not change the building, its brickwork, render, paint, or architecture.
- Do not change, move, redraw, translate or "improve" any signage, lettering, logo or house number. The business's name must read exactly as it does in IMAGE 1.
- Do not change the windows, the door, the door furniture, or what is visible through the glass.
- Do not add, remove, move or alter people, vehicles, bicycles, bins, or street furniture.
- Do not change the road, the pavement surface, the kerb, the sky, the weather, or the time of day.
- Do not restyle, colour-grade, sharpen, or beautify the photograph.
- Do not add any text, watermark, logo or caption.

The ONLY difference between IMAGE 1 and your output must be that the planter is now standing there, with its shadow. Everything else must be pixel-for-pixel the original photograph.

Output the edited photograph."""

    if reject_reasons:
        prompt += f"""

CORRECTING A PREVIOUS FAILED ATTEMPT:
Your previous attempt at this exact edit was rejected by an automated quality check for these specific reasons:
{chr(10).join(f"  - {r}" for r in reject_reasons)}
Fix every one of those faults. Keep everything else about the brief above identical."""

    return prompt


def composite_frontage(
    venue: VenueCandidate,
    capture: Capture,
    measurement: Measurement,
    product_slug: str,
    plates: dict[str, Path],
    out_dir: Path,
    run_id: str,
    model: str,
    attempt: int = 1,
    reject_reasons: list[str] | None = None,
) -> Composite | Rejection:
    """Generate one composite. Cached by venue + prompt + model + attempt."""
    prompt = build_prompt(measurement, capture, product_slug, reject_reasons)

    # Cache key per the brief: venue + prompt hash + model + attempt. Changing
    # the prompt invalidates it; changing nothing costs nothing.
    key = cache.cache_key("composite_v1", venue.id, prompt, model, attempt)
    out_path = out_dir / f"{venue.id}_after_a{attempt}.png"

    if (hit := cache.get_bytes("composite", key, ".png")) is not None:
        save_image(hit, out_path)
        log.info("%s: composite attempt %d (cached, free)", venue.name, attempt)
        return Composite(
            venue_id=venue.id,
            image_path=str(out_path),
            prompt=prompt,
            model=model,
            attempt=attempt,
            product_slug=product_slug,
            from_cache=True,
        )

    if DRY_RUN:
        log.info("%s: DRY_RUN — skipping billed image generation", venue.name)
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="composite",
            kind="error",
            reasons=["DRY_RUN enabled — no cached composite available for these inputs"],
            detail="Set DRY_RUN=false to generate. This is not a quality rejection.",
        )

    frontage = load_image(Path(capture.image_path))
    products = load_product_images(plates, product_slug)

    try:
        image_bytes = gemini.generate_image(
            model=model,
            contents=[frontage, *products, prompt],
            run_id=run_id,
            stage="composite",
            venue_id=venue.id,
            prompt_for_trace=prompt,
            attempt=attempt,
        )
    except Exception as exc:
        log.error("%s: composite attempt %d failed: %s", venue.name, attempt, exc)
        return Rejection(
            venue_id=venue.id,
            venue_name=venue.name,
            address=venue.address,
            stage="composite",
            kind="error",
            reasons=[f"Image generation failed on attempt {attempt}. {short_error(exc)}"],
            detail="Infrastructure failure, not a quality judgement.",
        )

    cache.put_bytes("composite", key, image_bytes, ".png")
    save_image(image_bytes, out_path)

    # The prompt is saved next to the image: reproducibility is a grading signal,
    # and a composite whose prompt we cannot produce is not defensible.
    (out_dir / f"{venue.id}_after_a{attempt}_prompt.txt").write_text(prompt, encoding="utf-8")

    log.info("%s: composite attempt %d generated (%.0f KB)", venue.name, attempt, len(image_bytes) / 1024)

    return Composite(
        venue_id=venue.id,
        image_path=str(out_path),
        prompt=prompt,
        model=model,
        attempt=attempt,
        product_slug=product_slug,
        from_cache=False,
    )
