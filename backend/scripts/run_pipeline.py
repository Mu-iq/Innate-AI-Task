"""Run the pipeline from the command line.

    cd backend && python -m scripts.run_pipeline

Saves every run to the database and its images to the storage bucket, and prints
the funnel at the end -- those numbers go straight into design.md.

Honour the config: MAX_VENUES caps the paid stages, DRY_RUN disables image
generation entirely, and everything is cached, so a re-run with unchanged inputs
costs nothing.
"""

from __future__ import annotations

import sys

from app.config import DRY_RUN, MAX_VENUES, TARGET_ACCEPTED
from app.services.pipeline import run_pipeline


def main() -> int:
    print(f"Running pipeline (MAX_VENUES={MAX_VENUES}, TARGET_ACCEPTED={TARGET_ACCEPTED}, DRY_RUN={DRY_RUN})\n")

    payload = run_pipeline()
    f = payload.funnel

    print("\n" + "=" * 68)
    print("FUNNEL")
    print("=" * 68)
    rows = [
        ("Discovered from Places", f.discovered),
        ("Survived chain / indoor filters", f.after_chain_filter),
        ("Survived status / review filters", f.after_status_filter),
        ("Entered paid stages (MAX_VENUES)", f.entered_pipeline),
        ("Frontage captured", f.capture_ok),
        ("Passed vision assessment", f.assess_ok),
        ("Scale measured", f.measure_ok),
        ("Composite generated", f.composite_ok),
        ("ACCEPTED (passed verification)", f.accepted),
    ]
    for label, n in rows:
        print(f"  {label:.<44} {n:>3}")

    print(f"\n  Rejected, with reasons: {len(payload.rejected)}")
    by_stage: dict[str, int] = {}
    for r in payload.rejected:
        by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
    for stage, n in sorted(by_stage.items(), key=lambda kv: -kv[1]):
        print(f"    {stage:.<42} {n:>3}")

    if payload.venues:
        print("\n  Accepted venues:")
        for v in payload.venues:
            print(f"    - {v.name}, {v.postcode} ({v.area})")
            print(
                f"      bareness {v.assessment.frontage_bare_score}/10 | "
                f"{v.image_source} | {v.product_slug} | {v.attempts} attempt(s)"
            )

    print("\n" + "=" * 68)
    if payload.source == "database":
        print("Saved to the database and storage bucket. View it in the app's run history.")
    else:
        print("NOT saved — the database was not configured. Set SUPABASE_* in .env.")
    print("=" * 68)

    return 0 if payload.venues else 1


if __name__ == "__main__":
    sys.exit(main())
