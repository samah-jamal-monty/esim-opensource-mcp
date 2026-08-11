"""Phase 2 MCP tools: read-only catalogue and bundle discovery.

Contract enforced by this layer:

* every tool is a read. Nothing here orders, reserves, prices for payment, mutates a
  wallet or provisions an eSIM, and no tool in this module may ever do so;
* catalogue browsing needs **no login**. Only the caller's verified identity is used, and
  only to derive the stable ``X-Device-Id`` the backend requires;
* backend identifiers the user never says out loud (country tag GUIDs) stay on the server.
  The one identifier that does travel, ``bundle_code``, is needed so the model can look up
  the option a user picked from a list it already showed;
* no result ever claims to be the platform's complete catalogue, because no backend
  endpoint returns that.

:class:`CatalogService` holds the behaviour and is directly unit-testable;
:func:`register_catalog_tools` is the thin MCP binding.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from esim_mcp.catalog.resolution import CountryResolver, RegionResolver, normalize, suggest_countries
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
from esim_mcp.catalog.summaries import (
    PRICE_NOTE,
    bundle_detail,
    bundle_summary,
    country_summary,
    region_summary,
)
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.errors import NoMatchingBundlesError
from esim_mcp.models.catalog import Bundle, Country
from esim_mcp.session.identity import ClientIdentityProvider, derive_device_id
from esim_mcp.settings import Settings
from esim_mcp.tools.guard import guarded

logger = logging.getLogger(__name__)

#: How much of the home catalogue a single overview result may carry.
HOME_COUNTRY_PREVIEW = 8
HOME_REGION_LIMIT = 20
HOME_BUNDLE_PREVIEW = 5

CountryArg = Annotated[
    str,
    Field(
        description=(
            "The destination country the user named, in their own words: a country name "
            "('France', 'United Arab Emirates'), an ISO2 code ('FR') or an ISO3 code ('FRA'). "
            "Pass what the user said; this tool resolves it against the platform's own country "
            "list. Never invent a country identifier or code."
        )
    ),
]
RegionArg = Annotated[
    str,
    Field(
        description=(
            "The region the user named: a region name ('Europe') or a region code ('EUR'), as "
            "returned by list_regions or browse_home_catalog. Never invent a region code."
        )
    ),
]
BundleCodeArg = Annotated[
    str,
    Field(
        description=(
            "The bundle_code of a plan from a result you have already received. Never invent, "
            "guess or edit one, and never ask the user to read one out -- if you do not have it, "
            "search again and use the code from the option the user picked."
        )
    ),
]
QueryArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional search text the user gave, e.g. 'France' or 'FR'. Omit it to browse the "
            "list. With it, the result is an exact match or a short list of close suggestions."
        )
    ),
]
MaxPriceArg = Annotated[
    float | None,
    Field(description="Optional maximum price, in the result currency. Only pass a budget the user actually stated."),
]
MinimumDataArg = Annotated[
    float | None,
    Field(description="Optional minimum data allowance in GB. Unlimited plans always satisfy this."),
]
MinimumValidityArg = Annotated[
    int | None,
    Field(description="Optional minimum validity in days. A 1-month plan counts as 30 days."),
]
UnlimitedOnlyArg = Annotated[
    bool | None,
    Field(description="Set true only when the user explicitly asks for unlimited-data plans."),
]
SortByArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional ordering: 'price' (cheapest first), 'data' (largest allowance first) or "
            "'validity' (longest first). Omit to keep the platform's own order, which is already "
            "price-first."
        )
    ),
]
BundleLimitArg = Annotated[
    int | None,
    Field(
        description=(
            f"How many plans to return. Default {DEFAULT_BUNDLE_LIMIT}, maximum {MAX_BUNDLE_LIMIT}. "
            "Keep it small: a chat reply lists a handful of options, and the user can ask for more."
        )
    ),
]
PlaceLimitArg = Annotated[
    int | None,
    Field(description=f"How many entries to return. Default {DEFAULT_PLACE_LIMIT}, maximum {MAX_PLACE_LIMIT}."),
]
LocaleArg = Annotated[
    str | None,
    Field(description="Optional language tag for platform text, e.g. 'en'. Omit to use the server default."),
]
CurrencyArg = Annotated[
    str | None,
    Field(description="Optional ISO-4217 currency code, e.g. 'USD'. Omit to use the server default."),
]


class CatalogService:
    """Backend-facing behaviour for the read-only catalogue tools."""

    def __init__(
        self,
        settings: Settings,
        catalog_client: CatalogApiClient,
        identity_provider: ClientIdentityProvider,
    ) -> None:
        self._settings = settings
        self._client = catalog_client
        self._identity_provider = identity_provider
        self._countries = CountryResolver(catalog_client)
        self._regions = RegionResolver(catalog_client)

    # ------------------------------------------------------------------ internals

    async def _device_id(self, ctx: Any | None) -> str:
        """The stable device id for this caller. No session and no token is required."""
        identity = await self._identity_provider.resolve(ctx)
        return derive_device_id(self._settings.salt_bytes(), identity)

    def _locale(self, locale: str | None) -> str:
        return (locale or self._settings.default_locale).strip()

    def _currency(self, currency: str | None) -> str:
        return (currency or self._settings.default_currency).strip().upper()

    # ---------------------------------------------------------------------- tools

    async def list_countries(
        self,
        *,
        query: str | None = None,
        locale: str | None = None,
        limit: int | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """List catalogue countries, or resolve the one the user named."""
        device_id = await self._device_id(ctx)
        size = clamp_limit(limit, default=DEFAULT_PLACE_LIMIT, maximum=MAX_PLACE_LIMIT)
        countries = await self._countries.list_countries(device_id=device_id, locale=self._locale(locale))
        ordered = sorted(countries, key=lambda country: country.name.casefold())

        if not (query or "").strip():
            return {
                "status": "ok",
                "match": "browse",
                "total_count": len(ordered),
                "returned_count": min(size, len(ordered)),
                "countries": [country_summary(country) for country in ordered[:size]],
                "note": (
                    "Alphabetical extract of the destinations the platform sells for. Ask the user which "
                    "destination they want rather than reading the whole list out."
                ),
            }

        exact = _exact_country_matches(query or "", ordered)
        if exact:
            return {
                "status": "ok",
                "match": "exact",
                "total_count": len(exact),
                "returned_count": len(exact[:size]),
                "countries": [country_summary(country) for country in exact[:size]],
                "note": "Use find_bundles_by_country with this country to see the plans available for it.",
            }

        suggestions = suggest_countries(query or "", ordered)
        return {
            "status": "ok",
            "match": "suggestions" if suggestions else "none",
            "total_count": len(suggestions),
            "returned_count": len(suggestions),
            "countries": [country_summary(country) for country in suggestions],
            "note": (
                "No exact match. Ask the user whether they meant one of these before searching for plans."
                if suggestions
                else "That destination is not in the catalogue. Ask the user for another one."
            ),
        }

    async def list_regions(
        self,
        *,
        query: str | None = None,
        locale: str | None = None,
        limit: int | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """List catalogue regions, or resolve the one the user named."""
        device_id = await self._device_id(ctx)
        size = clamp_limit(limit, default=DEFAULT_PLACE_LIMIT, maximum=MAX_PLACE_LIMIT)
        regions = await self._regions.list_regions(device_id=device_id, locale=self._locale(locale))
        ordered = sorted(regions, key=lambda region: region.name.casefold())

        selected = ordered
        match = "browse"
        if (query or "").strip():
            needle = normalize(query)
            selected = [
                region for region in ordered if needle in (normalize(region.name), normalize(region.code or ""))
            ]
            match = "exact" if selected else "none"
            if not selected:
                selected = ordered
                match = "none"

        return {
            "status": "ok",
            "match": match,
            "total_count": len(selected),
            "returned_count": min(size, len(selected)),
            "regions": [region_summary(region) for region in selected[:size]],
            "note": (
                "Names only -- this result carries no plans. Call find_bundles_by_region with one of these "
                "region_code values to see a region's plans, and never say a region has none on the strength "
                "of this list."
                if match != "none"
                else "No region matched. These are the regions the platform actually sells; ask the user to pick one."
            ),
        }

    async def browse_home_catalog(
        self,
        *,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Overview of what the catalogue offers: counts plus small previews."""
        device_id = await self._device_id(ctx)
        home = await self._client.get_home_catalog(
            device_id=device_id, locale=self._locale(locale), currency=self._currency(currency)
        )

        countries = sorted(home.countries, key=lambda country: country.name.casefold())
        regions = sorted(home.regions, key=lambda region: region.name.casefold())
        cruise = drop_unavailable(home.cruise_bundles)
        global_bundles = drop_unavailable(home.global_bundles)

        return {
            "status": "ok",
            "countries": {
                "total_count": len(countries),
                "returned_count": min(HOME_COUNTRY_PREVIEW, len(countries)),
                "preview": [country_summary(country) for country in countries[:HOME_COUNTRY_PREVIEW]],
            },
            "regions": {
                "total_count": len(regions),
                "returned_count": min(HOME_REGION_LIMIT, len(regions)),
                "regions": [region_summary(region) for region in regions[:HOME_REGION_LIMIT]],
            },
            "cruise_bundles": _bundle_block(cruise, HOME_BUNDLE_PREVIEW),
            "global_bundles": _bundle_block(global_bundles, HOME_BUNDLE_PREVIEW),
            "price_note": PRICE_NOTE,
            "note": (
                "Overview only -- these are the categories the platform sells, not every plan. There is no "
                "way to list every plan for every country at once. Ask the user which country or region they "
                "are travelling to, or whether they want a global or cruise plan, then search for that."
            ),
        }

    async def find_bundles_by_country(
        self,
        *,
        country: str,
        max_price: float | None = None,
        minimum_data_gb: float | None = None,
        minimum_validity_days: int | None = None,
        unlimited_only: bool | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Resolve a country, then list the plans the platform sells for it."""
        device_id = await self._device_id(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)
        filters = build_filters(
            max_price=max_price,
            minimum_data_gb=minimum_data_gb,
            minimum_validity_days=minimum_validity_days,
            unlimited_only=unlimited_only,
        )
        size = clamp_limit(limit, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT)
        if sort_by is not None:
            parse_sort_key(sort_by)

        resolved = await self._countries.resolve(country, device_id=device_id, locale=locale_value)
        guid = resolved.guid
        if not guid:
            raise NoMatchingBundlesError(
                f"The catalogue lists {resolved.name} but cannot be searched for it right now. "
                "Ask the user for another destination, or offer a regional plan."
            )
        bundles = await self._client.list_bundles_by_country_guid(
            guid, device_id=device_id, locale=locale_value, currency=currency_value
        )

        result = self._bundle_result(
            bundles,
            filters=filters,
            sort_by=sort_by,
            size=size,
            destination=resolved.name,
            scope_note=(
                f"Plans the platform sells for {resolved.name}. This is that destination's list, "
                "not the platform's entire catalogue."
            ),
        )
        result["country"] = country_summary(resolved)
        return result

    async def find_bundles_by_region(
        self,
        *,
        region: str,
        max_price: float | None = None,
        minimum_data_gb: float | None = None,
        minimum_validity_days: int | None = None,
        unlimited_only: bool | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Resolve a region, then list the plans the platform sells for it."""
        device_id = await self._device_id(ctx)
        locale_value = self._locale(locale)
        currency_value = self._currency(currency)
        filters = build_filters(
            max_price=max_price,
            minimum_data_gb=minimum_data_gb,
            minimum_validity_days=minimum_validity_days,
            unlimited_only=unlimited_only,
        )
        size = clamp_limit(limit, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT)
        if sort_by is not None:
            parse_sort_key(sort_by)

        resolved = await self._regions.resolve(region, device_id=device_id, locale=locale_value)
        # The backend compares region codes with `==`, so what goes in the path has to be the
        # code it gave us, not the upper-cased one shown to the model. Sending `resolved.code`
        # here made every region whose code is not already upper-case answer 400 "Region Not
        # Found", which the assistant then reported to the user as "no plans for that region".
        code = resolved.api_code
        if not code:
            raise NoMatchingBundlesError(
                f"The catalogue lists {resolved.name} but gives no region code for it, so its plans cannot be "
                "looked up. This does not mean it has no plans -- offer another region or a single country."
            )
        bundles = await self._client.list_bundles_by_region_code(
            code,
            device_id=device_id,
            locale=locale_value,
            currency=currency_value,
        )

        result = self._bundle_result(
            bundles,
            filters=filters,
            sort_by=sort_by,
            size=size,
            destination=resolved.name,
            scope_note=(
                f"Regional plans covering {resolved.name}. This is that region's list, not the "
                "platform's entire catalogue."
            ),
        )
        result["region"] = region_summary(resolved)
        return result

    async def list_cruise_bundles(
        self,
        *,
        max_price: float | None = None,
        minimum_data_gb: float | None = None,
        minimum_validity_days: int | None = None,
        unlimited_only: bool | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """List the plans sold for cruise ships."""
        device_id = await self._device_id(ctx)
        filters = build_filters(
            max_price=max_price,
            minimum_data_gb=minimum_data_gb,
            minimum_validity_days=minimum_validity_days,
            unlimited_only=unlimited_only,
        )
        size = clamp_limit(limit, default=DEFAULT_BUNDLE_LIMIT, maximum=MAX_BUNDLE_LIMIT)
        if sort_by is not None:
            parse_sort_key(sort_by)

        catalog = await self._client.get_cruise_catalog(
            device_id=device_id, locale=self._locale(locale), currency=self._currency(currency)
        )
        return self._bundle_result(
            catalog.cruise_bundles,
            filters=filters,
            sort_by=sort_by,
            size=size,
            destination="cruise ships",
            scope_note=(
                "Cruise plans only. Each one covers the ships listed with it -- ask the user which ship or "
                "line they are sailing with if that matters."
            ),
        )

    async def get_bundle_details(
        self,
        *,
        bundle_code: str,
        locale: str | None = None,
        currency: str | None = None,
        ctx: Any | None = None,
    ) -> dict[str, Any]:
        """Full, still bounded, detail for one plan the user selected."""
        device_id = await self._device_id(ctx)
        bundle = await self._client.get_bundle_details(
            bundle_code,
            device_id=device_id,
            locale=self._locale(locale),
            currency=self._currency(currency),
        )
        return {
            "status": "ok",
            "bundle": bundle_detail(bundle),
            "note": (
                "Describe this plan in ordinary language. Do not read the bundle code out to the user; "
                "keep it for your own follow-up calls."
            ),
        }

    # ---------------------------------------------------------------- result shaping

    def _bundle_result(
        self,
        bundles: list[Bundle],
        *,
        filters: BundleFilters,
        sort_by: str | None,
        size: int,
        destination: str,
        scope_note: str,
    ) -> dict[str, Any]:
        """Filter, sort, limit and summarize one destination's bundles."""
        available = drop_unavailable(bundles)
        matched = apply_bundle_filters(available, filters)

        if not matched and available and not filters.is_empty:
            applied = ", ".join(filters.describe())
            raise NoMatchingBundlesError(
                f"None of the {len(available)} plans for {destination} match those filters ({applied}). "
                "Tell the user what is constraining the search and ask whether to relax it, then search again."
            )

        ordered = sort_bundles(matched, sort_by)
        selected = ordered[:size]
        result: dict[str, Any] = {
            "status": "ok",
            "destination": destination,
            "total_count": len(matched),
            "returned_count": len(selected),
            "bundles": [bundle_summary(bundle) for bundle in selected],
            "price_note": PRICE_NOTE,
            "note": scope_note,
        }
        if not filters.is_empty:
            result["total_available"] = len(available)
            result["filters_applied"] = filters.describe()
        if not matched:
            result["note"] = (
                f"The platform currently sells no plans for {destination}. Tell the user plainly and offer "
                "a neighbouring country, a regional plan or a global plan instead."
            )
        elif len(matched) > len(selected):
            result["more_available"] = True
        return result


def _bundle_block(bundles: list[Bundle], preview: int) -> dict[str, Any]:
    """Counts plus a small preview, used by the home overview."""
    return {
        "total_count": len(bundles),
        "returned_count": min(preview, len(bundles)),
        "bundles": [bundle_summary(bundle) for bundle in bundles[:preview]],
    }


def _exact_country_matches(query: str, countries: list[Country]) -> list[Country]:
    """Countries whose ISO2, ISO3, name or alternative name equals the query exactly."""
    needle = normalize(query)
    if not needle:
        return []
    matches = [
        country
        for country in countries
        if needle in {normalize(country.iso2), normalize(country.iso3), normalize(country.name)}
        or needle == normalize(country.alternative_name)
    ]
    return [country for country in matches if needle]


def register_catalog_tools(server: MCPServer, service: CatalogService) -> None:
    """Bind the Phase 2 catalogue tools onto an :class:`MCPServer` instance.

    Descriptions are written for an *AI caller* making a sales conversation work: when to
    reach for the tool, what to ask the user first, how to present the result, and what
    never to claim. Every one of them is a read; none of them can order or charge anything.
    """

    @server.tool(
        name="list_countries",
        title="List eSIM destinations",
        description=(
            "List the countries the eSIM platform sells plans for, or check the one the user "
            "named.\n"
            "WHEN: the user asks which destinations are available, or you want to confirm a "
            "country exists before searching for plans.\n"
            "Pass the user's own wording as 'query' to resolve it; omit 'query' to browse. The "
            "result is deliberately a limited extract plus a total count -- do not read the whole "
            "list out, ask the user where they are travelling.\n"
            "This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def list_countries(
        query: QueryArg = None,
        locale: LocaleArg = None,
        limit: PlaceLimitArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "list_countries", lambda: service.list_countries(query=query, locale=locale, limit=limit, ctx=ctx)
        )

    @server.tool(
        name="list_regions",
        title="List eSIM regions",
        description=(
            "List the regions the eSIM platform sells multi-country plans for, such as Europe.\n"
            "WHEN: the user asks which regions exist, or you need to check that a region the user "
            "named is one the platform actually sells for.\n"
            "IMPORTANT: this returns region NAMES AND CODES ONLY -- it never returns a region's "
            "plans, and an empty or short list here says nothing about how many plans a region "
            'has. To show a region\'s plans ("show bundles for Europe", "bundles in Asia", "plans '
            'for this region") call find_bundles_by_region. Never answer a question about a '
            "region's plans from this result, and never conclude from it that a region has none.\n"
            "Pass each region on to find_bundles_by_region by its region_code from this result "
            "when you have it; it is the platform's own stable identifier and is safer than a "
            "name. This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def list_regions(
        query: QueryArg = None,
        locale: LocaleArg = None,
        limit: PlaceLimitArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "list_regions", lambda: service.list_regions(query=query, locale=locale, limit=limit, ctx=ctx)
        )

    @server.tool(
        name="browse_home_catalog",
        title="Browse the eSIM catalogue",
        description=(
            "Show what the catalogue offers: how many destinations and regions exist, plus a few "
            "cruise and global plans.\n"
            'WHEN: the user asks something broad like "show me all bundles", "what do you have" or '
            '"what plans are there".\n'
            "IMPORTANT: there is no endpoint that returns every plan for every country, so never "
            "claim to be showing all of them. Use this overview to say what kinds of plans exist, "
            "then ask which country or region the user is travelling to, or whether they want a "
            "global or cruise plan.\n"
            "This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def browse_home_catalog(
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "browse_home_catalog", lambda: service.browse_home_catalog(locale=locale, currency=currency, ctx=ctx)
        )

    @server.tool(
        name="find_bundles_by_country",
        title="Find eSIM plans for a country",
        description=(
            "Find the eSIM plans available for one country.\n"
            'WHEN: the user names a destination country -- "I need an eSIM for France".\n'
            "Pass the country exactly as the user said it (name, ISO2 or ISO3); this tool resolves "
            "it against the platform's own list. Only pass a filter the user actually stated "
            "(budget, minimum data, minimum validity, unlimited).\n"
            "AFTER SUCCESS: present a few options as a numbered list with data, validity and price, "
            "and ask whether the user wants details on one of them. Keep the bundle_code of each "
            'option so that "the second one" can be looked up with get_bundle_details; never read a '
            "code out to the user and never invent one.\n"
            "The result covers this destination only -- never describe it as the platform's whole "
            "catalogue. Quote the platform's prices as given; do not warn that they may change or "
            "exclude tax, because the platform says no such thing.\n"
            "This needs no login; browsing must never be blocked behind signing in."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def find_bundles_by_country(
        country: CountryArg,
        max_price: MaxPriceArg = None,
        minimum_data_gb: MinimumDataArg = None,
        minimum_validity_days: MinimumValidityArg = None,
        unlimited_only: UnlimitedOnlyArg = None,
        sort_by: SortByArg = None,
        limit: BundleLimitArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "find_bundles_by_country",
            lambda: service.find_bundles_by_country(
                country=country,
                max_price=max_price,
                minimum_data_gb=minimum_data_gb,
                minimum_validity_days=minimum_validity_days,
                unlimited_only=unlimited_only,
                sort_by=sort_by,
                limit=limit,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="find_bundles_by_region",
        title="Find eSIM plans for a region",
        description=(
            "Find the multi-country eSIM plans available for one region, such as Europe.\n"
            'WHEN: the user asks for a region\'s plans -- "show bundles for Europe", "bundles in '
            'Asia", "plans for this region", "what do you have for the Middle East" -- or names a '
            "region, or is visiting several countries in the same area. This is the ONLY tool that "
            "returns a region's plans; list_regions just names the regions.\n"
            "Prefer the region_code from a list_regions or browse_home_catalog result, which is "
            "the platform's own stable identifier. Otherwise pass the region name as the user said "
            "it and this tool resolves it against the platform's own region list -- it never "
            "guesses between two similar regions, it asks. Filters and presentation work exactly "
            "as in find_bundles_by_country: only pass filters the user stated, then offer a short "
            "numbered list and keep each bundle_code for follow-up.\n"
            "AFTER FAILURE: an error here means the plans could not be read, NOT that the region "
            "has none. Only say a region has no plans when this tool returns successfully with an "
            "empty list and says so.\n"
            "The result covers this region only. This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def find_bundles_by_region(
        region: RegionArg,
        max_price: MaxPriceArg = None,
        minimum_data_gb: MinimumDataArg = None,
        minimum_validity_days: MinimumValidityArg = None,
        unlimited_only: UnlimitedOnlyArg = None,
        sort_by: SortByArg = None,
        limit: BundleLimitArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "find_bundles_by_region",
            lambda: service.find_bundles_by_region(
                region=region,
                max_price=max_price,
                minimum_data_gb=minimum_data_gb,
                minimum_validity_days=minimum_validity_days,
                unlimited_only=unlimited_only,
                sort_by=sort_by,
                limit=limit,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="list_cruise_bundles",
        title="Find eSIM plans for cruises",
        description=(
            "List the eSIM plans sold for cruise ships.\n"
            "WHEN: the user says they are going on a cruise, names a ship or a cruise line, or asks "
            "for maritime coverage.\n"
            "Each plan covers specific ships; the summary carries how many, and get_bundle_details "
            "names them. If the user has a particular ship in mind, check it there before promising "
            "coverage. This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def list_cruise_bundles(
        max_price: MaxPriceArg = None,
        minimum_data_gb: MinimumDataArg = None,
        minimum_validity_days: MinimumValidityArg = None,
        unlimited_only: UnlimitedOnlyArg = None,
        sort_by: SortByArg = None,
        limit: BundleLimitArg = None,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "list_cruise_bundles",
            lambda: service.list_cruise_bundles(
                max_price=max_price,
                minimum_data_gb=minimum_data_gb,
                minimum_validity_days=minimum_validity_days,
                unlimited_only=unlimited_only,
                sort_by=sort_by,
                limit=limit,
                locale=locale,
                currency=currency,
                ctx=ctx,
            ),
        )

    @server.tool(
        name="get_bundle_details",
        title="Get eSIM plan details",
        description=(
            "Read the full details of one plan: data, validity, price, coverage, plan type, "
            "activation policy and availability.\n"
            'WHEN: the user asks about a specific plan you already listed -- "tell me more about the '
            'second one".\n'
            "Pass the bundle_code of the option the user picked, taken from the result you already "
            "have. Never invent a code, and never ask the user to read one out.\n"
            "AFTER SUCCESS: describe the plan in ordinary language and give the platform's price as "
            "it stands, without warning that it may change or exclude tax. Nothing here reserves, "
            "orders or charges anything. This needs no login."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_bundle_details(
        bundle_code: BundleCodeArg,
        locale: LocaleArg = None,
        currency: CurrencyArg = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await guarded(
            "get_bundle_details",
            lambda: service.get_bundle_details(bundle_code=bundle_code, locale=locale, currency=currency, ctx=ctx),
        )
