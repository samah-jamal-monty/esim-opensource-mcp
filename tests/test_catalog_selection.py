"""Client-side filtering, sorting and result limiting."""

from __future__ import annotations

import pytest

from esim_mcp.catalog.selection import (
    DEFAULT_BUNDLE_LIMIT,
    DEFAULT_PLACE_LIMIT,
    MAX_BUNDLE_LIMIT,
    MAX_PLACE_LIMIT,
    BundleFilters,
    apply_bundle_filters,
    build_filters,
    clamp_limit,
    drop_unavailable,
    parse_sort_key,
    sort_bundles,
)
from esim_mcp.errors import InvalidInputError
from esim_mcp.models.catalog import parse_bundles
from tests.conftest import bundle_payload


@pytest.fixture
def bundles() -> list:
    return parse_bundles(
        [
            bundle_payload(bundle_code="cheap", gprs_limit=1.0, price=5.0, validity=7),
            bundle_payload(bundle_code="mid", gprs_limit=5.0, price=12.5, validity=30),
            bundle_payload(bundle_code="big", gprs_limit=20.0, price=30.0, validity=30),
            bundle_payload(bundle_code="unlimited", unlimited=True, price=45.0, validity=15),
            bundle_payload(bundle_code="small-mb", gprs_limit=500.0, data_unit="MB", price=3.0, validity=3),
        ]
    )


def codes(items: list) -> list[str]:
    return [item.code for item in items]


# ------------------------------------------------------------------------------- filters


def test_no_filters_keeps_everything(bundles: list) -> None:
    assert apply_bundle_filters(bundles, BundleFilters()) == bundles


def test_max_price_excludes_anything_dearer(bundles: list) -> None:
    kept = apply_bundle_filters(bundles, build_filters(max_price=12.5))

    assert codes(kept) == ["cheap", "mid", "small-mb"]


def test_minimum_data_compares_in_gb_across_units(bundles: list) -> None:
    """500 MB must not pass a 5 GB minimum just because 500 > 5."""
    kept = apply_bundle_filters(bundles, build_filters(minimum_data_gb=5))

    assert codes(kept) == ["mid", "big", "unlimited"]


def test_unlimited_plans_satisfy_any_minimum_data(bundles: list) -> None:
    kept = apply_bundle_filters(bundles, build_filters(minimum_data_gb=1000))

    assert codes(kept) == ["unlimited"]


def test_unlimited_only_keeps_unlimited_plans(bundles: list) -> None:
    kept = apply_bundle_filters(bundles, build_filters(unlimited_only=True))

    assert codes(kept) == ["unlimited"]


def test_minimum_validity_uses_normalized_days(bundles: list) -> None:
    kept = apply_bundle_filters(bundles, build_filters(minimum_validity_days=15))

    assert codes(kept) == ["mid", "big", "unlimited"]


def test_filters_combine(bundles: list) -> None:
    kept = apply_bundle_filters(bundles, build_filters(minimum_data_gb=5, max_price=30, minimum_validity_days=30))

    assert codes(kept) == ["mid", "big"]


def test_a_bundle_with_an_unknown_allowance_is_excluded_rather_than_assumed(bundles: list) -> None:
    payload = bundle_payload(bundle_code="mystery")
    payload["gprs_limit"] = None
    payload["gprs_limit_display"] = "Unknown"
    unknown = parse_bundles([payload])

    assert apply_bundle_filters(unknown, build_filters(minimum_data_gb=1)) == []


def test_a_bundle_with_no_price_is_excluded_by_a_budget(bundles: list) -> None:
    payload = bundle_payload(bundle_code="no-price")
    payload["price"] = None
    priceless = parse_bundles([payload])

    assert apply_bundle_filters(priceless, build_filters(max_price=100)) == []


def test_filters_describe_themselves_for_the_model() -> None:
    described = build_filters(max_price=20, minimum_data_gb=5, minimum_validity_days=30, unlimited_only=True).describe()

    assert described == ["maximum price 20", "at least 5 GB", "at least 30 days", "unlimited data only"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_price": "cheap"}, "max_price must be a number."),
        ({"max_price": -1}, "max_price must not be negative."),
        ({"minimum_data_gb": "lots"}, "minimum_data_gb must be a number."),
        ({"minimum_validity_days": -5}, "minimum_validity_days must not be negative."),
    ],
)
def test_invalid_filter_arguments_are_rejected_safely(kwargs: dict, message: str) -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        build_filters(**kwargs)

    assert message in str(excinfo.value)


def test_inactive_bundles_are_dropped_before_anything_is_offered() -> None:
    catalog = parse_bundles([bundle_payload(bundle_code="live"), bundle_payload(bundle_code="dead", is_active=False)])

    assert codes(drop_unavailable(catalog)) == ["live"]


# ------------------------------------------------------------------------------- sorting


def test_no_sort_key_preserves_the_backend_order(bundles: list) -> None:
    assert sort_bundles(bundles, None) == bundles


def test_price_sorts_cheapest_first(bundles: list) -> None:
    assert codes(sort_bundles(bundles, "price")) == ["small-mb", "cheap", "mid", "big", "unlimited"]


def test_data_sorts_the_largest_allowance_first_with_unlimited_on_top(bundles: list) -> None:
    assert codes(sort_bundles(bundles, "data")) == ["unlimited", "big", "mid", "cheap", "small-mb"]


def test_validity_sorts_the_longest_first(bundles: list) -> None:
    assert codes(sort_bundles(bundles, "validity"))[:2] == ["mid", "big"]
    assert codes(sort_bundles(bundles, "validity"))[-1] == "small-mb"


@pytest.mark.parametrize("value", ["PRICE", " price ", "Data", "validity"])
def test_sort_keys_are_case_and_space_insensitive(value: str) -> None:
    assert parse_sort_key(value)


def test_an_unknown_sort_key_is_rejected_with_the_allowed_values() -> None:
    with pytest.raises(InvalidInputError) as excinfo:
        parse_sort_key("cheapness")

    message = str(excinfo.value)
    assert "'price'" in message and "'data'" in message and "'validity'" in message


# -------------------------------------------------------------------------------- limits


def test_an_omitted_limit_uses_the_default() -> None:
    assert clamp_limit(None, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT) == 5
    assert clamp_limit(None, default=DEFAULT_PLACE_LIMIT, maximum=MAX_PLACE_LIMIT) == 20


def test_an_oversized_limit_is_capped_rather_than_rejected() -> None:
    assert clamp_limit(500, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT) == MAX_BUNDLE_LIMIT
    assert clamp_limit(9999, default=DEFAULT_PLACE_LIMIT, maximum=MAX_PLACE_LIMIT) == MAX_PLACE_LIMIT


def test_a_zero_or_negative_limit_still_returns_something() -> None:
    assert clamp_limit(0, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT) == 1
    assert clamp_limit(-3, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT) == 1


def test_a_non_numeric_limit_is_rejected_safely() -> None:
    with pytest.raises(InvalidInputError):
        clamp_limit("many", default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT)


def test_the_documented_limits_are_the_ones_the_specification_asks_for() -> None:
    assert (DEFAULT_BUNDLE_LIMIT, MAX_BUNDLE_LIMIT) == (5, 20)
