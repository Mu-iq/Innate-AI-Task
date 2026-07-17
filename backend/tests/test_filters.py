"""Sanity checks on the discovery filters.

Here for the same reason as the geo tests: this is a place where a silent error
produces a confident wrong answer. A venue wrongly filtered never reaches a
vision call, so nothing downstream can notice — it just quietly never appears,
and the rejection log states a reason that is false.

Both false positives below are real. They came out of a live run against Google
Places, when the filters matched on plain substrings:

    "Pretty Earth"      binned as a chain     — "Pretty" contains "pret"
    "Small square cafe" binned as in a mall   — "Small"  contains "mall"

Run:  cd backend && python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from app.services.discovery import _is_chain, _is_indoor_context


class TestChainBlocklistCatchesRealChains:
    """A branch manager cannot say yes to a planter, so chains must go."""

    @pytest.mark.parametrize(
        "name",
        [
            "Pret A Manger",
            "Pret",
            "Starbucks Soho",
            "Costa Coffee",
            "Headmasters Clapham Junction",
            "Black Sheep Coffee",
            "Wagamama",
            "Pizza Express",
            "Franco Manca",
            "Dishoom Shoreditch",
            "Toni & Guy",
            "Joe & The Juice",
        ],
    )
    def test_named_chain_is_caught(self, name: str) -> None:
        assert _is_chain(name) is not None

    @pytest.mark.parametrize(
        "name",
        ["McDonald's", "McDonalds", "Gail's Bakery", "Gails", "Nando's", "Nandos", "Paul", "Pauls"],
    )
    def test_possessive_and_plural_forms_are_caught(self, name: str) -> None:
        """The list says "mcdonald"; the shopfront says "McDonald's" or "McDonalds"."""
        assert _is_chain(name) is not None

    def test_term_ending_in_punctuation_still_matches(self) -> None:
        """"eat." ends in a dot. A blanket trailing \\b would never match it."""
        assert _is_chain("EAT.") is not None


class TestChainBlocklistSparesIndependents:
    """The regression that matters: a real independent must not be binned."""

    def test_pretty_earth_is_not_pret(self) -> None:
        """The original false positive. "Pretty" contains "pret"."""
        assert _is_chain("Pretty Earth") is None

    @pytest.mark.parametrize(
        "name",
        [
            "Paulo Deli",  # contains "paul"
            "Leonardo Cafe",  # contains "leon"
            "Leonie Bar",  # contains "leon"
            "Preto Brazilian",  # contains "pret"
            "Subwaves",  # contains "subway"
            "Eaton Place Cafe",  # contains "eat"
        ],
    )
    def test_names_merely_containing_a_brand_survive(self, name: str) -> None:
        assert _is_chain(name) is None


class TestIndoorContext:
    """Venues inside a container have no street frontage to dress."""

    @pytest.mark.parametrize(
        "address",
        [
            "Unit 5, Westfield London",
            "The Food Court, Selfridges",
            "Boxpark Shoreditch",
            "Brent Cross Shopping Centre",
        ],
    )
    def test_real_container_is_caught(self, address: str) -> None:
        assert _is_indoor_context("Some Cafe", address) is not None

    def test_small_is_not_a_mall(self) -> None:
        """The second false positive. "Small" contains "mall"."""
        assert _is_indoor_context("Small square cafe", "6 Ravey St, London EC2A 4QW") is None

    @pytest.mark.parametrize(
        "name",
        ["Smallwood Bakery", "The Mallard", "Kerbside Coffee"],
    )
    def test_names_merely_containing_a_container_word_survive(self, name: str) -> None:
        assert _is_indoor_context(name, "12 Some St, London E1") is None
