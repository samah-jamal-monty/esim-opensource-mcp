"""Catalogue tools: what the model actually receives, and what it can never be told."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from esim_mcp.catalog.summaries import PRICE_NOTE
from esim_mcp.errors import (
    CountryNotFoundError,
    InvalidInputError,
    NoMatchingBundlesError,
    RegionNotFoundError,
)
from esim_mcp.tools.catalog import CatalogService
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    FRANCE_GUID,
    bundle_payload,
    country_payload,
    envelope,
    home_payload,
)

BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"


def mock_countries(respx_mock: respx.Router) -> respx.Route:
    return respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )


def mock_regions(respx_mock: respx.Router) -> respx.Route:
    return respx_mock.get(f"{API_URL}/bundles/region").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS))
    )


def mock_country_bundles(respx_mock: respx.Router, bundles: list) -> respx.Route:
    return respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(200, json=envelope(bundles))
    )


FRANCE_BUNDLES = [
    bundle_payload(bundle_code="b-1gb", gprs_limit=1.0, price=5.0, validity=7),
    bundle_payload(bundle_code="b-5gb", gprs_limit=5.0, price=12.5, validity=30),
    bundle_payload(bundle_code="b-10gb", gprs_limit=10.0, price=20.0, validity=30),
    bundle_payload(bundle_code="b-20gb", gprs_limit=20.0, price=30.0, validity=30),
    bundle_payload(bundle_code="b-unl", unlimited=True, price=45.0, validity=15),
    bundle_payload(bundle_code="b-50gb", gprs_limit=50.0, price=60.0, validity=90),
]


# -------------------------------------------------------------------------- no login


async def test_browsing_never_requires_a_session(catalog_service: CatalogService, respx_mock: respx.Router) -> None:
    """The service is built without a session manager at all: browsing cannot need one."""
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(country="France")

    assert result["status"] == "ok"
    assert not any("auth" in call.request.url.path for call in respx_mock.calls)


async def test_no_catalogue_tool_calls_an_order_purchase_or_callback_route(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_regions(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)
    respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope(FRANCE_BUNDLES))
    )
    respx_mock.get(f"{API_URL}/home/").mock(return_value=httpx.Response(200, json=envelope(home_payload())))
    respx_mock.get(f"{API_URL}/home/cruise").mock(return_value=httpx.Response(200, json=envelope(home_payload())))
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload()))
    )

    await catalog_service.list_countries()
    await catalog_service.list_regions()
    await catalog_service.browse_home_catalog()
    await catalog_service.find_bundles_by_country(country="France")
    await catalog_service.find_bundles_by_region(region="Europe")
    await catalog_service.list_cruise_bundles()
    await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE)

    forbidden = (
        "order",
        "purchase",
        "checkout",
        "payment",
        "stripe",
        "wallet",
        "voucher",
        "promo",
        "callback",
        "translate",
        "top-up",
        "topup",
        "consumption",
        "assign",
    )
    for call in respx_mock.calls:
        path = call.request.url.path.lower()
        assert call.request.method == "GET", path
        for word in forbidden:
            assert word not in path, f"catalogue call touched {path}"


# ------------------------------------------------------------------------ list_countries


async def test_list_countries_returns_a_limited_alphabetical_extract_plus_the_total(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)

    result = await catalog_service.list_countries(limit=2)

    assert result["total_count"] == 4
    assert result["returned_count"] == 2
    assert [entry["country"] for entry in result["countries"]] == ["France", "French Guiana"]
    assert result["match"] == "browse"


async def test_list_countries_resolves_an_exact_query(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)

    result = await catalog_service.list_countries(query="ARE")

    assert result["match"] == "exact"
    assert result["countries"] == [{"country": "United Arab Emirates", "country_code": "AE", "iso3_code": "ARE"}]


async def test_list_countries_offers_suggestions_for_a_near_miss(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)

    result = await catalog_service.list_countries(query="Fren")

    assert result["match"] == "suggestions"
    assert [entry["country"] for entry in result["countries"]] == ["French Guiana"]


async def test_list_countries_says_plainly_when_nothing_matches(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)

    result = await catalog_service.list_countries(query="Atlantis")

    assert result["match"] == "none"
    assert result["countries"] == []
    assert "another one" in result["note"]


async def test_country_results_never_carry_the_backend_tag_guid(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    """The GUID is an internal identifier: the model has no use for it and may not show it."""
    mock_countries(respx_mock)

    rendered = json.dumps(await catalog_service.list_countries())

    assert FRANCE_GUID not in rendered
    assert "icon" not in rendered


# -------------------------------------------------------------------------- list_regions


async def test_list_regions_returns_codes_and_names(catalog_service: CatalogService, respx_mock: respx.Router) -> None:
    mock_regions(respx_mock)

    result = await catalog_service.list_regions()

    assert result["regions"] == [
        {"region_name": "Europe", "region_code": "EUR"},
        {"region_name": "Middle East", "region_code": "MEA"},
    ]
    assert result["total_count"] == 2


async def test_list_regions_falls_back_to_the_real_regions_when_nothing_matches(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_regions(respx_mock)

    result = await catalog_service.list_regions(query="Scandinavia")

    assert result["match"] == "none"
    assert len(result["regions"]) == 2
    assert "ask the user to pick one" in result["note"].lower()


# -------------------------------------------------------------------- browse_home_catalog


async def test_home_overview_returns_counts_and_small_previews(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/home/").mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                home_payload(
                    countries=CATALOG_COUNTRIES,
                    regions=CATALOG_REGIONS,
                    cruise_bundles=[bundle_payload(bundle_code=f"c{index}") for index in range(9)],
                    global_bundles=[bundle_payload(bundle_code=f"g{index}") for index in range(7)],
                )
            ),
        )
    )

    result = await catalog_service.browse_home_catalog()

    assert result["countries"]["total_count"] == 4
    assert result["regions"]["total_count"] == 2
    assert result["cruise_bundles"]["total_count"] == 9
    assert result["cruise_bundles"]["returned_count"] == 5
    assert len(result["cruise_bundles"]["bundles"]) == 5
    assert result["global_bundles"]["total_count"] == 7
    assert len(result["global_bundles"]["bundles"]) == 5


async def test_home_overview_states_that_it_is_not_every_plan(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/home/").mock(return_value=httpx.Response(200, json=envelope(home_payload())))

    result = await catalog_service.browse_home_catalog()

    note = result["note"].lower()
    assert "not every plan" in note
    assert "no way to list every plan" in note
    assert "which country or region" in note
    assert result["price_note"] == PRICE_NOTE


async def test_home_overview_does_not_return_the_raw_backend_response(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/home/").mock(
        return_value=httpx.Response(
            200, json=envelope(home_payload(countries=CATALOG_COUNTRIES), developer_message="internal")
        )
    )

    rendered = json.dumps(await catalog_service.browse_home_catalog())

    for leaked in ("developerMessage", "totalCount", "responseCode", "operator_list", "zone_name", "icon"):
        assert leaked not in rendered


# ------------------------------------------------------------- find_bundles_by_country


async def test_country_search_resolves_the_guid_and_sends_it_to_the_backend(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    route = mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(country="france")

    assert route.calls.last.request.url.params["country_codes"] == FRANCE_GUID
    assert result["country"] == {"country": "France", "country_code": "FR", "iso3_code": "FRA"}


async def test_country_search_defaults_to_five_results_and_reports_both_counts(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(country="FR")

    assert result["total_count"] == 6
    assert result["returned_count"] == 5
    assert result["more_available"] is True


async def test_country_search_caps_an_oversized_limit(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, [bundle_payload(bundle_code=f"b{index}") for index in range(40)])

    result = await catalog_service.find_bundles_by_country(country="FR", limit=100)

    assert result["returned_count"] == 20


async def test_bundle_summaries_are_conversational_not_raw_records(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, [bundle_payload()])

    summary = (await catalog_service.find_bundles_by_country(country="FR"))["bundles"][0]

    assert summary["bundle_code"] == BUNDLE_CODE
    assert summary["name"] == "France 5GB / 30 Days"
    assert summary["data"] == "5.0 GB"
    assert summary["validity"] == "30 Day"
    assert summary["price"] == "12.50 USD"
    assert summary["unlimited"] is False
    assert summary["coverage"] == {"countries_count": 1, "countries": ["France"]}
    for noisy in ("icon", "bundle_info_code", "is_stockable", "display_subtitle", "activity_policy"):
        assert noisy not in summary


async def test_country_results_carry_the_tax_note_and_a_scope_that_is_not_the_whole_platform(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(country="FR")

    assert result["price_note"] == PRICE_NOTE
    assert "not the platform's entire catalogue" in result["note"]


async def test_filters_and_sorting_reach_the_result(catalog_service: CatalogService, respx_mock: respx.Router) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(
        country="FR", minimum_data_gb=5, max_price=30, sort_by="price", limit=10
    )

    assert [bundle["bundle_code"] for bundle in result["bundles"]] == ["b-5gb", "b-10gb", "b-20gb"]
    assert result["total_count"] == 3
    assert result["total_available"] == 6
    assert result["filters_applied"] == ["maximum price 30", "at least 5 GB"]


async def test_unlimited_only_narrows_to_unlimited_plans(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    result = await catalog_service.find_bundles_by_country(country="FR", unlimited_only=True)

    assert [bundle["bundle_code"] for bundle in result["bundles"]] == ["b-unl"]
    assert result["bundles"][0]["unlimited"] is True


async def test_filters_that_match_nothing_ask_the_user_to_relax_them(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    with pytest.raises(NoMatchingBundlesError) as excinfo:
        await catalog_service.find_bundles_by_country(country="FR", max_price=1)

    message = str(excinfo.value)
    assert "maximum price 1" in message
    assert "relax" in message.lower()


async def test_a_destination_with_no_plans_is_reported_plainly_not_as_an_error(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, [])

    result = await catalog_service.find_bundles_by_country(country="Lebanon")

    assert result["total_count"] == 0
    assert result["returned_count"] == 0
    assert result["bundles"] == []
    assert "no plans for Lebanon" in result["note"]
    assert "regional plan or a global plan" in result["note"]


async def test_inactive_plans_are_never_offered(catalog_service: CatalogService, respx_mock: respx.Router) -> None:
    mock_countries(respx_mock)
    mock_country_bundles(
        respx_mock,
        [bundle_payload(bundle_code="live"), bundle_payload(bundle_code="withdrawn", is_active=False)],
    )

    result = await catalog_service.find_bundles_by_country(country="FR")

    assert [bundle["bundle_code"] for bundle in result["bundles"]] == ["live"]


async def test_an_unknown_country_never_reaches_the_bundle_route(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    bundles_route = mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    with pytest.raises(CountryNotFoundError):
        await catalog_service.find_bundles_by_country(country="Atlantis")

    assert bundles_route.call_count == 0


async def test_an_invalid_sort_key_is_rejected_before_any_backend_call(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    with pytest.raises(InvalidInputError):
        await catalog_service.find_bundles_by_country(country="FR", sort_by="cheapness")

    assert not respx_mock.calls


async def test_locale_and_currency_reach_the_backend(catalog_service: CatalogService, respx_mock: respx.Router) -> None:
    countries_route = mock_countries(respx_mock)
    bundles_route = mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    await catalog_service.find_bundles_by_country(country="FR", locale="fr", currency="eur")

    assert countries_route.calls.last.request.headers["Accept-Language"] == "fr"
    bundle_request = bundles_route.calls.last.request
    assert bundle_request.headers["Accept-Language"] == "fr"
    assert bundle_request.headers["X-Currency"] == "EUR"
    assert len(bundle_request.headers["X-Device-Id"]) == 64


async def test_the_default_locale_and_currency_are_used_when_none_is_given(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_countries(respx_mock)
    route = mock_country_bundles(respx_mock, FRANCE_BUNDLES)

    await catalog_service.find_bundles_by_country(country="FR")

    request = route.calls.last.request
    assert request.headers["Accept-Language"] == "en"
    assert request.headers["X-Currency"] == "USD"


# -------------------------------------------------------------- find_bundles_by_region


async def test_region_search_sends_the_region_code_in_the_path(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_regions(respx_mock)
    route = respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope(FRANCE_BUNDLES))
    )

    result = await catalog_service.find_bundles_by_region(region="Europe")

    assert route.called
    assert result["region"] == {"region_name": "Europe", "region_code": "EUR"}
    assert "not the platform's entire catalogue" in result["note"]


async def test_region_search_supports_the_cheapest_first_request(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    """ "Show me the cheapest 5GB plan for Europe"."""
    mock_regions(respx_mock)
    respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope(FRANCE_BUNDLES))
    )

    result = await catalog_service.find_bundles_by_region(region="EUR", minimum_data_gb=5, sort_by="price", limit=1)

    assert [bundle["bundle_code"] for bundle in result["bundles"]] == ["b-5gb"]
    assert result["returned_count"] == 1
    # Five plans carry at least 5 GB (including the unlimited one); one is shown.
    assert result["total_count"] == 5
    assert result["more_available"] is True


async def test_an_unknown_region_never_reaches_the_bundle_route(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    mock_regions(respx_mock)
    route = respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope(FRANCE_BUNDLES))
    )

    with pytest.raises(RegionNotFoundError):
        await catalog_service.find_bundles_by_region(region="Scandinavia")

    assert route.call_count == 0


# ------------------------------------------------------------------ list_cruise_bundles


async def test_cruise_listing_uses_the_cruise_route_and_reports_ships(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    ships = [
        {"id": "s1", "country": "Symphony of the Seas"},
        {"id": "s2", "country": "Wonder of the Seas"},
    ]
    route = respx_mock.get(f"{API_URL}/home/cruise").mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                home_payload(
                    cruise_bundles=[
                        bundle_payload(bundle_code="cruise-1", category_type="CRUISE", supported_ships=ships)
                    ]
                )
            ),
        )
    )

    result = await catalog_service.list_cruise_bundles()

    assert route.called
    assert result["bundles"][0]["ships_count"] == 2
    assert result["destination"] == "cruise ships"
    assert "cruise plans only" in result["note"].lower()


async def test_cruise_listing_is_limited_like_every_other_bundle_result(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/home/cruise").mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                home_payload(cruise_bundles=[bundle_payload(bundle_code=f"c{index}") for index in range(12)])
            ),
        )
    )

    result = await catalog_service.list_cruise_bundles()

    assert result["total_count"] == 12
    assert result["returned_count"] == 5


# ------------------------------------------------------------------- get_bundle_details


async def test_bundle_details_return_everything_a_buyer_asks_about(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    coverage = [country_payload(), country_payload(guid="g2", country="Germany", country_code="DE", iso3_code="DEU")]
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(countries=coverage)))
    )

    bundle = (await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE))["bundle"]

    assert bundle["bundle_code"] == BUNDLE_CODE
    assert bundle["name"] == "France 5GB / 30 Days"
    assert bundle["data"] == "5.0 GB"
    assert bundle["unlimited"] is False
    assert bundle["validity"] == "30 Day"
    assert bundle["validity_days"] == 30
    assert bundle["price"] == "12.50 USD"
    assert bundle["currency"] == "USD"
    assert bundle["plan_type"] == "Data only"
    assert bundle["activation_policy"].startswith("The validity period starts")
    assert bundle["available"] is True
    assert bundle["coverage"] == {"countries_count": 2, "countries": ["France", "Germany"]}
    assert bundle["description"]


async def test_bundle_details_always_carry_the_tax_note(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload()))
    )

    result = await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE)

    assert result["bundle"]["price_note"] == PRICE_NOTE
    assert "do not read the bundle code out" in result["note"].lower()


async def test_bundle_details_name_the_supported_ships(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    ships = [{"id": f"s{index}", "country": f"Ship {index}"} for index in range(25)]
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(supported_ships=ships)))
    )

    bundle = (await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE))["bundle"]

    assert bundle["supported_ships"]["total_count"] == 25
    assert len(bundle["supported_ships"]["ships"]) == 20


async def test_bundle_details_bound_a_very_large_coverage_list(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    coverage = [country_payload(guid=f"g{index}", country=f"Country {index}") for index in range(120)]
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(countries=coverage)))
    )

    bundle = (await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE))["bundle"]

    assert bundle["coverage"]["countries_count"] == 120
    assert len(bundle["coverage"]["countries"]) == 30
    assert bundle["coverage"]["countries_truncated"] is True


async def test_details_report_an_unavailable_plan_honestly(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(is_active=False)))
    )

    bundle = (await catalog_service.get_bundle_details(bundle_code=BUNDLE_CODE))["bundle"]

    assert bundle["available"] is False


async def test_a_selected_option_can_be_looked_up_by_its_own_bundle_code(
    catalog_service: CatalogService, respx_mock: respx.Router
) -> None:
    """The "give me the second one" flow: the code from a listing resolves to that plan."""
    mock_countries(respx_mock)
    mock_country_bundles(respx_mock, FRANCE_BUNDLES)
    listing = await catalog_service.find_bundles_by_country(country="FR")
    second_code = listing["bundles"][1]["bundle_code"]
    detail_route = respx_mock.get(f"{API_URL}/bundles/{second_code}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload(bundle_code=second_code)))
    )

    detail = await catalog_service.get_bundle_details(bundle_code=second_code)

    assert detail_route.called
    assert detail["bundle"]["bundle_code"] == second_code
