"""Every tunable number for the pipeline. Reasoning lives in design.md.

Secrets come from the environment (see .env.example), never from this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Resolved from this file, not the working directory, so how the process was
# launched doesn't matter.
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

load_dotenv(REPO_ROOT / ".env")
load_dotenv(BACKEND_DIR / ".env")


def env(name: str, default: str = "") -> str:
    """Read an env var, treating blank as unset.

    os.getenv only falls back to its default when the name is absent, so a `FOO=`
    line in .env would silently override every default below with "".
    """
    return (os.getenv(name) or "").strip() or default


PRODUCTS_DIR = BACKEND_DIR / "data" / "products"
PRODUCT_PLATES_DIR = PRODUCTS_DIR / "plates"

# Gitignored scratch. The database and storage bucket are the durable record.
OUTPUTS_DIR = BACKEND_DIR / "outputs"
CACHE_DIR = BACKEND_DIR / ".cache"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
# Bypasses row-level security. Backend only — never send to a browser.
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_BUCKET = env("SUPABASE_BUCKET", "storefront-visuals")


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def require_maps_key() -> str:
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError(
            "GOOGLE_MAPS_API_KEY is not set. Copy .env.example to .env and fill it in. "
            "Needs Places API (New) + Street View Static API enabled."
        )
    return GOOGLE_MAPS_API_KEY


def require_gemini_key() -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in. "
            "Get one at https://aistudio.google.com/apikey"
        )
    return GEMINI_API_KEY


# --------------------------------------------------------------------------- #
# Models — never inline a model string elsewhere
# --------------------------------------------------------------------------- #

GEMINI_VISION_MODEL = env("GEMINI_VISION_MODEL", "gemini-3.5-flash")
GEMINI_IMAGE_MODEL = env("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

# Used only if the model above stops working — Google retires models with little
GEMINI_VISION_MODEL_CANDIDATES: tuple[str, ...] = (
    GEMINI_VISION_MODEL,
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
)

GEMINI_IMAGE_MODEL_CANDIDATES: tuple[str, ...] = (
    GEMINI_IMAGE_MODEL,
    "gemini-3-pro-image-preview",
    "gemini-2.5-flash-image",
)


# --------------------------------------------------------------------------- #
# [1] Discovery
# --------------------------------------------------------------------------- #

# Areas and categories, not venues — the code picks the venues.
LONDON_AREAS: tuple[str, ...] = (
    "Shoreditch",
    "Soho",
    "Islington",
    "Hackney",
    "Clapham",
)

DISCOVERY_QUERY_TEMPLATES: tuple[str, ...] = (
    "independent cafe in {area}, London",
    "independent restaurant in {area}, London",
    "hair salon in {area}, London",
)

# Excluded because the pitch needs an owner who can say yes. A branch manager
# can't. Case-insensitive substring match.
CHAIN_BLOCKLIST: tuple[str, ...] = (
    "pret", "starbucks", "costa", "caffe nero", "caffè nero", "greggs",
    "gail's", "gails", "itsu", "leon", "nando's", "nandos", "wagamama",
    "pizza express", "pizzaexpress", "subway", "mcdonald", "kfc", "burger king",
    "five guys", "wasabi", "eat.", "paul ", "patisserie valerie", "byron",
    "franco manca", "honest burgers", "dishoom", "shake shack", "chipotle",
    "wetherspoon", "toni & guy", "toni and guy", "supercuts", "regis",
    "headmasters", "rush hair", "coffee republic", "black sheep coffee",
    "joe & the juice", "joe and the juice", "sourdough sophia", "gordon ramsay",
)

# No street frontage to dress.
INDOOR_CONTEXT_TERMS: tuple[str, ...] = (
    "food court", "shopping centre", "shopping center", "westfield",
    "food hall", "market hall", "arcade", "terminal", "airport",
    "station concourse", "boxpark", "kerb ", "mall",
)

MIN_CANDIDATES = 20
PLACES_PAGE_SIZE = 20  # API caps a single text query at 20

# Near-zero reviews usually means closed-but-unmarked or a ghost kitchen.
MIN_USER_RATINGS = 5


# --------------------------------------------------------------------------- #
# [2] Capture
# --------------------------------------------------------------------------- #

STREETVIEW_SIZE = "640x640"  # free-tier maximum
STREETVIEW_FOV = 75  # fills the frame, still keeps both door jambs and pavement
STREETVIEW_PITCH = 8  # slight tilt up: keeps the fascia without losing pavement
STREETVIEW_SOURCE = "outdoor"  # skip business-interior panoramas

# Beyond this the frontage is too small and too oblique to composite onto.
MAX_PANO_DISTANCE_M = 30.0

# One re-shoot on a bad framing. ~1/3 of the fov: recentres a door at the frame
# edge without photographing the shop next door.
HEADING_NUDGE_DEG = 25.0

PLACES_PHOTO_MAX_WIDTH_PX = 1600

# Street View returns 200 OK with a flat grey "no imagery" tile instead of an
# error. Anything this featureless is that tile.
BLANK_IMAGE_STDDEV_THRESHOLD = 6.0


# --------------------------------------------------------------------------- #
# [3] Assess
# --------------------------------------------------------------------------- #

# Bareness 0-10. At 6+ the entrance is visibly under-dressed; below it the
# frontage already has greenery and the pitch is weak.
FRONTAGE_BARE_THRESHOLD = 6

# Google blurs faces, but a frame full of people is a GDPR risk and poor outreach
# material anyway.
PEOPLE_PROMINENCE_THRESHOLD = 6


# --------------------------------------------------------------------------- #
# [4] Measure
# --------------------------------------------------------------------------- #

# Gemini reports coordinates on a 0-1000 grid, not pixels, whatever the prompt
# asks. measure.py converts. Reading them as pixels made every planter ~56% too
# large, and the verifier compared against the same wrong number.
GEMINI_COORD_SCALE = 1000.0

# The scale anchor: UK door leaf 1981mm + frame. Real shopfront doors vary
# (2.0-2.3m), hence ~10% error — which is why SCALE_TOLERANCE is so wide.
STANDARD_DOOR_HEIGHT_M = 2.03

# A "door" filling 5% or 95% of the frame is a hallucination, and would poison
# px_per_metre for everything downstream.
MIN_DOOR_HEIGHT_FRAC = 0.15
MAX_DOOR_HEIGHT_FRAC = 0.85


# --------------------------------------------------------------------------- #
# Products — the client's three planters, from their brief PDF
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProductSpec:
    slug: str
    filename: str
    description: str  # goes into the composite prompt verbatim
    # The vessel alone, never the planting (foliage is seasonal, the pot isn't).
    # Drives the whole scale system: expected_px = px_per_metre * body_height_m.
    # Estimated from the photos — the client sent no spec sheet.
    body_height_m: float


PRODUCT_SPECS: tuple[ProductSpec, ...] = (
    ProductSpec(
        slug="charcoal_drum",
        filename="planter_1.png",
        description=(
            "a large matte charcoal-black cylindrical drum planter with a smooth, "
            "seamless finish and no visible rim lip, planted with layered shade "
            "foliage: broad-leaved hosta, variegated Fatsia japonica, fine "
            "ornamental grasses and trailing eucalyptus"
        ),
        body_height_m=0.70,
    ),
    ProductSpec(
        slug="corten_column",
        filename="planter_2.png",
        description=(
            "a Corten weathering-steel square column planter with a rust-orange "
            "patina, sharp mitred edges and a small brushed-metal maker's plaque "
            "on the front face, paired with a matching lower Corten cube planter"
        ),
        body_height_m=1.00,
    ),
    ProductSpec(
        slug="white_tapered",
        filename="planter_3.png",
        description=(
            "a gloss-white tapered square planter shaped like an inverted "
            "truncated pyramid with a narrow base and a fine line-art graphic on "
            "the front face, supplied as a matched pair, planted with mixed pink "
            "perennials and green shrub foliage"
        ),
        body_height_m=0.65,
    ),
)

PRODUCTS_BY_SLUG: dict[str, ProductSpec] = {p.slug: p for p in PRODUCT_SPECS}

# The references are lifestyle shots, so each is auto-cropped to its hero product
# first. A crop this small means the model found a background pot — keep the full
# photo instead, since a planter cut off at the rim teaches it the product is a bowl.
MIN_PRODUCT_CROP_AREA_FRAC = 0.04

# Which planter goes on which frontage. Assess reports the shopfront's palette
# and this decides — contrast, because a planter that blends in sells nothing.
DEFAULT_PRODUCT_SLUG = "charcoal_drum"
PRODUCT_MATCH_RULES: dict[str, str] = {
    "dark": "white_tapered",  # black/navy shopfront -> white reads at distance
    "light": "charcoal_drum",  # pale render -> charcoal anchors the entrance
    "warm_brick": "corten_column",  # brick -> Corten shares the palette
    "mixed": DEFAULT_PRODUCT_SLUG,
}


# --------------------------------------------------------------------------- #
# [5] Composite / [6] Verify
# --------------------------------------------------------------------------- #

# One attempt plus one retry carrying the verifier's reasons. A third paid
# generation on a frontage that already failed twice is worse value than the next
# venue.
MAX_COMPOSITE_ATTEMPTS = 2

# How far the rendered planter may be from the expected height. Loose on purpose:
# it absorbs the ~10% door assumption plus bbox noise. Still catches what matters
# — a doll-sized or skip-sized planter is off by 2x, not 40%.
SCALE_TOLERANCE = 0.40


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Caps what reaches the paid stages. Discovery still pulls MIN_CANDIDATES, so the
# funnel stays real.
MAX_VENUES = int(env("MAX_VENUES", "8"))
TARGET_ACCEPTED = int(env("TARGET_ACCEPTED", "3"))

# The UI can raise the two above, never past these. The server clamps every
# request — that's what keeps an adjustable knob from becoming an unbounded bill.
MAX_VENUES_HARD_CAP = int(env("MAX_VENUES_HARD_CAP", "12"))
TARGET_ACCEPTED_HARD_CAP = int(env("TARGET_ACCEPTED_HARD_CAP", "6"))

# POST /api/run has no auth on purpose — a demo you need a password to try is a
# demo nobody tries. Bounded rather than locked.
MAX_CONCURRENT_RUNS = 1  # a second caller watches the first run
MAX_RUNS_PER_HOUR = int(env("MAX_RUNS_PER_HOUR", "3"))


# --------------------------------------------------------------------------- #
# Cost tracking (USD)
# --------------------------------------------------------------------------- #

# Per call. Street View metadata is free and isn't counted.
PLACES_TEXT_SEARCH_COST = float(env("PLACES_TEXT_SEARCH_COST", "0.032"))
STREETVIEW_STATIC_COST = float(env("STREETVIEW_STATIC_COST", "0.007"))
PLACES_PHOTO_COST = float(env("PLACES_PHOTO_COST", "0.007"))

# gemini-3.5-flash, per 1M tokens. Images sent to it are tokenised and counted in
# the input figure, which we read from each response.
GEMINI_VISION_INPUT_COST_PER_1M = float(env("GEMINI_VISION_INPUT_COST_PER_1M", "1.50"))
GEMINI_VISION_OUTPUT_COST_PER_1M = float(env("GEMINI_VISION_OUTPUT_COST_PER_1M", "9.00"))

# gemini-3.1-flash-image. Its text/image input is $0.25/1M; a generated image is
# priced per image, not at the text output rate. 1K (~1MP) is the default output
# size at 1,120 tokens ≈ $0.067. Raise if the model is asked for 2K/4K.
GEMINI_IMAGE_INPUT_COST_PER_1M = float(env("GEMINI_IMAGE_INPUT_COST_PER_1M", "0.25"))
GEMINI_IMAGE_COST_PER_IMAGE = float(env("GEMINI_IMAGE_COST_PER_IMAGE", "0.067"))


# --------------------------------------------------------------------------- #
# Timeouts, retries, caching, CORS
# --------------------------------------------------------------------------- #

HTTP_TIMEOUT_S = 30.0
GEMINI_TIMEOUT_S = 120.0  # image generation is slow

MAX_HTTP_RETRIES = 3

# Higher than the HTTP one because it also absorbs 429s: the free tier allows
# ~5 requests/min and we make 3 vision calls per venue back to back.
MAX_GEMINI_RETRIES = 5

# A free-tier 429 asks for ~50s; we honour the delay the API states rather than
# guessing. This is the ceiling on any single wait.
GEMINI_MAX_RETRY_WAIT_S = 75.0

# Minimum gap between Gemini calls. 0 = no pacing (right for a paid key). The
# client raises it to GEMINI_BACKOFF_INTERVAL_S after the first 429 anyway;
# setting it up front just skips that first wait.
GEMINI_MIN_INTERVAL_S = float(env("GEMINI_MIN_INTERVAL_S", "0"))
GEMINI_BACKOFF_INTERVAL_S = 13.0  # ~5 requests/minute with headroom

# Vision models sometimes wrap the JSON in prose. Re-ask rather than crash.
MAX_JSON_PARSE_RETRIES = 2

RETRY_BACKOFF_BASE_S = 1.0
RETRY_BACKOFF_MAX_S = 15.0

# Paid calls are cached by deterministic key, so a re-run with unchanged inputs
# costs nothing. False forces regeneration.
CACHE_ENABLED = env("CACHE_ENABLED", "true").lower() in ("1", "true", "yes")

# Any localhost port, by regex not a list: Vite silently moves to 5174 when 5173
# is taken, and the result is an opaque "Failed to fetch" next to a 200 in the
# server log.
CORS_LOCALHOST_REGEX = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

# Deployed frontends, comma-separated. No wildcard: this backend spends money.
_EXTRA_ORIGINS = tuple(
    o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()
)

CORS_ORIGINS: tuple[str, ...] = _EXTRA_ORIGINS
