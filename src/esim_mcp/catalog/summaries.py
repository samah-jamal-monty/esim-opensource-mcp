"""Conversational summaries of catalogue records.

Tool results are what the model reads, so they are deliberately *small*: a backend bundle
carries icons, marketing copy, category codes, region objects and a full country list, and
almost none of that helps a chat reply. What survives is what a user is actually asked to
choose between -- name, data, validity, price -- plus the ``bundle_code`` the model needs
to look one up again later.

Nothing here invents a value. A field that the backend did not supply is omitted rather
than defaulted, so the model can never read out a price, an allowance or a validity that
did not come from the platform.
"""

from __future__ import annotations

from typing import Any

from esim_mcp.models.catalog import Bundle, Country, Region

#: Required wording whenever a price is shown. This is the platform's catalogue price and it
#: is the only figure this server has: the platform states nothing about tax, surcharges or
#: adjustments here, so neither does this note. It used to promise that the amount "may not
#: include final tax" and "will be confirmed before purchase" -- two claims the platform
#: never made, which the assistant then repeated to users as fact.
PRICE_NOTE = (
    "The platform's price for this plan. Quote it to the user as it stands, without attaching caveats the "
    "platform never made: no warning about tax, and no suggestion that the figure is provisional."
)

#: How much country coverage a *summary* shows before falling back to a count.
COVERAGE_PREVIEW = 3
#: How much coverage the *details* tool may show.
COVERAGE_DETAIL_MAX = 30
#: How many ships a cruise bundle lists.
SHIPS_DETAIL_MAX = 20


def country_summary(country: Country) -> dict[str, Any]:
    """One country, as a chat assistant would name it. The tag GUID never leaves the server."""
    summary: dict[str, Any] = {"country": country.name}
    if country.iso2:
        summary["country_code"] = country.iso2
    if country.iso3:
        summary["iso3_code"] = country.iso3
    return summary


def region_summary(region: Region) -> dict[str, Any]:
    """One region. ``region_code`` is what the bundle tools take as input."""
    summary: dict[str, Any] = {"region_name": region.name}
    if region.code:
        summary["region_code"] = region.code
    return summary


def bundle_summary(bundle: Bundle) -> dict[str, Any]:
    """A bundle as one numbered option in a chat reply."""
    summary: dict[str, Any] = {
        "bundle_code": bundle.code,
        "name": bundle.name,
        "data": bundle.data_display,
        "unlimited": bundle.is_unlimited,
        "validity": bundle.validity_text,
        "price": bundle.price_text,
    }
    if bundle.price is not None:
        summary["price_amount"] = bundle.price
    if bundle.currency_code:
        summary["currency"] = bundle.currency_code
    coverage = _coverage(bundle, preview=COVERAGE_PREVIEW)
    if coverage:
        summary["coverage"] = coverage
    if bundle.supported_ships:
        summary["ships_count"] = len(bundle.supported_ships)
    return summary


def bundle_detail(bundle: Bundle) -> dict[str, Any]:
    """A fuller, still bounded, view for ``get_bundle_details``."""
    detail: dict[str, Any] = {
        "bundle_code": bundle.code,
        "name": bundle.name,
        "data": bundle.data_display,
        "unlimited": bundle.is_unlimited,
        "validity": bundle.validity_text,
        "price": bundle.price_text,
        "available": bundle.is_available,
    }
    if bundle.subtitle:
        detail["description"] = bundle.subtitle
    if bundle.price is not None:
        detail["price_amount"] = bundle.price
    if bundle.currency_code:
        detail["currency"] = bundle.currency_code
    if bundle.original_price is not None and bundle.price is not None and bundle.original_price != bundle.price:
        detail["original_price_amount"] = bundle.original_price
    if bundle.validity_days is not None:
        detail["validity_days"] = bundle.validity_days
    if bundle.plan_type:
        detail["plan_type"] = bundle.plan_type
    if bundle.activity_policy:
        detail["activation_policy"] = bundle.activity_policy
    if bundle.label:
        detail["label"] = bundle.label
    if bundle.category_type:
        detail["category"] = bundle.category_type

    coverage = _coverage(bundle, preview=COVERAGE_DETAIL_MAX)
    if coverage:
        detail["coverage"] = coverage
    if bundle.bundle_region:
        detail["regions"] = [region.name for region in bundle.bundle_region]
    if bundle.supported_ships:
        ships = [ship.name for ship in bundle.supported_ships]
        detail["supported_ships"] = {
            "total_count": len(ships),
            "ships": ships[:SHIPS_DETAIL_MAX],
        }
    detail["price_note"] = PRICE_NOTE
    return detail


def _coverage(bundle: Bundle, *, preview: int) -> dict[str, Any] | None:
    """Country coverage as a count plus a bounded preview, never the raw list."""
    names = [country.name for country in bundle.countries if country.name]
    total = bundle.count_countries or len(names)
    if not total and not names:
        return None
    coverage: dict[str, Any] = {"countries_count": total}
    if names:
        coverage["countries"] = names[:preview]
        if len(names) > preview:
            coverage["countries_truncated"] = True
    return coverage
