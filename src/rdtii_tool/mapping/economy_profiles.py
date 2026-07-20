"""Economy-specific terms used by Zone 2 mapping.

This is the only place where domestic/foreign geography phrases should be
maintained for mapping logic. Indicator definitions remain economy-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EconomyProfile:
    name: str
    slug: str
    domestic_terms: tuple[str, ...]
    foreign_terms: tuple[str, ...]


_PROFILES = {
    "singapore": EconomyProfile(
        name="Singapore",
        slug="singapore",
        domestic_terms=("in singapore", "within singapore"),
        foreign_terms=("outside singapore", "overseas", "foreign country or territory", "country or territory outside singapore"),
    ),
    "australia": EconomyProfile(
        name="Australia",
        slug="australia",
        domestic_terms=("in australia", "within australia", "in the commonwealth"),
        foreign_terms=("outside australia", "overseas", "foreign country"),
    ),
    "malaysia": EconomyProfile(
        name="Malaysia",
        slug="malaysia",
        domestic_terms=("in malaysia", "within malaysia", "in the federation"),
        foreign_terms=("outside malaysia", "overseas", "foreign country"),
    ),
}


def economy_profile(economy: str | None) -> EconomyProfile:
    key = (economy or "singapore").casefold().replace("_", " ").strip()
    key = {"commonwealth of australia": "australia"}.get(key, key)
    return _PROFILES.get(key, EconomyProfile(name=economy or "Singapore", slug=key or "singapore", domestic_terms=(), foreign_terms=("overseas", "foreign country")))


def domestic_terms(economy: str | None) -> tuple[str, ...]:
    return economy_profile(economy).domestic_terms


def foreign_terms(economy: str | None) -> tuple[str, ...]:
    return economy_profile(economy).foreign_terms
