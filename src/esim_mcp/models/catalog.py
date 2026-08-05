"""Catalogue models mirroring the backend's ``/home`` and ``/bundles`` contracts.

Every field is optional or defaulted: the catalogue is a read-only projection of an
upstream eSIM hub, and the backend itself substitutes placeholders (``"Unknown"``, ``""``)
when the upstream record is incomplete. Parsing therefore never fails on a missing field,
but it also never invents one -- a placeholder is normalized to ``None`` rather than being
carried into a tool result where the model might read it out as a real value.

Two derived values are computed here rather than in the tool layer, because both depend on
how the *backend* builds its display strings:

* :attr:`Bundle.data_gb` reads the unit out of ``gprs_limit_display`` (``"5.0 GB"``); the
  numeric ``gprs_limit`` alone is unit-less, so it can never be compared safely on its own.
* :attr:`Bundle.validity_days` mirrors the backend's own day/week/month/year multipliers,
  so a "30 Day" and a "1 Month" bundle compare correctly.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from esim_mcp.errors import InvalidBackendResponseError

logger = logging.getLogger(__name__)

#: Values the backend emits when the upstream record has no usable value.
_PLACEHOLDERS = frozenset({"", "-", "n/a", "na", "none", "null", "unknown"})

_LEADING_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_UNIT_RE = re.compile(r"[A-Za-z]+")

#: ``gprs_limit_display`` is ``f"{gprs_limit} {dataUnit}"``, so the unit is authoritative.
_DATA_UNIT_TO_GB: dict[str, float] = {
    "kb": 1.0 / (1024.0 * 1024.0),
    "mb": 1.0 / 1024.0,
    "gb": 1.0,
    "tb": 1024.0,
}

#: The same multipliers the backend applies when it derives expiry from ``validity_display``.
_VALIDITY_UNIT_TO_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}


def clean_text(value: str | None) -> str | None:
    """Normalize whitespace and map the backend's placeholders onto ``None``."""
    if value is None:
        return None
    candidate = " ".join(str(value).split())
    return None if candidate.lower() in _PLACEHOLDERS else candidate


def _coerce_float(value: Any) -> float | None:
    """Best-effort float from a number or a numeric-looking string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _LEADING_NUMBER_RE.search(str(value))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


class BundleCategory(BaseModel):
    """``bundleCategory`` -- ``type`` is the interesting one (``GLOBAL``, ``CRUISE``, ...)."""

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    title: str | None = None
    code: str | None = None

    @property
    def label(self) -> str | None:
        return clean_text(self.title) or clean_text(self.type)


class Country(BaseModel):
    """``CountryDTO``. Also used by the backend to describe a cruise ship."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    country: str = ""
    country_code: str | None = None
    iso3_code: str | None = None
    alternative_country: str | None = None
    zone_name: str | None = None
    icon: str | None = None
    operator_list: list[str] = Field(default_factory=list)

    @field_validator("operator_list", mode="before")
    @classmethod
    def _default_operator_list(cls, value: Any) -> Any:
        return [] if value is None else value

    @property
    def guid(self) -> str | None:
        """The tag GUID ``/bundles/by-country`` expects. Internal: never returned to a client."""
        return clean_text(self.id)

    @property
    def name(self) -> str:
        """Human-readable name, falling back to a code when the name is a placeholder."""
        return clean_text(self.country) or self.iso3 or self.iso2 or "Unknown"

    @property
    def iso2(self) -> str | None:
        code = clean_text(self.country_code)
        return code.upper() if code else None

    @property
    def iso3(self) -> str | None:
        code = clean_text(self.iso3_code)
        return code.upper() if code else None

    @property
    def alternative_name(self) -> str | None:
        return clean_text(self.alternative_country)

    @property
    def zone(self) -> str | None:
        return clean_text(self.zone_name)


class Region(BaseModel):
    """``RegionDTO``. ``region_code`` (e.g. ``EUR``) is what ``/bundles/by-region`` expects."""

    model_config = ConfigDict(extra="ignore")

    region_code: str = ""
    region_name: str = ""
    zone_name: str | None = None
    guid: str | None = None
    icon: str | None = None

    @property
    def code(self) -> str | None:
        code = clean_text(self.region_code)
        return code.upper() if code else None

    @property
    def name(self) -> str:
        return clean_text(self.region_name) or self.code or "Unknown"


class Bundle(BaseModel):
    """``BundleDTO`` as returned by the catalogue routes.

    ``price``/``original_price`` are *display* prices in ``currency_code``; the backend
    computes them from an exchange rate and they may not include final payment tax.
    """

    model_config = ConfigDict(extra="ignore")

    bundle_code: str = ""
    display_title: str | None = None
    display_subtitle: str | None = None
    bundle_name: str | None = None
    bundle_marketing_name: str | None = None
    bundle_category: BundleCategory | None = None
    bundle_region: list[Region] = Field(default_factory=list)
    count_countries: int = 0
    countries: list[Country] = Field(default_factory=list)
    supported_ships: list[Country] = Field(default_factory=list)
    currency_code: str | None = None
    gprs_limit: float | None = None
    gprs_limit_display: str | None = None
    original_price: float | None = None
    price: float | None = None
    price_display: str | None = None
    unlimited: bool = False
    validity: int = 0
    validity_label: str | None = None
    validity_display: str | None = None
    plan_type: str | None = None
    activity_policy: str | None = None
    icon: str | None = None
    label: str | None = None
    is_stockable: bool | None = None
    bundle_info_code: str | None = None
    is_active: bool | None = None

    @field_validator("bundle_region", "countries", "supported_ships", mode="before")
    @classmethod
    def _default_list(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("gprs_limit", "original_price", "price", mode="before")
    @classmethod
    def _numeric(cls, value: Any) -> Any:
        return _coerce_float(value)

    @field_validator("validity", mode="before")
    @classmethod
    def _validity_int(cls, value: Any) -> Any:
        number = _coerce_float(value)
        return 0 if number is None else int(number)

    @field_validator("count_countries", mode="before")
    @classmethod
    def _count_int(cls, value: Any) -> Any:
        number = _coerce_float(value)
        return 0 if number is None else max(0, int(number))

    @property
    def code(self) -> str | None:
        return clean_text(self.bundle_code)

    @property
    def name(self) -> str:
        """The name to show a user, preferring the localized display title."""
        for candidate in (self.display_title, self.bundle_marketing_name, self.bundle_name):
            cleaned = clean_text(candidate)
            if cleaned:
                return cleaned
        return "eSIM plan"

    @property
    def subtitle(self) -> str | None:
        return clean_text(self.display_subtitle)

    @property
    def is_unlimited(self) -> bool:
        """The backend sets ``unlimited`` from a negative ``gprsLimit``; honour both."""
        return bool(self.unlimited) or (self.gprs_limit is not None and self.gprs_limit < 0)

    @property
    def data_gb(self) -> float | None:
        """Data allowance in GB, or ``inf`` for an unlimited plan.

        The unit comes from ``gprs_limit_display``. When that string carries no unit the
        numeric ``gprs_limit`` is used as GB, which is the backend's own default unit;
        ``None`` means the allowance could not be established and the bundle is therefore
        never silently included in a "at least N GB" filter.
        """
        if self.is_unlimited:
            return math.inf
        display = clean_text(self.gprs_limit_display)
        if display:
            amount = _coerce_float(display)
            unit_match = _UNIT_RE.search(display)
            unit = unit_match.group(0).lower() if unit_match else None
            if amount is not None and unit in _DATA_UNIT_TO_GB:
                return amount * _DATA_UNIT_TO_GB[unit]
            if amount is not None and unit is None:
                return amount
        if self.gprs_limit is not None and self.gprs_limit >= 0:
            return self.gprs_limit
        return None

    @property
    def data_display(self) -> str:
        """Allowance as the backend renders it, with a readable unlimited fallback."""
        display = clean_text(self.gprs_limit_display)
        if self.is_unlimited:
            return display or "Unlimited"
        return display or "Unknown"

    @property
    def validity_days(self) -> int | None:
        """Validity normalized to days, mirroring the backend's own multipliers."""
        display = clean_text(self.validity_display)
        if display:
            amount = _coerce_float(display)
            unit_match = _UNIT_RE.search(display)
            unit = unit_match.group(0).lower().rstrip("s") if unit_match else None
            if amount is not None and unit in _VALIDITY_UNIT_TO_DAYS:
                return int(amount) * _VALIDITY_UNIT_TO_DAYS[unit]
            if amount is not None and unit is None:
                return int(amount)
        if self.validity > 0:
            label = (clean_text(self.validity_label) or "day").lower().rstrip("s")
            return self.validity * _VALIDITY_UNIT_TO_DAYS.get(label, 1)
        return None

    @property
    def validity_text(self) -> str:
        display = clean_text(self.validity_display)
        if display:
            return display
        if self.validity > 0:
            return f"{self.validity} {clean_text(self.validity_label) or 'Day'}"
        return "Unknown"

    @property
    def price_text(self) -> str:
        display = clean_text(self.price_display)
        if display:
            return display
        if self.price is None:
            return "Unknown"
        return f"{self.price:.2f} {clean_text(self.currency_code) or ''}".strip()

    @property
    def is_available(self) -> bool:
        """A bundle is offerable unless the backend explicitly marks it inactive."""
        return self.is_active is not False

    @property
    def category_type(self) -> str | None:
        return clean_text(self.bundle_category.type) if self.bundle_category else None


class HomeCatalog(BaseModel):
    """``HomeResponseDto`` -- the same shape for ``/home/``, ``/home/cruise`` and ``/home/land``."""

    model_config = ConfigDict(extra="ignore")

    countries: list[Country] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)
    cruise_bundles: list[Bundle] = Field(default_factory=list)
    global_bundles: list[Bundle] = Field(default_factory=list)

    @field_validator("countries", "regions", "cruise_bundles", "global_bundles", mode="before")
    @classmethod
    def _default_list(cls, value: Any) -> Any:
        return [] if value is None else value


def parse_home_catalog(data: Any) -> HomeCatalog:
    """Validate the ``data`` portion of a ``/home`` envelope."""
    if not isinstance(data, dict):
        logger.warning("catalog_home_response_not_an_object")
        raise InvalidBackendResponseError()
    try:
        return HomeCatalog.model_validate(data)
    except ValidationError:
        logger.warning("catalog_home_response_validation_failed")
        raise InvalidBackendResponseError() from None


def parse_countries(data: Any) -> list[Country]:
    """Validate a ``/bundles/countries`` payload."""
    return _parse_list(data, Country, "country")


def parse_regions(data: Any) -> list[Region]:
    """Validate a ``/bundles/region`` payload."""
    return _parse_list(data, Region, "region")


def parse_bundles(data: Any) -> list[Bundle]:
    """Validate a bundle-list payload."""
    return _parse_list(data, Bundle, "bundle")


def parse_bundle(data: Any) -> Bundle:
    """Validate a single ``/bundles/{bundle_code}`` payload."""
    if not isinstance(data, dict):
        logger.warning("catalog_bundle_response_not_an_object")
        raise InvalidBackendResponseError()
    try:
        return Bundle.model_validate(data)
    except ValidationError:
        logger.warning("catalog_bundle_response_validation_failed")
        raise InvalidBackendResponseError() from None


def _parse_list[ModelT: BaseModel](data: Any, model: type[ModelT], kind: str) -> list[ModelT]:
    """Validate a list payload, dropping (and counting) individual malformed records.

    A wrong *container* type is a contract violation and fails the call. A single bad
    record is not allowed to take a whole destination's results down with it, so it is
    skipped and counted; the record itself is never logged.
    """
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("catalog_list_response_not_a_list", extra={"payload_kind": kind})
        raise InvalidBackendResponseError()

    parsed: list[ModelT] = []
    skipped = 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            parsed.append(model.model_validate(item))
        except ValidationError:
            skipped += 1
    if skipped:
        logger.warning("catalog_records_skipped", extra={"payload_kind": kind, "skipped": skipped})
    return parsed
