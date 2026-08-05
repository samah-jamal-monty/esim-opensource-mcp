"""Client-side filtering, sorting and result limiting.

The backend already returns catalogue results sorted primarily by price and deduplicated
by data allowance and validity, so everything here is a *narrowing* of what the backend
returned -- never a second source of truth, and never a claim about the whole platform.

Result limits exist to protect the model's context: a destination can carry dozens of
bundles, and a conversation needs a handful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from esim_mcp.errors import InvalidInputError
from esim_mcp.models.catalog import Bundle

#: Bundle result sizes. Small by default because a chat reply lists a few options.
DEFAULT_BUNDLE_LIMIT = 5
MAX_BUNDLE_LIMIT = 20

#: Country/region result sizes. Larger, because each entry is one short line.
DEFAULT_PLACE_LIMIT = 20
MAX_PLACE_LIMIT = 100


class SortKey(StrEnum):
    """Sort orders offered to the model.

    Each maps to the order a user means when they ask for it: cheapest first for price,
    biggest allowance first for data, longest validity first for validity.
    """

    PRICE = "price"
    DATA = "data"
    VALIDITY = "validity"


@dataclass(frozen=True, slots=True)
class BundleFilters:
    """Optional narrowing requested by the user. ``None`` means "no constraint"."""

    max_price: float | None = None
    minimum_data_gb: float | None = None
    minimum_validity_days: int | None = None
    unlimited_only: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            self.max_price is None
            and self.minimum_data_gb is None
            and self.minimum_validity_days is None
            and not self.unlimited_only
        )

    def describe(self) -> list[str]:
        """Human-readable filter list, used in "nothing matched" guidance."""
        described: list[str] = []
        if self.max_price is not None:
            described.append(f"maximum price {self.max_price:g}")
        if self.minimum_data_gb is not None:
            described.append(f"at least {self.minimum_data_gb:g} GB")
        if self.minimum_validity_days is not None:
            described.append(f"at least {self.minimum_validity_days} days")
        if self.unlimited_only:
            described.append("unlimited data only")
        return described


def build_filters(
    *,
    max_price: float | None = None,
    minimum_data_gb: float | None = None,
    minimum_validity_days: int | None = None,
    unlimited_only: bool | None = None,
) -> BundleFilters:
    """Validate raw tool arguments into :class:`BundleFilters`."""
    return BundleFilters(
        max_price=_positive_number("max_price", max_price),
        minimum_data_gb=_positive_number("minimum_data_gb", minimum_data_gb),
        minimum_validity_days=_positive_int("minimum_validity_days", minimum_validity_days),
        unlimited_only=bool(unlimited_only),
    )


def apply_bundle_filters(bundles: list[Bundle], filters: BundleFilters) -> list[Bundle]:
    """Keep only the bundles that satisfy every requested constraint.

    A bundle whose allowance or validity cannot be established is excluded by the
    corresponding minimum rather than being assumed to qualify.
    """
    kept: list[Bundle] = []
    for bundle in bundles:
        if filters.unlimited_only and not bundle.is_unlimited:
            continue
        if filters.max_price is not None and (bundle.price is None or bundle.price > filters.max_price):
            continue
        if filters.minimum_data_gb is not None:
            data_gb = bundle.data_gb
            if data_gb is None or data_gb < filters.minimum_data_gb:
                continue
        if filters.minimum_validity_days is not None:
            days = bundle.validity_days
            if days is None or days < filters.minimum_validity_days:
                continue
        kept.append(bundle)
    return kept


def drop_unavailable(bundles: list[Bundle]) -> list[Bundle]:
    """Remove bundles the backend marks inactive -- they cannot be offered to a buyer."""
    return [bundle for bundle in bundles if bundle.is_available]


def sort_bundles(bundles: list[Bundle], sort_by: str | None) -> list[Bundle]:
    """Apply an explicit sort order, or preserve the backend's own price-first order."""
    if sort_by is None:
        return list(bundles)
    key = parse_sort_key(sort_by)
    if key is SortKey.PRICE:
        return sorted(bundles, key=lambda bundle: (bundle.price is None, bundle.price or 0.0))
    if key is SortKey.DATA:
        return sorted(bundles, key=_data_sort_key, reverse=True)
    return sorted(bundles, key=lambda bundle: bundle.validity_days or 0, reverse=True)


def parse_sort_key(sort_by: str | None) -> SortKey:
    """Validate a ``sort_by`` argument into a :class:`SortKey`."""
    try:
        return SortKey((sort_by or "").strip().lower())
    except ValueError:
        allowed = ", ".join(f"'{member.value}'" for member in SortKey)
        raise InvalidInputError(f"sort_by must be one of {allowed}.") from None


def clamp_limit(value: Any, *, default: int, maximum: int) -> int:
    """Resolve a requested result size into a safe one.

    An omitted limit becomes the default; an oversized one is capped rather than rejected,
    so a model asking for "all of them" still gets a usable answer.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidInputError("limit must be a whole number.")
    try:
        requested = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidInputError("limit must be a whole number.") from None
    return max(1, min(requested, maximum))


def _data_sort_key(bundle: Bundle) -> float:
    data_gb = bundle.data_gb
    if data_gb is None:
        return -1.0
    return data_gb if not math.isinf(data_gb) else float("inf")


def _positive_number(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidInputError(f"{name} must be a number.")
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidInputError(f"{name} must be a number.") from None
    if number < 0 or math.isnan(number):
        raise InvalidInputError(f"{name} must not be negative.")
    return number


def _positive_int(name: str, value: Any) -> int | None:
    number = _positive_number(name, value)
    return None if number is None else int(number)
