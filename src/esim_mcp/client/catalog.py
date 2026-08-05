"""Typed wrapper over the backend's read-only catalogue routes.

Every route here is a ``GET`` that needs only ``X-Device-Id`` -- catalogue browsing does
not require a signed-in user, so no method takes or uses a token. All of them are reads,
so all of them may use the shared bounded retry policy.

Deliberately absent, and never to be added to this module: order, purchase, payment,
wallet, voucher, promotion, provisioning, top-up, consumption, callback and the
``/bundles/translate_*`` maintenance routes.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from esim_mcp.client.base import BackendApiClient
from esim_mcp.errors import (
    BackendUnavailableError,
    BundleNotFoundError,
    CatalogUnavailableError,
    InvalidInputError,
    RegionNotFoundError,
)
from esim_mcp.models.catalog import (
    Bundle,
    Country,
    HomeCatalog,
    Region,
    parse_bundle,
    parse_bundles,
    parse_countries,
    parse_home_catalog,
    parse_regions,
)

logger = logging.getLogger(__name__)

HOME_PATH = "/home/"
HOME_CRUISE_PATH = "/home/cruise"
HOME_LAND_PATH = "/home/land"
COUNTRIES_PATH = "/bundles/countries"
REGIONS_PATH = "/bundles/region"
BUNDLES_BY_COUNTRY_PATH = "/bundles/by-country"
BUNDLES_BY_REGION_PATH = "/bundles/by-region"
BUNDLE_PATH = "/bundles"


class CatalogApiClient:
    """Backend catalogue calls, one method per documented route."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    # ------------------------------------------------------------------------- home

    async def get_home_catalog(self, *, device_id: str, locale: str, currency: str) -> HomeCatalog:
        """``GET /home/`` -- countries, regions, cruise bundles and global bundles."""
        data = await self._get(HOME_PATH, device_id=device_id, locale=locale, currency=currency)
        return parse_home_catalog(data)

    async def get_cruise_catalog(self, *, device_id: str, locale: str, currency: str) -> HomeCatalog:
        """``GET /home/cruise`` -- same envelope shape, cruise bundles only."""
        data = await self._get(HOME_CRUISE_PATH, device_id=device_id, locale=locale, currency=currency)
        return parse_home_catalog(data)

    async def get_land_catalog(self, *, device_id: str, locale: str, currency: str) -> HomeCatalog:
        """``GET /home/land`` -- countries, regions and global bundles, no cruise bundles."""
        data = await self._get(HOME_LAND_PATH, device_id=device_id, locale=locale, currency=currency)
        return parse_home_catalog(data)

    # -------------------------------------------------------------- countries/regions

    async def list_countries(self, *, device_id: str, locale: str) -> list[Country]:
        """``GET /bundles/countries`` -- the authority for country tag GUIDs and ISO codes."""
        data = await self._get(COUNTRIES_PATH, device_id=device_id, locale=locale)
        return parse_countries(data)

    async def list_regions(self, *, device_id: str, locale: str) -> list[Region]:
        """``GET /bundles/region`` -- the authority for region codes."""
        data = await self._get(REGIONS_PATH, device_id=device_id, locale=locale)
        return parse_regions(data)

    # ----------------------------------------------------------------------- bundles

    async def list_bundles_by_country_guid(
        self,
        country_guid: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> list[Bundle]:
        """``GET /bundles/by-country?country_codes=<tag-guid>``.

        The query parameter takes the country's **tag GUID**, never an ISO code; resolving
        a user's wording to that GUID is :mod:`esim_mcp.catalog.resolution`'s job.
        """
        guid = _require_identifier(country_guid, "country")
        data = await self._get(
            BUNDLES_BY_COUNTRY_PATH,
            device_id=device_id,
            locale=locale,
            currency=currency,
            params={"country_codes": guid},
        )
        return parse_bundles(data)

    async def list_bundles_by_region_code(
        self,
        region_code: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> list[Bundle]:
        """``GET /bundles/by-region/{region_code}`` -- e.g. ``EUR``."""
        code = _require_identifier(region_code, "region")
        try:
            data = await self._get(
                f"{BUNDLES_BY_REGION_PATH}/{quote(code, safe='')}",
                device_id=device_id,
                locale=locale,
                currency=currency,
            )
        except InvalidInputError:
            # The backend answers 400 "Region Not Found" for an unknown code.
            raise RegionNotFoundError() from None
        return parse_bundles(data)

    async def get_bundle_details(
        self,
        bundle_code: str,
        *,
        device_id: str,
        locale: str,
        currency: str,
    ) -> Bundle:
        """``GET /bundles/{bundle_code}`` -- the bundle UUID, as returned by a list call."""
        code = _require_identifier(bundle_code, "bundle")
        try:
            data = await self._get(
                f"{BUNDLE_PATH}/{quote(code, safe='')}",
                device_id=device_id,
                locale=locale,
                currency=currency,
            )
        except InvalidInputError:
            raise BundleNotFoundError() from None
        if data is None:
            raise BundleNotFoundError()
        return parse_bundle(data)

    # ----------------------------------------------------------------------- internal

    async def _get(
        self,
        path: str,
        *,
        device_id: str,
        locale: str,
        currency: str | None = None,
        params: dict[str, str] | None = None,
    ) -> object:
        """Shared read: bounded retry, envelope validation, catalogue-flavoured errors."""
        try:
            return await self._client.request(
                "GET",
                path,
                device_id=device_id,
                locale=locale,
                currency=currency,
                params=params,
                allow_retry=True,
            )
        except BackendUnavailableError:
            # Same condition, but with a next action the model can offer the user.
            raise CatalogUnavailableError() from None


def _require_identifier(value: str, kind: str) -> str:
    """Reject empty or path-bending identifiers before they reach a URL."""
    candidate = (value or "").strip()
    if not candidate or "/" in candidate or "\\" in candidate:
        raise InvalidInputError(
            f"A valid {kind} identifier from a previous catalogue result is required. Never invent one."
        )
    return candidate
