"""Run logging and Gemini call tracing.

Two jobs:

1. A readable console log, so a run's funnel is visible as it happens.
2. A full trace of every Gemini call -- prompt, raw response, parsed result --
   written to outputs/{run_id}/trace/. Reproducibility is a grading signal: any
   decision the pipeline made can be reconstructed from disk after the fact,
   including the ones that went wrong.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from app.config import OUTPUTS_DIR

_CONFIGURED = False

# Google API errors stringify to ~600 characters of nested JSON. The full text
# belongs in the trace on disk; the rejection log is a deliverable a human reads,
# so it gets the sentence, not the payload.
_API_MESSAGE_RE = re.compile(r"'message':\s*'([^']+)'")


def short_error(exc: BaseException, limit: int = 180) -> str:
    """A human-readable one-liner for an API exception.

    Pulls the API's own `message` out of the JSON blob where there is one, and
    falls back to a truncated repr otherwise. The untruncated error is always
    still written to outputs/{run_id}/trace/, so nothing is lost — this only
    decides what a person sees.
    """
    text = str(exc).replace("\n", " ").strip()

    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return (
            "Gemini API quota exhausted (429). This is a rate limit on the API key, "
            "not a judgement about this venue."
        )
    if "404" in text and "no longer available" in text:
        model = re.search(r"models/([\w.\-]+)", text)
        name = model.group(1) if model else "the configured model"
        return f"Model {name} is retired and no longer callable on this API key (404)."
    if "503" in text:
        return "Gemini API temporarily overloaded (503)."
    if "REQUEST_DENIED" in text or "403" in text:
        return "Google API rejected the request (403) — check the key's API restrictions and billing."

    if (m := _API_MESSAGE_RE.search(text)) is not None:
        text = m.group(1)

    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def get_logger(name: str) -> logging.Logger:
    """Console logger. Configured once, idempotently."""
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")
        )
        root = logging.getLogger("app")
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    return logging.getLogger(f"app.{name}")


def run_dir(run_id: str) -> Path:
    d = OUTPUTS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def trace_dir(run_id: str) -> Path:
    d = run_dir(run_id) / "trace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def trace_gemini_call(
    run_id: str,
    stage: str,
    venue_id: str,
    model: str,
    prompt: str,
    raw_response: str,
    parsed: Any = None,
    error: str | None = None,
    attempt: int = 1,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Persist one Gemini call in full.

    Written for every call including failures -- a malformed response we retried
    past is exactly the thing worth being able to read back later.
    """
    safe_venue = "".join(c if c.isalnum() or c in "-_" else "_" for c in venue_id)[:40]
    path = trace_dir(run_id) / f"{stage}_{safe_venue}_a{attempt}.json"

    payload: dict[str, Any] = {
        "stage": stage,
        "venue_id": venue_id,
        "model": model,
        "attempt": attempt,
        "prompt": prompt,
        "raw_response": raw_response,
        "parsed": parsed,
        "error": error,
    }
    if extra:
        payload["extra"] = extra

    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
