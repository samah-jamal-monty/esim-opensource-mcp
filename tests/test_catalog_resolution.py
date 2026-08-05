"""Country and region resolution: deterministic matching, ambiguity and suggestions."""

from __future__ import annotations

import httpx
import pytest
import respx

from esim_mcp.catalog.resolution import CountryResolver, RegionResolver, suggest_countries
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.errors import (
    AmbiguousCountryError,
    AmbiguousRegionError,
    CountryNotFoundError,
    InvalidInputError,
    RegionNotFoundError,
)
from esim_mcp.models.catalog import parse_countries, parse_regions
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    FRANCE_GUID,
    LEBANON_GUID,
    UAE_GUID,
    country_payload,
    envelope,
    region_payload,
)

DEVICE_ID = "c0ffee" * 10


@pytest.fixture
def countries() -> list:
    return parse_countries(CATALOG_COUNTRIES)


@pytest.fixture
def regions() -> list:
    return parse_regions(CATALOG_REGIONS)


@pytest.fixture
def country_resolver(catalog_client: CatalogApiClient) -> CountryResolver:
    return CountryResolver(catalog_client)


@pytest.fixture
def region_resolver(catalog_client: CatalogApiClient) -> RegionResolver:
    return RegionResolver(catalog_client)


# --------------------------------------------------------------- country: exact matching


@pytest.mark.parametrize(
    ("query", "expected_guid"),
    [
        ("FR", FRANCE_GUID),
        ("fr", FRANCE_GUID),
        ("FRA", FRANCE_GUID),
        ("fra", FRANCE_GUID),
        ("France", FRANCE_GUID),
        ("france", FRANCE_GUID),
        ("  France  ", FRANCE_GUID),
        ("LB", LEBANON_GUID),
        ("LBN", LEBANON_GUID),
        ("Lebanon", LEBANON_GUID),
        ("United Arab Emirates", UAE_GUID),
        ("united  arab   emirates", UAE_GUID),
        ("UAE", UAE_GUID),
        ("ARE", UAE_GUID),
    ],
)
def test_iso2_iso3_name_and_alternative_name_all_resolve(
    country_resolver: CountryResolver, countries: list, query: str, expected_guid: str
) -> None:
    assert country_resolver.resolve_in(query, countries).guid == expected_guid


def test_iso_codes_win_over_a_name_that_happens_to_collide(country_resolver: CountryResolver) -> None:
    """Priority order is fixed: ISO2, then ISO3, then name, then alternative name."""
    catalog = parse_countries(
        [
            country_payload(guid="iso2-holder", country="Somewhere", country_code="AL", iso3_code="SWH"),
            country_payload(guid="name-holder", country="AL", country_code="ZZ", iso3_code="ZZZ"),
        ]
    )

    assert country_resolver.resolve_in("AL", catalog).guid == "iso2-holder"


def test_placeholder_codes_never_match(country_resolver: CountryResolver) -> None:
    """The backend writes 'Unknown' where the upstream had no code; it is not a country."""
    catalog = parse_countries([country_payload(country_code="Unknown", iso3_code="Unknown")])

    with pytest.raises(CountryNotFoundError):
        country_resolver.resolve_in("Unknown", catalog)


# ------------------------------------------------------------------- country: ambiguity


def test_two_countries_with_the_same_name_ask_the_user_to_choose(country_resolver: CountryResolver) -> None:
    catalog = parse_countries(
        [
            country_payload(guid="guid-1", country="Congo", country_code="CG", iso3_code="COG"),
            country_payload(guid="guid-2", country="Congo", country_code="CD", iso3_code="COD"),
        ]
    )

    with pytest.raises(AmbiguousCountryError) as excinfo:
        country_resolver.resolve_in("Congo", catalog)

    message = str(excinfo.value)
    assert "Congo" in message
    assert "which one" in message.lower()


def test_the_same_destination_listed_twice_is_not_ambiguous(country_resolver: CountryResolver) -> None:
    """De-duplication is by identity, so one destination under one tag resolves cleanly."""
    duplicate = country_payload()
    catalog = parse_countries([duplicate, dict(duplicate)])

    assert country_resolver.resolve_in("France", catalog).guid == FRANCE_GUID


# ----------------------------------------------------------------- country: not found


def test_an_unknown_country_offers_real_catalogue_suggestions(
    country_resolver: CountryResolver, countries: list
) -> None:
    with pytest.raises(CountryNotFoundError) as excinfo:
        country_resolver.resolve_in("Fren", countries)

    message = str(excinfo.value)
    assert "French Guiana" in message
    assert "meant" in message.lower()


def test_a_completely_unknown_destination_says_so_and_offers_the_alternatives(
    country_resolver: CountryResolver, countries: list
) -> None:
    with pytest.raises(CountryNotFoundError) as excinfo:
        country_resolver.resolve_in("Atlantis", countries)

    message = str(excinfo.value).lower()
    assert "not a country in the esim catalogue" in message
    assert "regional, global or cruise" in message


def test_resolution_never_silently_picks_a_near_miss(country_resolver: CountryResolver, countries: list) -> None:
    """'Franc' must not become France by itself -- the user has to confirm."""
    with pytest.raises(CountryNotFoundError):
        country_resolver.resolve_in("Franc", countries)


def test_an_empty_query_asks_for_a_destination(country_resolver: CountryResolver, countries: list) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        country_resolver.resolve_in("   ", countries)

    assert "ask the user" in str(excinfo.value).lower()


# ------------------------------------------------------------------------- suggestions


def test_suggestions_prefer_a_prefix_match_and_stay_short(countries: list) -> None:
    suggestions = suggest_countries("fr", countries)

    assert [country.name for country in suggestions] == ["France", "French Guiana"]


def test_a_substring_match_ranks_below_a_prefix_match(countries: list) -> None:
    """'guiana' is only a substring of 'French Guiana', so a prefix hit would outrank it."""
    assert [country.name for country in suggest_countries("guiana", countries)] == ["French Guiana"]


def test_a_one_character_query_produces_no_suggestions(countries: list) -> None:
    assert suggest_countries("f", countries) == []


def test_suggestions_also_look_at_the_alternative_name(countries: list) -> None:
    assert [country.name for country in suggest_countries("ua", countries)] == ["United Arab Emirates"]


# ------------------------------------------------------------------------------ regions


@pytest.mark.parametrize("query", ["EUR", "eur", "Europe", "europe", " Europe "])
def test_regions_resolve_by_code_and_by_name(region_resolver: RegionResolver, regions: list, query: str) -> None:
    assert region_resolver.resolve_in(query, regions).code == "EUR"


def test_an_unknown_region_lists_the_real_ones(region_resolver: RegionResolver, regions: list) -> None:
    with pytest.raises(RegionNotFoundError) as excinfo:
        region_resolver.resolve_in("Scandinavia", regions)

    message = str(excinfo.value)
    assert "Europe" in message
    assert "Middle East" in message


def test_two_regions_with_the_same_name_ask_the_user_to_choose(region_resolver: RegionResolver) -> None:
    catalog = parse_regions(
        [
            region_payload(region_code="EU1", region_name="Europe", guid="g1"),
            region_payload(region_code="EU2", region_name="Europe", guid="g2"),
        ]
    )

    with pytest.raises(AmbiguousRegionError):
        region_resolver.resolve_in("Europe", catalog)


def test_an_empty_region_query_asks_for_one(region_resolver: RegionResolver, regions: list) -> None:
    with pytest.raises(InvalidInputError):
        region_resolver.resolve_in("", regions)


# ------------------------------------------------------------------- resolution over HTTP


async def test_resolution_reads_the_country_list_from_the_backend(
    country_resolver: CountryResolver, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )

    resolved = await country_resolver.resolve("France", device_id=DEVICE_ID, locale="en")

    assert route.called
    assert resolved.guid == FRANCE_GUID


async def test_region_resolution_reads_the_region_list_from_the_backend(
    region_resolver: RegionResolver, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/region").mock(return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS)))

    resolved = await region_resolver.resolve("Middle East", device_id=DEVICE_ID, locale="en")

    assert resolved.code == "MEA"
