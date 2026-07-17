"""Generate the run-dependent tables in design.md from the database.

    cd backend && python -m scripts.design_tables            # latest run
    cd backend && python -m scripts.design_tables <run_key>  # a specific run

design.md has to quote a real funnel, the real venues that were selected, and the
real rejections. Typing those by hand invites them to drift from what the
pipeline actually did. This reads the run back from the database and prints
markdown to paste into the marked sections. Re-run it after any pipeline run.
"""

from __future__ import annotations

import sys

from app.schemas import ResultsPayload
from app.services import repository


def _check_reconciles(p: ResultsPayload) -> list[str]:
    """Every drop between two rows must have a rejection row explaining it.

    design.md prints the funnel and the rejection table next to each other and
    claims the second accounts for the first. That claim went stale once already
    — a counter that only incremented on success made "composite generated" a
    silent duplicate of "accepted", so a venue whose composite was refused
    vanished from the arithmetic. Nothing failed; the table just quietly lied.

    So the script refuses to print a funnel it cannot reconcile. It is checking
    the pipeline's bookkeeping, not its own.
    """
    f = p.funnel
    drops: dict[str, int] = {}
    for r in p.rejected:
        if r.stage != "discover":
            drops[r.stage] = drops.get(r.stage, 0) + 1

    steps = [
        ("entered the paid stages", "capture", f.entered_pipeline, f.capture_ok),
        ("frontage photographed", "assess", f.capture_ok, f.assess_ok),
        ("passed assessment", "measure", f.assess_ok, f.measure_ok),
        ("scale measured", "composite", f.measure_ok, f.composite_ok),
        ("composite generated", "verify", f.composite_ok, f.accepted),
    ]
    problems = []
    for label, stage, before, after in steps:
        expected = drops.get(stage, 0)
        if before - after != expected:
            problems.append(
                f"{label}: {before} -> {after} loses {before - after}, but "
                f"{expected} rejection(s) are recorded at '{stage}'"
            )
    if f.accepted != len(p.venues):
        problems.append(f"funnel.accepted={f.accepted} but {len(p.venues)} venues are stored")
    return problems


def _funnel_table(p: ResultsPayload) -> str:
    f = p.funnel
    cap = p.settings.max_venues if p.settings else None
    entered_note = "Everything that survived discovery, capped by MAX_VENUES"
    if cap is not None:
        entered_note = f"Shortlist capped at {cap} (MAX_VENUES); the run stops early on reaching the target"
    rows = [
        ("Pulled from Google Places", f.discovered, "Text Search across 5 areas x 3 categories"),
        ("Survived chain / indoor filters", f.after_chain_filter, "Name blocklist + container terms"),
        ("Survived status / review filters", f.after_status_filter, "OPERATIONAL, >= 5 reviews"),
        ("Entered the paid stages", f.entered_pipeline, entered_note),
        ("Frontage photographed", f.capture_ok, "Street View, or Places Photos fallback"),
        ("Passed vision assessment", f.assess_ok, "Entrance visible, framing usable, bare enough"),
        ("Scale measured", f.measure_ok, "Door found and within sanity bounds"),
        ("Composite generated", f.composite_ok, "Nano Banana returned an image"),
        ("**Accepted**", f.accepted, "**Passed verification — safe to send**"),
    ]
    out = ["| Stage | Remaining | Gate |", "|---|---:|---|"]
    out += [f"| {label} | {n} | {note} |" for label, n, note in rows]
    return "\n".join(out)


def _venues_table(p: ResultsPayload) -> str:
    if not p.venues:
        return "_No venues accepted in this run._"
    out = [
        "| Venue | Address | Postcode | Bareness | Source | Planter | Attempts |",
        "|---|---|---|---:|---|---|---:|",
    ]
    for v in p.venues:
        src = "Street View" if v.image_source == "streetview" else "Places photo"
        if v.heading_used is not None:
            src += f" @ {v.heading_used:.0f}°"
        out.append(
            f"| {v.name} | {v.address} | {v.postcode or '—'} | "
            f"{v.assessment.frontage_bare_score}/10 | {src} | "
            f"{v.product_slug.replace('_', ' ')} | {v.attempts} |"
        )
    return "\n".join(out)


def _rejections_table(p: ResultsPayload) -> str:
    if not p.rejected:
        return "_Nothing was rejected in this run._"
    out = ["| Venue | Stage | Reason |", "|---|---|---|"]
    for r in p.rejected:
        reason = "; ".join(r.reasons).replace("|", "\\|")
        out.append(f"| {r.venue_name} | {r.stage} | {reason} |")
    return "\n".join(out)


def _rejection_summary(p: ResultsPayload) -> str:
    by_stage: dict[str, int] = {}
    for r in p.rejected:
        by_stage[r.stage] = by_stage.get(r.stage, 0) + 1
    if not by_stage:
        return "_No rejections._"
    out = ["| Stage | Rejected |", "|---|---:|"]
    out += [f"| {s} | {n} |" for s, n in sorted(by_stage.items(), key=lambda kv: -kv[1])]
    return "\n".join(out)


def _latest_succeeded_key() -> str | None:
    """The newest run that actually finished.

    Not simply the newest run: a run still in flight has an empty funnel and zero
    cost until finish_run() writes them, so defaulting to "latest" would quietly
    print a table of zeros — into the one document where the numbers are the
    point.
    """
    for r in repository.list_runs(limit=25):
        if r.status == "succeeded":
            return r.run_key
    return None


def main() -> int:
    # A specific run via argv, otherwise the most recent completed one.
    run_key = sys.argv[1] if len(sys.argv) > 1 else _latest_succeeded_key()
    if run_key is None:
        print("No completed runs in the database. Let a run finish, then re-run this.")
        return 1

    p = repository.get_run(run_key)
    if p is None:
        print(f"No run with key {run_key}.")
        return 1

    if problems := _check_reconciles(p):
        print(f"Run {p.run_id}: the funnel does not reconcile with the rejection table.\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nNothing printed: design.md prints these two tables side by side and")
        print("says the second explains the first. Fix the counters, not the prose.")
        return 1

    print(f"<!-- Generated by scripts/design_tables.py from run {p.run_id} -->")
    print(f"<!-- Image model: {p.image_model or 'n/a'}{' | DRY RUN' if p.dry_run else ''} -->\n")
    print("### The funnel\n")
    print(_funnel_table(p))
    print("\n### The chosen venues\n")
    print(_venues_table(p))
    print("\n### Rejections by stage\n")
    print(_rejection_summary(p))
    print("\n### Every rejection, with its reason\n")
    print(_rejections_table(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
