"""Sanity checks on the geo maths.

Deliberately not a test suite. The brief asks for a couple of checks on the geo
maths and nothing more, so this covers the two ways the bearing calculation can
be silently wrong -- reversed direction, and a broken wrap across north -- plus
the distance check that gates the Places Photos fallback. Everything else in the
pipeline is a network call or a model judgement, neither of which a unit test
tells the truth about.

Run:  cd backend && python -m pytest tests/ -q
"""

from __future__ import annotations

import math

import pytest

from app.utils.geo import haversine_m, initial_bearing, normalise_bearing, nudge_heading

# Two real London points ~1.1km apart, roughly north-south.
LIVERPOOL_ST = (51.5178, -0.0823)
SHOREDITCH_HIGH_ST = (51.5237, -0.0754)


class TestInitialBearing:
    def test_due_north_is_zero(self) -> None:
        assert initial_bearing(51.5, -0.1, 51.6, -0.1) == pytest.approx(0.0, abs=0.01)

    def test_due_south_is_180(self) -> None:
        assert initial_bearing(51.6, -0.1, 51.5, -0.1) == pytest.approx(180.0, abs=0.01)

    def test_due_east_is_about_90(self) -> None:
        """Note: NOT exactly 90.

        Following a parallel due east is a rhumb line, not a great circle. The
        great circle between two points at the same latitude bows towards the
        pole, so at 51.5N it sets off a fraction north of east (~89.96) and
        curves back. The tolerance here is the size of that bow over 0.1 deg of
        longitude, and asserting an exact 90 would be asserting the wrong maths.
        Street View wants the great-circle bearing, which is what we compute.
        """
        assert initial_bearing(51.5, -0.1, 51.5, 0.0) == pytest.approx(90.0, abs=0.05)

    def test_due_west_is_about_270(self) -> None:
        """Mirror of the above: a fraction north of west, for the same reason."""
        assert initial_bearing(51.5, 0.0, 51.5, -0.1) == pytest.approx(270.0, abs=0.05)

    def test_direction_is_not_reversed(self) -> None:
        """The bug that photographs the shop across the road.

        Reversing the arguments must flip the bearing by ~180 degrees. If capture
        ever passes (venue -> pano) instead of (pano -> venue), this is what
        catches it.
        """
        there = initial_bearing(*LIVERPOOL_ST, *SHOREDITCH_HIGH_ST)
        back = initial_bearing(*SHOREDITCH_HIGH_ST, *LIVERPOOL_ST)
        delta = abs(there - back) % 360.0
        assert delta == pytest.approx(180.0, abs=1.0)

    def test_always_in_compass_range(self) -> None:
        """Street View rejects a negative heading; atan2 returns them natively."""
        for lat, lng in [(51.6, -0.2), (51.4, 0.1), (51.5, -0.0823), (51.51, -0.09)]:
            b = initial_bearing(*LIVERPOOL_ST, lat, lng)
            assert 0.0 <= b < 360.0

    def test_known_london_bearing(self) -> None:
        """Liverpool St -> Shoreditch High St is roughly north-east."""
        assert initial_bearing(*LIVERPOOL_ST, *SHOREDITCH_HIGH_ST) == pytest.approx(
            41.0, abs=5.0
        )


class TestHaversine:
    def test_zero_distance(self) -> None:
        assert haversine_m(*LIVERPOOL_ST, *LIVERPOOL_ST) == pytest.approx(0.0, abs=0.01)

    def test_known_london_distance(self) -> None:
        d = haversine_m(*LIVERPOOL_ST, *SHOREDITCH_HIGH_ST)
        assert d == pytest.approx(830.0, rel=0.1)

    def test_is_symmetric(self) -> None:
        a = haversine_m(*LIVERPOOL_ST, *SHOREDITCH_HIGH_ST)
        b = haversine_m(*SHOREDITCH_HIGH_ST, *LIVERPOOL_ST)
        assert a == pytest.approx(b, abs=0.01)

    def test_typical_pano_offset_is_under_the_cap(self) -> None:
        """~15m north: a normal kerbside panorama, must pass MAX_PANO_DISTANCE_M."""
        lat, lng = LIVERPOOL_ST
        d = haversine_m(lat, lng, lat + 15 / 111_320, lng)
        assert d == pytest.approx(15.0, rel=0.02)
        assert d < 30.0


class TestHeadingNudge:
    def test_wraps_across_north(self) -> None:
        """350 + 25 must be 15, not 375. Street View silently misreads 375."""
        assert nudge_heading(350.0, 25.0) == pytest.approx(15.0, abs=0.01)

    def test_wraps_negative_across_north(self) -> None:
        assert nudge_heading(10.0, -25.0) == pytest.approx(345.0, abs=0.01)

    def test_normalise_handles_negatives(self) -> None:
        assert normalise_bearing(-90.0) == pytest.approx(270.0, abs=0.01)
        assert normalise_bearing(450.0) == pytest.approx(90.0, abs=0.01)

    def test_nudge_is_reversible(self) -> None:
        for start in (0.0, 90.0, 180.0, 359.9):
            out = nudge_heading(nudge_heading(start, 25.0), -25.0)
            assert math.isclose(out, start, abs_tol=0.01)
