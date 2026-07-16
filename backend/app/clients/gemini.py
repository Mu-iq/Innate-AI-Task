"""Gemini client wrapper: one client, bounded retries, defensive JSON, tracing.

Every Gemini call in the pipeline goes through here, so that prompt, raw
response and parsed result are traced to disk for every call without each
service having to remember to do it.

Two behaviours worth calling out:

* **JSON is parsed defensively even when we ask for JSON mode.** Vision models
  still occasionally wrap the object in prose or a markdown fence. We ask with
  `response_mime_type="application/json"` and a response schema where the SDK
  can convert one, then parse as if we had asked for neither, then re-ask up to
  MAX_JSON_PARSE_RETRIES times before giving up. A stage never sees a malformed
  dict.
* **The image model string is resolved by probing, not by faith.** See
  `resolve_image_model`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Sequence, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt

from app.config import (
    DRY_RUN,
    GEMINI_BACKOFF_INTERVAL_S,
    GEMINI_IMAGE_MODEL,
    GEMINI_IMAGE_MODEL_CANDIDATES,
    GEMINI_MAX_RETRY_WAIT_S,
    GEMINI_MIN_INTERVAL_S,
    GEMINI_TIMEOUT_S,
    GEMINI_VISION_MODEL,
    GEMINI_VISION_MODEL_CANDIDATES,
    MAX_GEMINI_RETRIES,
    MAX_JSON_PARSE_RETRIES,
    RETRY_BACKOFF_BASE_S,
    RETRY_BACKOFF_MAX_S,
    require_gemini_key,
)
from app.utils import cache, metrics
from app.utils.logging import get_logger, trace_gemini_call


def _usage_tokens(response: Any) -> tuple[int, int]:
    """(input, output) token counts from a Gemini response, best-effort.

    The SDK exposes usage_metadata with prompt/candidates token counts. If a
    response lacks it we return zeros rather than guess — a missing count costs
    nothing in the estimate, a fabricated one would mislead.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    inp = getattr(usage, "prompt_token_count", 0) or 0
    out = getattr(usage, "candidates_token_count", 0) or 0
    return int(inp), int(out)

log = get_logger("clients.gemini")

# Not PEP 695 syntax: the brief targets Python 3.11+, where `def f[T](...)` is a
# syntax error.
T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None
_resolved_image_model: str | None = None
_resolved_vision_model: str | None = None


class GeminiJSONError(RuntimeError):
    """The model returned something we could not turn into the expected shape."""


class GeminiNoModelError(RuntimeError):
    """No candidate model on this key could serve the request."""


def _is_schema_rejection(exc: BaseException) -> bool:
    """Did the API refuse our response_schema, as opposed to failing for any
    other reason?

    Only a 400/INVALID_ARGUMENT is worth retrying without the schema. A 404
    (model retired) or 429 (quota) has nothing to do with the schema, and
    retrying those without it just spends the call twice.
    """
    text = str(exc)
    if "429" in text or "404" in text or "RESOURCE_EXHAUSTED" in text or "NOT_FOUND" in text:
        return False
    return "400" in text or "INVALID_ARGUMENT" in text or "schema" in text.lower()


class GeminiNoImageError(RuntimeError):
    """The model responded, but with no image part. Usually a safety refusal."""


def get_client() -> genai.Client:
    """Single shared client. The SDK is thread-safe for our usage."""
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=require_gemini_key(),
            http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_S * 1000)),
        )
    return _client


# --------------------------------------------------------------------------- #
# Rate limiting
#
# The free tier allows ~5 requests/minute/model, and this pipeline makes three
# vision calls per venue back to back. A 429 is therefore a routine event, not an
# error — and crucially the API tells us exactly how long to wait. Two behaviours
# follow:
#
#   1. Retry on 429, sleeping for the API's OWN retryDelay rather than a guess.
#   2. After the first 429, pace every subsequent call so we stop walking into
#      the same wall. Waiting 13s voluntarily beats being told to wait 50s.
#
# The pacing floor only ever rises, and only within this process. On a paid key
# with no rate limit it never engages at all.
# --------------------------------------------------------------------------- #

_pace_lock = threading.Lock()
_min_interval_s: float = GEMINI_MIN_INTERVAL_S
_last_call_at: float = 0.0

# "Please retry in 53.38s" and "'retryDelay': '53s'" — both shapes appear.
_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s|'retryDelay':\s*'(\d+)s'")


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def _parse_retry_delay(exc: BaseException) -> float | None:
    """The wait the API asked for, in seconds. None if it did not say."""
    m = _RETRY_DELAY_RE.search(str(exc))
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _throttle() -> None:
    """Sleep just long enough to respect the current pacing floor."""
    global _last_call_at
    with _pace_lock:
        interval = _min_interval_s
        if interval > 0:
            wait = interval - (time.monotonic() - _last_call_at)
            if wait > 0:
                log.debug("pacing: sleeping %.1fs before next Gemini call", wait)
                time.sleep(wait)
        _last_call_at = time.monotonic()


def _adopt_pacing_floor() -> None:
    """Called on a 429. Slow every later call down so we stop hitting the limit."""
    global _min_interval_s
    with _pace_lock:
        if _min_interval_s < GEMINI_BACKOFF_INTERVAL_S:
            _min_interval_s = GEMINI_BACKOFF_INTERVAL_S
            log.warning(
                "rate limited — pacing all further Gemini calls to 1 per %.0fs. "
                "This key looks rate-limited (free tier is ~5/min); the run will be "
                "slower but will not keep hitting 429.",
                GEMINI_BACKOFF_INTERVAL_S,
            )


def _should_retry(exc: BaseException) -> bool:
    """Retry transport failures, 5xx (incl. 503 overloaded), and 429."""
    if isinstance(exc, (TimeoutError, ConnectionError, genai.errors.ServerError)):
        return True
    return _is_rate_limited(exc)


def _wait_gemini(retry_state: Any) -> float:
    """Honour the API's stated retryDelay; otherwise exponential backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is not None and _is_rate_limited(exc):
        _adopt_pacing_floor()
        asked = _parse_retry_delay(exc)
        if asked is not None:
            # +1s of headroom: the quota window is wall-clock and we would rather
            # wait a second longer than be refused a second time.
            return min(asked + 1.0, GEMINI_MAX_RETRY_WAIT_S)
    attempt = max(retry_state.attempt_number - 1, 0)
    return min(RETRY_BACKOFF_BASE_S * (2**attempt), RETRY_BACKOFF_MAX_S)


_retry_gemini = retry(
    stop=stop_after_attempt(MAX_GEMINI_RETRIES),
    wait=_wait_gemini,
    retry=retry_if_exception(_should_retry),
    reraise=True,
)


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response, however it was dressed up.

    Handles: bare JSON, markdown-fenced JSON, and JSON with prose either side.
    Raises GeminiJSONError rather than returning a half-parsed dict.
    """
    if not text or not text.strip():
        raise GeminiJSONError("Empty response")

    candidates: list[str] = [text.strip()]

    if (fenced := _FENCE_RE.search(text)) is not None:
        candidates.insert(0, fenced.group(1))

    # Outermost brace pair, for prose-wrapped objects.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise GeminiJSONError(f"No JSON object found in response: {text[:200]!r}")


# --------------------------------------------------------------------------- #
# Structured JSON generation
# --------------------------------------------------------------------------- #


@_retry_gemini
def _generate(model: str, contents: Sequence[Any], config: types.GenerateContentConfig) -> Any:
    _throttle()
    return get_client().models.generate_content(
        model=model, contents=list(contents), config=config
    )


def generate_json(
    *,
    model: str,
    contents: Sequence[Any],
    schema: type[T],
    run_id: str,
    stage: str,
    venue_id: str,
    prompt_for_trace: str,
    attempt: int = 1,
) -> T:
    """Call Gemini and return a validated pydantic model.

    Retries on malformed or schema-violating output, appending the parse error
    to the conversation so the model can correct itself rather than repeating
    the same mistake into a fixed number of wasted calls.
    """
    contents = list(contents)
    last_error: str = ""

    # Ask for a real response schema where we can, but never depend on it. Not
    # every model here converts cleanly -- MeasurementRaw.placement_zones is a
    # nested coordinate array, and nested arrays are exactly what schema
    # converters reject. The API can refuse the schema at REQUEST time (400
    # InvalidArgument), not just at assignment, and that error would otherwise
    # surface as a venue rejection that looks like a quality failure and isn't.
    # So on any request-time failure we drop the schema and try once more with
    # JSON mode alone. The defensive parse below is the actual guarantee; the
    # schema is only ever an optimisation.
    use_schema = True

    for parse_attempt in range(1, MAX_JSON_PARSE_RETRIES + 2):
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,  # judgements must be reproducible run to run
        )
        if use_schema:
            try:
                config.response_schema = schema
            except Exception:  # pragma: no cover - SDK-version dependent
                use_schema = False

        raw_text = ""
        try:
            response = _generate(model, contents, config)
            # Count the call and its tokens whether or not parsing then succeeds:
            # a malformed response was still billed.
            metrics.record_vision_call(*_usage_tokens(response))
            raw_text = (response.text or "").strip()
            data = extract_json_object(raw_text)
            parsed = schema.model_validate(data)
        except (GeminiJSONError, ValidationError) as exc:
            last_error = str(exc)
            trace_gemini_call(
                run_id=run_id,
                stage=stage,
                venue_id=f"{venue_id}_parsefail{parse_attempt}",
                model=model,
                prompt=prompt_for_trace,
                raw_response=raw_text,
                error=last_error,
                attempt=attempt,
            )
            log.warning(
                "%s: malformed JSON (parse attempt %d/%d): %s",
                stage,
                parse_attempt,
                MAX_JSON_PARSE_RETRIES + 1,
                last_error[:160],
            )
            # Give the model its own bad output plus the error, and re-ask.
            contents = [
                *contents,
                f"Your previous response could not be parsed: {last_error}\n"
                f"Return ONLY a single valid JSON object matching the requested "
                f"schema. No markdown, no commentary.",
            ]
            continue

        except Exception as exc:
            # The request itself failed. Only ONE class of failure is worth
            # retrying differently: the API rejecting our response schema (400 /
            # INVALID_ARGUMENT), where dropping the schema may get us an answer.
            #
            # Everything else must propagate untouched. Retrying a 404 (model
            # retired) or a 429 (quota) without the schema just spends another
            # call to be told the same thing — which is exactly what this did
            # before it was narrowed, wasting three calls per product plate
            # against a model that no longer existed.
            if use_schema and _is_schema_rejection(exc):
                use_schema = False
                last_error = str(exc)
                log.warning(
                    "%s: API rejected the response schema (%s). Retrying with JSON "
                    "mode only.",
                    stage,
                    str(exc)[:160],
                )
                continue
            raise

        trace_gemini_call(
            run_id=run_id,
            stage=stage,
            venue_id=venue_id,
            model=model,
            prompt=prompt_for_trace,
            raw_response=raw_text,
            parsed=parsed.model_dump(),
            attempt=attempt,
        )
        return parsed

    raise GeminiJSONError(
        f"{stage}: no valid JSON after {MAX_JSON_PARSE_RETRIES + 1} attempts. "
        f"Last error: {last_error}"
    )


# --------------------------------------------------------------------------- #
# Image generation
# --------------------------------------------------------------------------- #


def _extract_image_bytes(response: Any) -> bytes | None:
    """First inline image part in the response, or None."""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data
    return None


def _response_text(response: Any) -> str:
    """Any text the model returned alongside (or instead of) an image.

    On a refusal this is the only explanation we get, so it goes in the trace.
    """
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if text := getattr(part, "text", None):
                chunks.append(text)
    return "\n".join(chunks)


def generate_image(
    *,
    model: str,
    contents: Sequence[Any],
    run_id: str,
    stage: str,
    venue_id: str,
    prompt_for_trace: str,
    attempt: int = 1,
) -> bytes:
    """Multi-image generate call. Returns PNG/JPEG bytes or raises.

    Callers cache by prompt hash: this function always spends money.
    """
    config = types.GenerateContentConfig(
        response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
    )
    response = _generate(model, contents, config)
    # Billed per image, even if it comes back empty. The input tokens cover the
    # frontage + product references we sent.
    metrics.record_image_generation(_usage_tokens(response)[0])

    image = _extract_image_bytes(response)
    text = _response_text(response)

    trace_gemini_call(
        run_id=run_id,
        stage=stage,
        venue_id=venue_id,
        model=model,
        prompt=prompt_for_trace,
        raw_response=text or "<image returned, no text>",
        parsed={"image_bytes": len(image) if image else 0},
        error=None if image else "no image part in response",
        attempt=attempt,
    )

    if not image:
        raise GeminiNoImageError(
            f"No image part returned by {model}. Model said: {text[:300]!r}"
        )
    return image


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #


def resolve_vision_model(force_probe: bool = False) -> str:
    """Return a vision model string this key can actually call.

    Probed with a real (one-token) request rather than trusted from
    `models.list()`, because listing is not permission: `gemini-2.5-flash` still
    appears in the listing on a new key and answers 404 "no longer available to
    new users" when called. Anything that trusts the listing will confidently
    pick a model that does not work.

    Cached to disk, so this costs a fraction of a penny once. Set
    GEMINI_VISION_MODEL in .env to skip it.
    """
    global _resolved_vision_model

    if GEMINI_VISION_MODEL and not force_probe:
        return GEMINI_VISION_MODEL
    if _resolved_vision_model and not force_probe:
        return _resolved_vision_model

    key = cache.cache_key("vision_model_v2", *GEMINI_VISION_MODEL_CANDIDATES)
    if not force_probe and (hit := cache.get_json("model_probe", key)) is not None:
        if model := hit.get("model"):
            _resolved_vision_model = model
            return model

    errors: list[str] = []
    for candidate in GEMINI_VISION_MODEL_CANDIDATES:
        try:
            _generate(
                candidate,
                ['Reply with only this JSON: {"ok":true}'],
                types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.0
                ),
            )
        except Exception as exc:
            errors.append(f"{candidate}: {str(exc)[:90]}")
            log.info("vision model %s unavailable: %s", candidate, str(exc)[:90])
            continue

        log.info("resolved vision model: %s", candidate)
        _resolved_vision_model = candidate
        cache.put_json("model_probe", key, {"model": candidate})
        return candidate

    raise GeminiNoModelError(
        "No vision model on this key could be called. Tried:\n  "
        + "\n  ".join(errors)
        + "\nCheck billing is enabled, or set GEMINI_VISION_MODEL in .env."
    )


def _list_model_names() -> set[str]:
    """Model names this key can see. Free, and only ever used to SKIP probes."""
    try:
        return {
            name
            for m in get_client().models.list()
            if (name := (getattr(m, "name", "") or "").removeprefix("models/"))
        }
    except Exception as exc:
        log.warning("could not list models (%s); probing all candidates", exc)
        return set()


def resolve_image_model(force_probe: bool = False) -> str:
    """Return an image model string this key can actually generate with.

    The Nano Banana 2 identifier appears as `gemini-3.1-flash-image` on the AI
    Studio developer API and `-preview` on Vertex, and availability differs per
    key. Guessing wastes hours on confusing 404s.

    So we probe with a REAL generation rather than trusting `models.list()`.
    Listing is not permission — `gemini-2.5-flash` is still listed on a new key
    and answers 404 "no longer available to new users" when called. An earlier
    version of this function trusted the listing and would happily have returned
    a model that could not generate, failing at the first venue instead of here.

    The listing is still used, but only to *skip* probing models the key cannot
    see at all — never to conclude one works.

    Costs one small image generation (~$0.04), once, then cached to disk. That
    is worth paying at setup rather than discovering mid-run. Set
    GEMINI_IMAGE_MODEL in .env to skip it entirely.
    """
    global _resolved_image_model

    if GEMINI_IMAGE_MODEL and not force_probe:
        return GEMINI_IMAGE_MODEL
    if _resolved_image_model and not force_probe:
        return _resolved_image_model

    key = cache.cache_key("image_model_v2", *GEMINI_IMAGE_MODEL_CANDIDATES)
    if not force_probe and (hit := cache.get_json("model_probe", key)) is not None:
        if model := hit.get("model"):
            _resolved_image_model = model
            return model

    # In DRY_RUN nothing will be generated, so do not pay to verify a generator.
    # Report the best candidate the key lists and move on.
    if DRY_RUN:
        listed = _list_model_names()
        for candidate in GEMINI_IMAGE_MODEL_CANDIDATES:
            if candidate in listed:
                log.info("DRY_RUN: assuming image model %s (not probed)", candidate)
                return candidate
        return GEMINI_IMAGE_MODEL_CANDIDATES[0]

    listed = _list_model_names()
    errors: list[str] = []

    for candidate in GEMINI_IMAGE_MODEL_CANDIDATES:
        if listed and candidate not in listed:
            errors.append(f"{candidate}: not listed for this key")
            continue
        try:
            _probe_image_generation(candidate)
        except Exception as exc:
            errors.append(f"{candidate}: {str(exc)[:90]}")
            log.info("image model %s unusable: %s", candidate, str(exc)[:90])
            continue

        log.info("resolved image model: %s (verified by real generation)", candidate)
        _resolved_image_model = candidate
        cache.put_json("model_probe", key, {"model": candidate})
        return candidate

    raise GeminiNoModelError(
        "No image model on this key could generate. Tried:\n  "
        + "\n  ".join(errors)
        + "\nImage generation may not be enabled on a free-tier key — check "
        "billing, or set GEMINI_IMAGE_MODEL in .env to override."
    )


def _probe_image_generation(model: str) -> bytes:
    """Smallest real generation we can make. Raises unless image bytes come back."""
    from PIL import Image  # local import: only the probe path needs Pillow here

    swatch = Image.new("RGB", (64, 64), (200, 200, 200))
    config = types.GenerateContentConfig(
        response_modalities=[types.Modality.TEXT, types.Modality.IMAGE],
    )
    response = _generate(
        model, [swatch, "Add a small green dot in the centre. Output the image."], config
    )
    image = _extract_image_bytes(response)
    if not image:
        raise GeminiNoImageError(f"{model} responded without an image part")
    return image
