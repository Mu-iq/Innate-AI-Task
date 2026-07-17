"""The funnel must reconcile with the rejection table.

design.md prints both side by side and claims the second explains the first. That
claim went stale once: `composite_ok` only incremented when verification passed,
which made it a silent duplicate of `accepted` and erased venues whose composite
was generated and then refused. Nothing errored — the arithmetic just stopped
adding up, in the one document where the numbers are the point.

These tests fix the meaning of each counter so that cannot come back.
"""

from __future__ import annotations

import pytest

from app.schemas import Funnel, Rejection
from app.services.repository import reconcile_funnel

DISCOVERY = {"discovered": 285, "after_chain_filter": 279, "after_status_filter": 279}


def rej(stage: str, name: str = "A Venue") -> Rejection:
    return Rejection(venue_id=name.lower(), venue_name=name, address="", stage=stage, reasons=["r"])


def test_verify_rejection_still_counts_as_a_composite_generated():
    """The bug that made design.md disagree with itself.

    A venue refused by the verifier HAD an image generated — that is the whole
    point of having a verifier. Counting only accepted composites makes the
    actor-critic loop invisible in the funnel.
    """
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=3, rejected=[rej("verify", "Gloria")])
    assert f.composite_ok == 4, "the refused composite was still generated"
    assert f.accepted == 3


def test_composite_rejection_means_no_image_came_back():
    """The mirror case: generation itself failed, so nothing was generated."""
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=3, rejected=[rej("composite")])
    assert f.measure_ok == 4
    assert f.composite_ok == 3


def test_the_real_run_reconciles_end_to_end():
    """Run 20260716-235319-f88e2c, the one design.md quotes."""
    rejected = [rej("assess", f"assess-{i}") for i in range(6)] + [rej("verify", "Gloria")]
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=3, rejected=rejected)

    assert (f.entered_pipeline, f.capture_ok, f.assess_ok) == (10, 10, 4)
    assert (f.measure_ok, f.composite_ok, f.accepted) == (4, 4, 3)


def test_every_drop_is_explained_by_a_rejection():
    """The property design.md actually relies on, checked at every stage."""
    rejected = [rej("capture"), rej("assess"), rej("measure"), rej("composite"), rej("verify")]
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=2, rejected=rejected)

    assert f.entered_pipeline == 7  # 2 accepted + 5 dropped
    for before, after in [
        (f.entered_pipeline, f.capture_ok),
        (f.capture_ok, f.assess_ok),
        (f.assess_ok, f.measure_ok),
        (f.measure_ok, f.composite_ok),
        (f.composite_ok, f.accepted),
    ]:
        assert before - after == 1, "each stage lost exactly its one recorded rejection"


def test_discovery_rejections_never_enter_the_paid_stages():
    """A chain filtered on its name never cost a penny; it is not a drop-out."""
    rejected = [rej("discover", f"chain-{i}") for i in range(6)]
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=3, rejected=rejected)

    assert f.entered_pipeline == 3, "discovery drops are pre-pipeline"
    assert f.capture_ok == 3


def test_discovery_counters_are_passed_through_untouched():
    """They are not derivable: we keep the survivors, not the ~285 we filtered."""
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=0, rejected=[])
    assert (f.discovered, f.after_chain_filter, f.after_status_filter) == (285, 279, 279)


@pytest.mark.parametrize("accepted", [0, 1, 12])
def test_accepted_always_matches_the_venues_stored(accepted: int):
    f = reconcile_funnel(Funnel(**DISCOVERY), accepted=accepted, rejected=[])
    assert f.accepted == accepted
    assert f.entered_pipeline == accepted, "nothing rejected, so everything entered was accepted"
