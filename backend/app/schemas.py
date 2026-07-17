"""Pydantic models for every boundary in the pipeline.

Two rules hold throughout:

1. Each stage returns either its result model or a `Rejection`. A stage never
   returns `None` to mean "no good" -- a rejection always carries its reasons,
   because the rejection log is a deliverable, not debug output.
2. The `*Raw` models are what we ask Gemini to emit. They are parsed defensively
   and then enriched into the models the rest of the pipeline uses, so a vision
   model can never hand a derived number (like px_per_metre) straight through.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ImageSource = Literal["streetview", "places_photo"]
Verdict = Literal["accept", "reject"]
Stage = Literal["discover", "capture", "assess", "measure", "composite", "verify"]
FrontagePalette = Literal["dark", "light", "warm_brick", "mixed"]

# Why a candidate did not make it.
#
#   "decision" — the pipeline looked and said no. A chain brand, a frontage that
#                is already dressed, a composite that failed verification. These
#                are the deliverable: they are the evidence that selection was
#                automated.
#   "error"    — the pipeline never got to decide. A quota 429, a retired model,
#                a network failure. These are operational noise.
#
# They are kept apart deliberately. Counting a rate-limit error as a "rejection"
# would inflate the funnel with judgements that were never made, which is exactly
# the kind of quiet dishonesty this whole log exists to prevent.
RejectionKind = Literal["decision", "error"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Rejection — the deliverable
# --------------------------------------------------------------------------- #


class Rejection(BaseModel):
    """Why a candidate never reached a venue owner. Persisted, always."""

    venue_id: str
    venue_name: str
    address: str = ""
    stage: Stage
    kind: RejectionKind = "decision"
    reasons: list[str] = Field(default_factory=list)
    detail: str = ""
    at: str = Field(default_factory=_utc_now)


# --------------------------------------------------------------------------- #
# [1] discover
# --------------------------------------------------------------------------- #


class VenueCandidate(BaseModel):
    """An independent, street-facing London venue that survived discovery filters."""

    id: str
    name: str
    address: str
    postcode: str = ""
    lat: float
    lng: float
    types: list[str] = Field(default_factory=list)
    primary_type: str = ""
    business_status: str = ""
    rating: float | None = None
    user_ratings_total: int | None = None
    photo_names: list[str] = Field(default_factory=list)
    area: str = ""  # which LONDON_AREAS query surfaced it


# --------------------------------------------------------------------------- #
# [2] capture
# --------------------------------------------------------------------------- #


class Capture(BaseModel):
    """A real photograph of a real entrance, plus how we came to point the camera."""

    venue_id: str
    image_path: str
    image_source: ImageSource
    width: int
    height: int

    # Street View only. Null on the Places Photos fallback path.
    heading_used: float | None = None
    pano_id: str | None = None
    pano_lat: float | None = None
    pano_lng: float | None = None
    pano_distance_m: float | None = None
    pano_date: str | None = None
    fov: int | None = None
    pitch: int | None = None

    # Set when this capture is a re-shoot after a framing rejection.
    heading_nudge_applied: float | None = None
    attempt: int = 1


# --------------------------------------------------------------------------- #
# [3] assess
# --------------------------------------------------------------------------- #


class AssessmentRaw(BaseModel):
    """Exactly what we ask the vision model for. Nothing derived."""

    entrance_visible: bool
    frontage_bare_score: int = Field(ge=0, le=10)
    framing_usable: bool
    frontage_palette: FrontagePalette = "mixed"
    people_prominence: int = Field(default=0, ge=0, le=10)
    obstructions: list[str] = Field(default_factory=list)
    reject_reasons: list[str] = Field(default_factory=list)

    @field_validator("frontage_bare_score", "people_prominence", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> object:
        """Models occasionally emit 8.5 or "8" for an integer score."""
        if isinstance(v, (int, float)):
            return max(0, min(10, round(float(v))))
        if isinstance(v, str):
            try:
                return max(0, min(10, round(float(v.strip()))))
            except ValueError:
                return v
        return v


class Assessment(AssessmentRaw):
    """The raw judgement plus the decision our code made from it."""

    accepted: bool
    product_slug: str  # chosen by PRODUCT_MATCH_RULES from frontage_palette


# --------------------------------------------------------------------------- #
# [4] measure
# --------------------------------------------------------------------------- #


class MeasurementRaw(BaseModel):
    """The scale anchor exactly as the vision model reports it.

    IMPORTANT: every coordinate here is on Gemini's normalised **0-1000 grid**,
    not in pixels — that is the model's trained convention and it ignores prompts
    asking for anything else. `door_height_px` keeps its name for continuity with
    the brief's schema, but in this class it is 0-1000. measure.py converts to
    real pixels; nothing else should read this model.
    """

    door_bbox: list[float] = Field(min_length=4, max_length=4)  # [x1, y1, x2, y2], 0-1000
    door_height_px: float  # 0-1000, despite the name
    ground_line_y: float  # 0-1000
    light_direction: str
    placement_zones: list[list[float]] = Field(default_factory=list)  # 0-1000


class Measurement(MeasurementRaw):
    """Observation converted to real pixels, plus the scale we derived from it.

    Unlike MeasurementRaw, every coordinate here is in **actual image pixels**.
    This is the model the rest of the pipeline consumes.

    px_per_metre and expected_planter_px are computed in Python, never asked of
    the model: a vision model guessing at metres is a hallucination we would then
    have to defend on a call.
    """

    px_per_metre: float
    expected_planter_px: float
    product_slug: str

    # Carried so the verifier can compare like with like: a composite may come
    # back at a different resolution than the frontage, so scale is checked as a
    # fraction of image height rather than in absolute pixels.
    image_width: int
    image_height: int


# --------------------------------------------------------------------------- #
# [5] composite
# --------------------------------------------------------------------------- #


class Composite(BaseModel):
    """One generated visual, with everything needed to reproduce it."""

    venue_id: str
    image_path: str
    prompt: str
    model: str
    attempt: int
    product_slug: str
    from_cache: bool = False


# --------------------------------------------------------------------------- #
# [6] verify
# --------------------------------------------------------------------------- #


class VerificationRaw(BaseModel):
    """The before/after diff judgement."""

    building_unaltered: bool
    product_faithful_to_reference: bool
    scale_plausible: bool
    grounded_with_shadow: bool
    planter_blocks_entrance: bool
    observed_planter_height_px: float | None = None
    verdict: Verdict
    reject_reasons: list[str] = Field(default_factory=list)


class Verification(VerificationRaw):
    """Model judgement plus our own arithmetic check on scale.

    The model is asked whether scale looks plausible, but we do not take its word
    for it: we compare its observed pixel height against the stage-4 expectation
    ourselves. `verdict` here is the final, code-owned decision.
    """

    scale_ratio: float | None = None  # observed / expected
    scale_within_tolerance: bool | None = None


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class VenueResult(BaseModel):
    """An accepted venue, with the full decision trail that got it there."""

    id: str
    name: str
    address: str
    postcode: str = ""
    lat: float
    lng: float
    area: str = ""
    image_source: ImageSource
    heading_used: float | None = None
    pano_distance_m: float | None = None
    product_slug: str
    product_description: str = ""
    assessment: Assessment
    measurement: Measurement
    verification: Verification
    frontage_url: str
    composite_url: str
    attempts: int = 1


class Funnel(BaseModel):
    """The numbers quoted in design.md. Every drop-off is accounted for."""

    discovered: int = 0
    after_chain_filter: int = 0
    after_status_filter: int = 0
    entered_pipeline: int = 0
    capture_ok: int = 0
    assess_ok: int = 0
    measure_ok: int = 0
    composite_ok: int = 0
    accepted: int = 0


class Thresholds(BaseModel):
    """The constants this run's decisions were made against.

    Shipped inside results.json so the UI renders the bar a venue actually had to
    clear, rather than a number copied into a component that silently goes stale
    the first time config.py is tuned. Populated from config at write time --
    never edited here.
    """

    frontage_bare_threshold: int
    standard_door_height_m: float
    scale_tolerance: float
    max_composite_attempts: int
    max_pano_distance_m: float
    people_prominence_threshold: int
    heading_nudge_deg: float


class RunStatus(BaseModel):
    """Live progress, polled by the UI while a run is in flight.

    `stage` is the machine name of the step currently executing, so the UI can
    light up the right one in a pipeline diagram rather than parse prose. The
    venue fields say who it is working on and how far through it is — "venue 3 of
    8" is the thing a watcher actually wants to know.
    """

    run_id: str
    stage: str = "queued"
    venue: str | None = None  # the venue currently being worked on
    venue_index: int = 0  # 1-based position in the shortlist
    venue_total: int = 0  # how many entered the paid stages
    processed: int = 0
    accepted: int = 0
    rejected: int = 0
    done: bool = False
    error: str | None = None
    started_at: str = Field(default_factory=_utc_now)
    finished_at: str | None = None


class RunCost(BaseModel):
    """Estimated USD cost of a run, from counted billable calls x config prices."""

    counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: dict[str, float] = Field(default_factory=dict)
    total_cost_usd: float = 0.0


class RunSettings(BaseModel):
    """The knobs a run was launched with. Echoed back so the UI shows what ran."""

    max_venues: int
    target_accepted: int
    # False = skip venues an earlier run already accepted, so this run spends its
    # budget on new ones instead of regenerating a visual that already exists.
    allow_duplicates: bool = True


class RunSummary(BaseModel):
    """One row of run history. Cheap enough to list without joining results."""

    run_key: str
    status: str
    stage: str = ""
    started_at: str
    finished_at: str | None = None
    duration_s: int | None = None
    dry_run: bool = False
    vision_model: str = ""
    image_model: str = ""
    max_venues: int | None = None
    target_accepted: int | None = None
    allow_duplicates: bool = True
    total_cost_usd: float = 0.0
    discovered: int = 0
    entered_pipeline: int = 0
    accepted: int = 0
    # Counted apart, always. A run that errored on every venue is not a run that
    # rejected every venue, and a history list that conflates them is lying.
    rejected_decisions: int = 0
    rejected_errors: int = 0
    error: str | None = None


class ResultsPayload(BaseModel):
    """What the live API, the database, and the static results.json all serve."""

    run_id: str = ""
    generated_at: str = Field(default_factory=_utc_now)
    dry_run: bool = False
    vision_model: str = ""
    image_model: str = ""
    thresholds: Thresholds | None = None
    settings: RunSettings | None = None
    cost: RunCost | None = None
    funnel: Funnel = Field(default_factory=Funnel)
    venues: list[VenueResult] = Field(default_factory=list)
    rejected: list[Rejection] = Field(default_factory=list)

    # Where this payload came from: "database" (durable, full history) or
    # "snapshot" (the committed results.json). Surfaced in the UI so it is always
    # obvious which one is on screen.
    source: Literal["database", "snapshot"] = "snapshot"
