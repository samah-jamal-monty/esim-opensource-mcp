"""Read-only catalogue domain logic: identifier resolution, selection and summaries."""

from esim_mcp.catalog.resolution import CountryResolver, RegionResolver
from esim_mcp.catalog.selection import (
    DEFAULT_BUNDLE_LIMIT,
    DEFAULT_PLACE_LIMIT,
    MAX_BUNDLE_LIMIT,
    MAX_PLACE_LIMIT,
    BundleFilters,
    apply_bundle_filters,
    clamp_limit,
    sort_bundles,
)
from esim_mcp.catalog.summaries import (
    PRICE_NOTE,
    bundle_detail,
    bundle_summary,
    country_summary,
    region_summary,
)

__all__ = [
    "DEFAULT_BUNDLE_LIMIT",
    "DEFAULT_PLACE_LIMIT",
    "MAX_BUNDLE_LIMIT",
    "MAX_PLACE_LIMIT",
    "PRICE_NOTE",
    "BundleFilters",
    "CountryResolver",
    "RegionResolver",
    "apply_bundle_filters",
    "bundle_detail",
    "bundle_summary",
    "clamp_limit",
    "country_summary",
    "region_summary",
    "sort_bundles",
]
