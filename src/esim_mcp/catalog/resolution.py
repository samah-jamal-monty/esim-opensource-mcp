"""Deterministic resolution of a user's wording to a backend catalogue identifier.

Chat users say "France", "FR", "FRA" or "Europe"; the backend wants a country **tag GUID**
or a **region code**. The mapping between the two is fetched from the backend on every
call -- it is never guessed, never cached from the model's own memory and never
fuzzy-matched into a silent choice.

Matching rules (identical in spirit for countries and regions):

* comparison is case-insensitive with collapsed whitespace, and nothing else -- no
  stemming, no edit distance, no substring matching;
* candidates are tried in a fixed priority order, and the first order that produces
  matches decides;
* exactly one match is used, several matches ask the user to choose, none produces a short
  list of real catalogue entries as suggestions.

Because suggestions and choices must survive as plain text through the MCP error channel,
they are written into the error *message* rather than a structured payload.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence

from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.errors import (
    AmbiguousCountryError,
    AmbiguousRegionError,
    CountryNotFoundError,
    InvalidInputError,
    RegionNotFoundError,
)
from esim_mcp.models.catalog import Country, Region

logger = logging.getLogger(__name__)

#: How many alternatives are offered when a query does not match anything.
MAX_SUGGESTIONS = 5
#: How many options are listed when a query matches several catalogue entries.
MAX_CHOICES = 5


def normalize(value: str | None) -> str:
    """Casefold and collapse whitespace. The only normalization that is applied."""
    return " ".join((value or "").split()).casefold()


class CountryResolver:
    """Resolves a country name or ISO code to its catalogue record."""

    def __init__(self, client: CatalogApiClient) -> None:
        self._client = client

    async def list_countries(self, *, device_id: str, locale: str) -> list[Country]:
        return await self._client.list_countries(device_id=device_id, locale=locale)

    async def resolve(self, query: str, *, device_id: str, locale: str) -> Country:
        """Return the single catalogue country the query names, or raise."""
        candidate = (query or "").strip()
        if not candidate:
            raise InvalidInputError(
                "No destination was supplied. Ask the user which country they are travelling to, then try again."
            )
        countries = await self.list_countries(device_id=device_id, locale=locale)
        return self.resolve_in(candidate, countries)

    def resolve_in(self, query: str, countries: Sequence[Country]) -> Country:
        """Pure matching step, separated from I/O so it is directly testable."""
        needle = normalize(query)
        if not needle:
            raise InvalidInputError(
                "No destination was supplied. Ask the user which country they are travelling to, then try again."
            )
        accessors: tuple[Callable[[Country], str | None], ...] = (
            lambda country: country.iso2,
            lambda country: country.iso3,
            lambda country: country.name,
            lambda country: country.alternative_name,
        )

        for accessor in accessors:
            matches = [country for country in countries if normalize(accessor(country)) == needle]
            if not matches:
                continue
            unique = _unique_by(matches, lambda country: country.guid or country.name)
            if len(unique) == 1:
                return unique[0]
            raise AmbiguousCountryError(
                f"Several countries in the catalogue match {query.strip()!r}: "
                f"{_join(country.name for country in unique[:MAX_CHOICES])}. "
                "Ask the user which one they mean, then call this tool again with that exact name."
            )

        suggestions = suggest_countries(query, countries)
        if suggestions:
            raise CountryNotFoundError(
                f"{query.strip()!r} is not a country in the eSIM catalogue. "
                f"Closest available destinations: {_join(country.name for country in suggestions)}. "
                "Ask the user whether they meant one of those."
            )
        raise CountryNotFoundError(
            f"{query.strip()!r} is not a country in the eSIM catalogue, and nothing similar is available. "
            "Ask the user for another destination, or offer regional, global or cruise plans instead."
        )


class RegionResolver:
    """Resolves a region code or region name to its catalogue record."""

    def __init__(self, client: CatalogApiClient) -> None:
        self._client = client

    async def list_regions(self, *, device_id: str, locale: str) -> list[Region]:
        return await self._client.list_regions(device_id=device_id, locale=locale)

    async def resolve(self, query: str, *, device_id: str, locale: str) -> Region:
        """Return the single catalogue region the query names, or raise."""
        candidate = (query or "").strip()
        if not candidate:
            raise InvalidInputError(
                "No region was supplied. Ask the user which region they are travelling to, then try again."
            )
        regions = await self.list_regions(device_id=device_id, locale=locale)
        return self.resolve_in(candidate, regions)

    def resolve_in(self, query: str, regions: Sequence[Region]) -> Region:
        """Pure matching step, separated from I/O so it is directly testable."""
        needle = normalize(query)
        if not needle:
            raise InvalidInputError(
                "No region was supplied. Ask the user which region they are travelling to, then try again."
            )
        accessors: tuple[Callable[[Region], str | None], ...] = (
            lambda region: region.code,
            lambda region: region.name,
        )

        for accessor in accessors:
            matches = [region for region in regions if normalize(accessor(region)) == needle]
            if not matches:
                continue
            unique = _unique_by(matches, lambda region: region.code or region.name)
            if len(unique) == 1:
                return unique[0]
            raise AmbiguousRegionError(
                f"Several regions in the catalogue match {query.strip()!r}: "
                f"{_join(region.name for region in unique[:MAX_CHOICES])}. "
                "Ask the user which one they mean, then call this tool again with that exact name."
            )

        available = _join(region.name for region in regions[:MAX_SUGGESTIONS])
        if available:
            raise RegionNotFoundError(
                f"{query.strip()!r} is not a region in the eSIM catalogue. Available regions include: {available}. "
                "Ask the user which region they meant, or offer a single country instead."
            )
        raise RegionNotFoundError()


def suggest_countries(query: str, countries: Sequence[Country], *, limit: int = MAX_SUGGESTIONS) -> list[Country]:
    """Real catalogue entries whose name contains the query, for the model to offer.

    These are *suggestions the user must confirm*, never an automatic choice, so a loose
    substring test is safe here in a way it would not be in :meth:`CountryResolver.resolve`.
    """
    needle = normalize(query)
    if len(needle) < 2:
        return []
    scored: list[tuple[int, str, Country]] = []
    for country in countries:
        haystacks = [normalize(country.name), normalize(country.alternative_name)]
        if any(haystack.startswith(needle) for haystack in haystacks if haystack):
            scored.append((0, normalize(country.name), country))
        elif any(needle in haystack for haystack in haystacks if haystack):
            scored.append((1, normalize(country.name), country))
    scored.sort(key=lambda item: (item[0], item[1]))
    return _unique_by([country for _, _, country in scored], lambda country: country.guid or country.name)[:limit]


def _unique_by[ItemT](items: Iterable[ItemT], key: Callable[[ItemT], object]) -> list[ItemT]:
    """Stable de-duplication: the backend can list one destination under several tags."""
    seen: set[object] = set()
    unique: list[ItemT] = []
    for item in items:
        marker = key(item)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(item)
    return unique


def _join(values: Iterable[str]) -> str:
    return ", ".join(value for value in values if value)
