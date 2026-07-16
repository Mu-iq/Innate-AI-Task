"""Per-run cost accounting.

Counts the *billable* work a run actually did — cache hits are free and never
counted — and captures Gemini token usage from each response. At the end of a
run the totals are multiplied by the prices in config.py to estimate a cost that
is saved with the run and shown in the UI.

Scope is a contextvar, so the counters belong to the run currently executing and
do not bleed across runs. In this prototype runs are serialised
(MAX_CONCURRENT_RUNS = 1) so this is belt-and-braces, but it is also correct if
that ever changes.
"""

from __future__ import annotations

import contextvars
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import (
    GEMINI_IMAGE_COST_PER_IMAGE,
    GEMINI_IMAGE_INPUT_COST_PER_1M,
    GEMINI_VISION_INPUT_COST_PER_1M,
    GEMINI_VISION_OUTPUT_COST_PER_1M,
    PLACES_PHOTO_COST,
    PLACES_TEXT_SEARCH_COST,
    STREETVIEW_STATIC_COST,
)


@dataclass
class RunMetrics:
    """Billable call counts and token usage for a single run."""

    places_text_searches: int = 0
    streetview_statics: int = 0
    places_photos: int = 0
    vision_calls: int = 0
    vision_input_tokens: int = 0
    vision_output_tokens: int = 0
    image_generations: int = 0
    # The compositor is sent the frontage plus three product references, and
    # images are tokenised on input (~1,120 tokens each), so this is worth
    # counting even though the generated image dominates the bill.
    image_input_tokens: int = 0

    def cost_breakdown(self) -> dict[str, float]:
        """USD per line item, rounded. Uses the prices in config."""
        places = self.places_text_searches * PLACES_TEXT_SEARCH_COST
        streetview = self.streetview_statics * STREETVIEW_STATIC_COST
        photos = self.places_photos * PLACES_PHOTO_COST
        vision = (
            self.vision_input_tokens / 1_000_000 * GEMINI_VISION_INPUT_COST_PER_1M
            + self.vision_output_tokens / 1_000_000 * GEMINI_VISION_OUTPUT_COST_PER_1M
        )
        image = (
            self.image_generations * GEMINI_IMAGE_COST_PER_IMAGE
            + self.image_input_tokens / 1_000_000 * GEMINI_IMAGE_INPUT_COST_PER_1M
        )
        return {
            "places": round(places, 4),
            "streetview": round(streetview, 4),
            "places_photos": round(photos, 4),
            "vision": round(vision, 4),
            "image": round(image, 4),
        }

    def total_cost(self) -> float:
        return round(sum(self.cost_breakdown().values()), 4)

    def as_dict(self) -> dict[str, Any]:
        """Counts + per-line cost + total, for persisting and display."""
        return {
            "counts": asdict(self),
            "cost_usd": self.cost_breakdown(),
            "total_cost_usd": self.total_cost(),
        }


# The run currently executing. Defaults to a throwaway instance so calls made
# outside a run (a probe, a one-off script) never crash on a missing context.
_current: contextvars.ContextVar[RunMetrics] = contextvars.ContextVar(
    "run_metrics", default=RunMetrics()
)


def start_run() -> RunMetrics:
    """Begin counting for a new run. Returns the fresh metrics object."""
    m = RunMetrics()
    _current.set(m)
    return m


def current() -> RunMetrics:
    return _current.get()


# --- Increment helpers, called from the clients on a real (non-cached) call --- #


def record_places_search() -> None:
    current().places_text_searches += 1


def record_streetview_static() -> None:
    current().streetview_statics += 1


def record_places_photo() -> None:
    current().places_photos += 1


def record_vision_call(input_tokens: int = 0, output_tokens: int = 0) -> None:
    m = current()
    m.vision_calls += 1
    m.vision_input_tokens += max(0, input_tokens)
    m.vision_output_tokens += max(0, output_tokens)


def record_image_generation(input_tokens: int = 0) -> None:
    m = current()
    m.image_generations += 1
    m.image_input_tokens += max(0, input_tokens)
