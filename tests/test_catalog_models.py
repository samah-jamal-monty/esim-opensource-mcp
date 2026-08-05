"""Catalogue parsing: real backend shapes, placeholders, units and defensive behaviour."""

from __future__ import annotations

import math

import pytest

from esim_mcp.errors import InvalidBackendResponseError
from esim_mcp.models.catalog import (
    Bundle,
    Country,
    Region,
    parse_bundle,
    parse_bundles,
    parse_countries,
    parse_home_catalog,
    parse_regions,
)
from tests.conftest import (
    CATALOG_COUNTRIES,
    CATALOG_REGIONS,
    FRANCE_GUID,
    bundle_payload,
    country_payload,
    home_payload,
    region_payload,
)

# ------------------------------------------------------------------------------ countries


def test_country_payload_parses_every_documented_field() -> None:
    country = parse_countries([country_payload()])[0]

    assert country.guid == FRANCE_GUID
    assert country.name == "France"
    assert country.iso2 == "FR"
    assert country.iso3 == "FRA"
    assert country.zone == "Europe"
    assert country.operator_list == ["Orange"]


def test_country_placeholders_become_none_instead_of_being_read_out() -> None:
    """The backend substitutes 'Unknown'/'' upstream; neither is a real value."""
    country = parse_countries([country_payload(country_code="Unknown", iso3_code="", alternative_country="")])[0]

    assert country.iso2 is None
    assert country.iso3 is None
    assert country.alternative_name is None


def test_country_name_falls_back_to_a_code_when_the_name_is_missing() -> None:
    country = Country.model_validate({"id": "x", "country": "Unknown", "iso3_code": "FRA"})

    assert country.name == "FRA"


def test_alternative_country_name_is_preserved() -> None:
    countries = parse_countries(CATALOG_COUNTRIES)
    uae = next(country for country in countries if country.iso2 == "AE")

    assert uae.alternative_name == "UAE"


# -------------------------------------------------------------------------------- regions


def test_region_payload_parses() -> None:
    region = parse_regions(CATALOG_REGIONS)[0]

    assert region.code == "EUR"
    assert region.name == "Europe"
    assert region.guid


def test_region_code_is_upper_cased_for_comparison() -> None:
    region = parse_regions([region_payload(region_code="eur")])[0]

    assert region.code == "EUR"


# -------------------------------------------------------------------------------- bundles


def test_bundle_payload_parses_every_documented_field() -> None:
    bundle = parse_bundle(bundle_payload())

    assert bundle.code == "aaaaaaaa-0000-4000-8000-000000000001"
    assert bundle.name == "France 5GB / 30 Days"
    assert bundle.subtitle
    assert bundle.currency_code == "USD"
    assert bundle.price == 12.5
    assert bundle.original_price == 12.5
    assert bundle.price_text == "12.50 USD"
    assert bundle.validity == 30
    assert bundle.validity_label == "Day"
    assert bundle.plan_type == "Data only"
    assert bundle.activity_policy
    assert bundle.is_stockable is True
    assert bundle.bundle_info_code == "BND-1"
    assert bundle.is_active is True
    assert bundle.category_type == "COUNTRY"
    assert bundle.count_countries == 1
    assert bundle.countries[0].name == "France"


@pytest.mark.parametrize(
    ("gprs_limit", "data_unit", "expected_gb"),
    [
        (5.0, "GB", 5.0),
        (500.0, "MB", 500.0 / 1024.0),
        (1.0, "TB", 1024.0),
    ],
)
def test_data_allowance_uses_the_unit_from_the_display_string(
    gprs_limit: float, data_unit: str, expected_gb: float
) -> None:
    """``gprs_limit`` alone is unit-less, so a filter may never read it on its own."""
    bundle = parse_bundle(bundle_payload(gprs_limit=gprs_limit, data_unit=data_unit))

    assert bundle.data_gb == pytest.approx(expected_gb)


def test_unlimited_bundle_reports_infinite_data_and_a_readable_display() -> None:
    bundle = parse_bundle(bundle_payload(unlimited=True))

    assert bundle.is_unlimited is True
    assert math.isinf(bundle.data_gb or 0.0)
    assert bundle.data_display == "∞ Unlimited"


def test_negative_gprs_limit_alone_marks_a_bundle_unlimited() -> None:
    payload = bundle_payload()
    payload["gprs_limit"] = -1
    payload["unlimited"] = False

    assert parse_bundle(payload).is_unlimited is True


@pytest.mark.parametrize(
    ("validity", "label", "expected_days"),
    [(30, "Day", 30), (2, "Week", 14), (1, "Month", 30), (1, "Year", 365)],
)
def test_validity_is_normalized_to_days_like_the_backend_does(validity: int, label: str, expected_days: int) -> None:
    bundle = parse_bundle(bundle_payload(validity=validity, validity_label=label))

    assert bundle.validity_days == expected_days
    assert bundle.validity_text == f"{validity} {label}"


def test_missing_allowance_is_unknown_rather_than_zero() -> None:
    payload = bundle_payload()
    payload["gprs_limit"] = None
    payload["gprs_limit_display"] = "Unknown"

    bundle = parse_bundle(payload)

    assert bundle.data_gb is None
    assert bundle.data_display == "Unknown"


def test_bundle_name_falls_back_through_the_documented_name_fields() -> None:
    payload = bundle_payload()
    payload["display_title"] = ""
    payload["bundle_marketing_name"] = "Marketing name"

    assert parse_bundle(payload).name == "Marketing name"


def test_unknown_backend_fields_are_ignored_not_fatal() -> None:
    payload = bundle_payload() | {"a_brand_new_backend_field": {"nested": True}}

    assert parse_bundle(payload).code


def test_numeric_strings_are_coerced() -> None:
    payload = bundle_payload() | {"price": "9.99", "validity": "7", "gprs_limit": "3"}

    bundle = parse_bundle(payload)

    assert bundle.price == pytest.approx(9.99)
    assert bundle.validity == 7


def test_inactive_bundles_are_flagged_not_hidden_by_the_model() -> None:
    assert parse_bundle(bundle_payload(is_active=False)).is_available is False


def test_supported_ships_parse_as_named_entries() -> None:
    ships = [{"id": "s1", "country": "Symphony of the Seas", "country_code": "", "iso3_code": ""}]
    bundle = parse_bundle(bundle_payload(supported_ships=ships))

    assert [ship.name for ship in bundle.supported_ships] == ["Symphony of the Seas"]


# ----------------------------------------------------------------------------------- home


def test_home_payload_parses_all_four_sections() -> None:
    catalog = parse_home_catalog(
        home_payload(
            countries=CATALOG_COUNTRIES,
            regions=CATALOG_REGIONS,
            cruise_bundles=[bundle_payload(category_type="CRUISE")],
            global_bundles=[bundle_payload(category_type="GLOBAL")],
        )
    )

    assert len(catalog.countries) == 4
    assert len(catalog.regions) == 2
    assert len(catalog.cruise_bundles) == 1
    assert len(catalog.global_bundles) == 1


def test_home_sections_default_to_empty_when_the_backend_omits_them() -> None:
    catalog = parse_home_catalog({"countries": None, "regions": [], "cruise_bundles": None})

    assert catalog.countries == []
    assert catalog.global_bundles == []


# ---------------------------------------------------------------------- defensive parsing


@pytest.mark.parametrize("payload", ["not-json-object", 42, ["a", "list"]])
def test_a_home_response_of_the_wrong_shape_is_rejected(payload: object) -> None:
    with pytest.raises(InvalidBackendResponseError):
        parse_home_catalog(payload)


def test_a_list_response_of_the_wrong_shape_is_rejected() -> None:
    with pytest.raises(InvalidBackendResponseError):
        parse_countries({"countries": []})


def test_a_single_malformed_record_is_skipped_not_fatal() -> None:
    """One bad record must not take a whole destination's results down with it."""
    bundles = parse_bundles([bundle_payload(), "not-an-object", bundle_payload(bundle_code="b2")])

    assert [bundle.code for bundle in bundles] == ["aaaaaaaa-0000-4000-8000-000000000001", "b2"]


def test_an_empty_list_is_a_valid_answer() -> None:
    assert parse_bundles([]) == []
    assert parse_bundles(None) == []


def test_a_bundle_response_of_the_wrong_shape_is_rejected() -> None:
    with pytest.raises(InvalidBackendResponseError):
        parse_bundle(["not", "an", "object"])


def test_an_almost_empty_bundle_still_parses_with_honest_unknowns() -> None:
    bundle = Bundle.model_validate({"bundle_code": "b1"})

    assert bundle.code == "b1"
    assert bundle.name == "eSIM plan"
    assert bundle.data_display == "Unknown"
    assert bundle.validity_text == "Unknown"
    assert bundle.price_text == "Unknown"
    assert bundle.validity_days is None


def test_region_and_country_lists_tolerate_missing_optional_fields() -> None:
    assert parse_regions([{"region_code": "EUR"}])[0].name == "EUR"
    assert parse_countries([{"country": "France"}])[0].guid is None
    assert Region.model_validate({}).name == "Unknown"
