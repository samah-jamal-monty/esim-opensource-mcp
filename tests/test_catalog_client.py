"""Catalogue client: routes, headers, identifiers, retry policy and error translation."""

from __future__ import annotations

import httpx
import pytest
import respx

from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.errors import (
    AuthenticationRequiredError,
    BackendTimeoutError,
    BundleNotFoundError,
    CatalogUnavailableError,
    InvalidBackendResponseError,
    InvalidInputError,
    RateLimitedError,
    RegionNotFoundError,
)
from tests.conftest import (
    API_URL,
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    FRANCE_GUID,
    bundle_payload,
    envelope,
    home_payload,
)

DEVICE_ID = "c0ffee" * 10
BUNDLE_CODE = "aaaaaaaa-0000-4000-8000-000000000001"

# ------------------------------------------------------------------------------- routing


async def test_countries_call_hits_the_documented_route_with_device_and_locale_headers(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/countries").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
    )

    countries = await catalog_client.list_countries(device_id=DEVICE_ID, locale="fr")

    request = route.calls.last.request
    assert request.headers["X-Device-Id"] == DEVICE_ID
    assert request.headers["Accept-Language"] == "fr"
    assert "Authorization" not in request.headers
    assert [country.iso2 for country in countries] == ["FR", "LB", "AE", "GF"]


async def test_regions_call_hits_the_documented_route(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/region").mock(
        return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS))
    )

    regions = await catalog_client.list_regions(device_id=DEVICE_ID, locale="en")

    assert route.called
    assert [region.code for region in regions] == ["EUR", "MEA"]


async def test_by_country_sends_the_tag_guid_never_an_iso_code(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    """The single most important contract detail of this route."""
    route = respx_mock.get(f"{API_URL}/bundles/by-country").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    await catalog_client.list_bundles_by_country_guid(FRANCE_GUID, device_id=DEVICE_ID, locale="en", currency="EUR")

    request = route.calls.last.request
    assert request.url.params["country_codes"] == FRANCE_GUID
    assert request.headers["X-Currency"] == "EUR"
    assert "FR" not in request.url.params.get("country_codes", "").split(",")


async def test_by_region_puts_the_region_code_in_the_path(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    bundles = await catalog_client.list_bundles_by_region_code("EUR", device_id=DEVICE_ID, locale="en", currency="USD")

    assert route.called
    assert len(bundles) == 1


async def test_by_region_sends_the_region_code_exactly_as_given(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    """The backend matches region codes with ``==``, so the client may not re-case one.

    ``bundle_service.get_bundles_by_region`` selects with
    ``region.region_code == region_code`` and raises 400 "Region Not Found" when nothing
    matches, so a client that upper-cases a mixed-case code turns a real region into an
    error.
    """
    route = respx_mock.get(f"{API_URL}/bundles/by-region/Nordics").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    await catalog_client.list_bundles_by_region_code("Nordics", device_id=DEVICE_ID, locale="en", currency="USD")

    assert route.called
    assert route.calls.last.request.url.path.endswith("/bundles/by-region/Nordics")


async def test_by_region_sends_the_headers_the_route_declares_and_no_pagination(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    """``GET /bundles/by-region/{region_code}`` declares no paging parameter at all.

    Its only inputs are the path code and the ``x-device-id`` / ``accept-language`` /
    ``x-currency`` headers; the whole list comes back in one response, so sending a page or
    limit parameter would be inventing a contract the backend does not have.
    """
    route = respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(200, json=envelope([bundle_payload()]))
    )

    await catalog_client.list_bundles_by_region_code("EUR", device_id=DEVICE_ID, locale="fr", currency="EUR")

    request = route.calls.last.request
    assert dict(request.url.params) == {}
    assert request.headers["X-Device-Id"] == DEVICE_ID
    assert request.headers["Accept-Language"] == "fr"
    assert request.headers["X-Currency"] == "EUR"
    assert "Authorization" not in request.headers


async def test_a_region_the_backend_rejects_never_reads_as_an_empty_catalogue(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    """A 400 is an error, and the message must forbid reporting it as "no plans"."""
    respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(
            400,
            json=envelope(None, status="failed", title="Request failed. Please try again.", response_code=400),
        )
    )

    with pytest.raises(RegionNotFoundError) as excinfo:
        await catalog_client.list_bundles_by_region_code("EUR", device_id=DEVICE_ID, locale="en", currency="USD")

    message = str(excinfo.value).lower()
    assert "never tell the user there are" in message
    assert "not a statement that the region has no plans" in message


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, AuthenticationRequiredError),
        (403, AuthenticationRequiredError),
        (404, RegionNotFoundError),
        (429, RateLimitedError),
        (500, CatalogUnavailableError),
    ],
)
async def test_a_failing_region_search_always_raises_and_never_returns_no_bundles(
    catalog_client: CatalogApiClient,
    respx_mock: respx.Router,
    status_code: int,
    expected: type[Exception],
) -> None:
    """Every backend failure is an exception. None of them can be mistaken for an empty list."""
    respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(
            status_code, json=envelope(None, status="failed", title="Failed", response_code=status_code)
        )
    )

    with pytest.raises(expected):
        await catalog_client.list_bundles_by_region_code("EUR", device_id=DEVICE_ID, locale="en", currency="USD")


async def test_a_region_with_no_bundles_comes_back_as_a_genuine_empty_list(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    """A valid region with nothing to sell answers 200 with ``data: []``."""
    respx_mock.get(f"{API_URL}/bundles/by-region/MEA").mock(
        return_value=httpx.Response(200, json=envelope([], total_count=0))
    )

    bundles = await catalog_client.list_bundles_by_region_code("MEA", device_id=DEVICE_ID, locale="en", currency="USD")

    assert bundles == []


async def test_bundle_details_puts_the_bundle_code_in_the_path(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload()))
    )

    bundle = await catalog_client.get_bundle_details(BUNDLE_CODE, device_id=DEVICE_ID, locale="en", currency="USD")

    assert route.called
    assert bundle.code == BUNDLE_CODE


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/home/", "get_home_catalog"),
        ("/home/cruise", "get_cruise_catalog"),
        ("/home/land", "get_land_catalog"),
    ],
)
async def test_home_routes_send_locale_and_currency(
    catalog_client: CatalogApiClient, respx_mock: respx.Router, path: str, method: str
) -> None:
    route = respx_mock.get(f"{API_URL}{path}").mock(return_value=httpx.Response(200, json=envelope(home_payload())))

    await getattr(catalog_client, method)(device_id=DEVICE_ID, locale="ar", currency="AED")

    request = route.calls.last.request
    assert request.headers["Accept-Language"] == "ar"
    assert request.headers["X-Currency"] == "AED"
    assert request.headers["X-Device-Id"] == DEVICE_ID


async def test_cruise_route_returns_the_cruise_section(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/home/cruise").mock(
        return_value=httpx.Response(
            200, json=envelope(home_payload(cruise_bundles=[bundle_payload(category_type="CRUISE")]))
        )
    )

    catalog = await catalog_client.get_cruise_catalog(device_id=DEVICE_ID, locale="en", currency="USD")

    assert len(catalog.cruise_bundles) == 1
    assert catalog.cruise_bundles[0].category_type == "CRUISE"


# ------------------------------------------------------------------------- no login used


async def test_no_catalogue_call_ever_sends_a_token(catalog_client: CatalogApiClient, respx_mock: respx.Router) -> None:
    """Catalogue browsing is login-free; there is no code path that attaches credentials."""
    routes = [
        respx_mock.get(f"{API_URL}/home/").mock(return_value=httpx.Response(200, json=envelope(home_payload()))),
        respx_mock.get(f"{API_URL}/bundles/countries").mock(
            return_value=httpx.Response(200, json=envelope(CATALOG_COUNTRIES))
        ),
        respx_mock.get(f"{API_URL}/bundles/region").mock(
            return_value=httpx.Response(200, json=envelope(CATALOG_REGIONS))
        ),
        respx_mock.get(f"{API_URL}/bundles/by-country").mock(
            return_value=httpx.Response(200, json=envelope([bundle_payload()]))
        ),
    ]
    await catalog_client.get_home_catalog(device_id=DEVICE_ID, locale="en", currency="USD")
    await catalog_client.list_countries(device_id=DEVICE_ID, locale="en")
    await catalog_client.list_regions(device_id=DEVICE_ID, locale="en")
    await catalog_client.list_bundles_by_country_guid(FRANCE_GUID, device_id=DEVICE_ID, locale="en", currency="USD")

    for route in routes:
        for call in route.calls:
            assert "Authorization" not in call.request.headers
            assert "X-Refresh-Token" not in call.request.headers


# ------------------------------------------------------------------------ retry and errors


async def test_catalogue_reads_retry_within_the_existing_bounded_policy(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/countries").mock(
        side_effect=[
            httpx.Response(503, json=envelope(None, status="failed", response_code=503)),
            httpx.Response(200, json=envelope(CATALOG_COUNTRIES)),
        ]
    )

    countries = await catalog_client.list_countries(device_id=DEVICE_ID, locale="en")

    assert route.call_count == 2
    assert len(countries) == 4


async def test_a_persistently_failing_catalogue_reports_it_as_unavailable(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    route = respx_mock.get(f"{API_URL}/home/").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(CatalogUnavailableError) as excinfo:
        await catalog_client.get_home_catalog(device_id=DEVICE_ID, locale="en", currency="USD")

    assert route.call_count == 3
    assert "try again" in str(excinfo.value).lower()


async def test_a_catalogue_timeout_is_reported_as_a_timeout(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/countries").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(BackendTimeoutError):
        await catalog_client.list_countries(device_id=DEVICE_ID, locale="en")


async def test_an_unknown_region_code_is_reported_as_region_not_found(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/by-region/XX").mock(
        return_value=httpx.Response(
            400, json=envelope(None, status="failed", title="Region Not Found", response_code=400)
        )
    )

    with pytest.raises(RegionNotFoundError):
        await catalog_client.list_bundles_by_region_code("XX", device_id=DEVICE_ID, locale="en", currency="USD")


async def test_an_unknown_bundle_code_is_reported_as_bundle_not_found(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/does-not-exist").mock(
        return_value=httpx.Response(404, json=envelope(None, status="failed", title="Not Found", response_code=404))
    )

    with pytest.raises(BundleNotFoundError) as excinfo:
        await catalog_client.get_bundle_details("does-not-exist", device_id=DEVICE_ID, locale="en", currency="USD")

    assert "never invent" in str(excinfo.value).lower()


async def test_an_empty_bundle_body_is_reported_as_not_found(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/{BUNDLE_CODE}").mock(return_value=httpx.Response(200, json=envelope(None)))

    with pytest.raises(BundleNotFoundError):
        await catalog_client.get_bundle_details(BUNDLE_CODE, device_id=DEVICE_ID, locale="en", currency="USD")


async def test_a_non_json_catalogue_response_is_translated(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/countries").mock(return_value=httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(InvalidBackendResponseError):
        await catalog_client.list_countries(device_id=DEVICE_ID, locale="en")


async def test_a_developer_message_never_reaches_the_caller(
    catalog_client: CatalogApiClient, respx_mock: respx.Router
) -> None:
    respx_mock.get(f"{API_URL}/bundles/by-region/EUR").mock(
        return_value=httpx.Response(
            500,
            json=envelope(
                None,
                status="failed",
                title="Exception",
                developer_message="Traceback: psycopg2 connection refused at 10.0.0.4",
                response_code=500,
            ),
        )
    )

    with pytest.raises(CatalogUnavailableError) as excinfo:
        await catalog_client.list_bundles_by_region_code("EUR", device_id=DEVICE_ID, locale="en", currency="USD")

    message = str(excinfo.value)
    assert "psycopg2" not in message
    assert "10.0.0.4" not in message
    assert "Traceback" not in message


# ------------------------------------------------------------------ identifier validation


@pytest.mark.parametrize("bad", ["", "   ", "../auth/user-info", "a/b"])
async def test_empty_or_path_bending_identifiers_are_refused_before_any_request(
    catalog_client: CatalogApiClient, respx_mock: respx.Router, bad: str
) -> None:
    with pytest.raises(InvalidInputError):
        await catalog_client.get_bundle_details(bad, device_id=DEVICE_ID, locale="en", currency="USD")

    assert not respx_mock.calls


async def test_bundle_codes_are_url_encoded(catalog_client: CatalogApiClient, respx_mock: respx.Router) -> None:
    route = respx_mock.get(f"{API_URL}/bundles/odd%20code").mock(
        return_value=httpx.Response(200, json=envelope(bundle_payload()))
    )

    await catalog_client.get_bundle_details("odd code", device_id=DEVICE_ID, locale="en", currency="USD")

    assert route.called
