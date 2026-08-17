"""The one top-up read this server may make: which plans a given eSIM can be topped up with.

``GET /user/related-topup/{bundle_code}/{iccid}`` is the platform's own compatibility list.
The backend resolves the ICCID against the **authenticated caller's own** profiles (raising
"This ICCID is not linked to this user" otherwise), asks the eSIM hub which bundles are
related to that SIM's live order, and then returns each one as the platform's *local*
catalogue record -- so the price, the currency, the data allowance and the validity are the
platform's own values in the caller's own currency.

This is the only honest source for "what can I add to this SIM". The general catalogue
cannot answer it: a plan that looks similar is not necessarily one the provider will attach
to a SIM already in use, and presenting one as a top-up would be a guess with a price on it.

The QA-only execution call
--------------------------
:meth:`TopupApiClient.execute_topup` wraps the platform's **legacy**
``POST /user/bundle/assign-top-up`` -- the same route the portal uses. It exists for QA and
is gated three times over: the tool is not registered unless
:attr:`~esim_mcp.settings.Settings.esim_topup_execution_enabled` is on, the service refuses
unless it is on, and :func:`~esim_mcp.client.base.enforce_route_is_permitted` refuses the
path unless it is on. Production settings refuse to construct with the flag set at all.

**This call is not idempotent and cannot be made so from here.** The route accepts no
idempotency key, and the ``Topup`` row it writes carries neither an ICCID nor a request key,
so the platform cannot tell a retry apart from a second genuine top-up. It also debits the
wallet *before* provisioning and swallows a failed provisioning. Every safety property this
module can offer is therefore about **not sending a second request**, never about making a
second request safe: one send, no retry at any level, and a caller that records the attempt
before it goes out.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, BackendOutcome, RequestCredentials
from esim_mcp.client.consumption import require_iccid
from esim_mcp.errors import InvalidInputError

logger = logging.getLogger(__name__)

#: The compatibility read, without its two trailing reference segments. The prefix is
#: allowlisted in the transport guard; both segments appended to it are validated below.
RELATED_TOPUP_PATH_PREFIX = "/user/related-topup/"

#: The platform's legacy top-up execution route. Reachable only under the QA flag -- see the
#: module docstring and :data:`~esim_mcp.client.base.QA_ESIM_TOPUP_ROUTE`.
LEGACY_TOPUP_EXECUTION_PATH = "/user/bundle/assign-top-up"

#: The exact value the platform's ``AssignTopUpRequest.payment_type`` is compared against.
#: It is a plain ``str`` on the backend model, matched against ``PaymentTypeEnum.WALLET``, so
#: the spelling has to be exact -- "wallet" or "WALLET" would fall through to the endpoint's
#: "unsupported payment type" branch.
WALLET_PAYMENT_TYPE = "Wallet"

#: The shape a bundle code may take inside a path. The platform's own codes are GUIDs, but
#: the field is a free-text column upstream, so the pattern is the same conservative opaque
#: segment the rest of this codebase uses for a reference: no slash, no dot-segment, no
#: query, nothing that could change which route is being addressed.
_BUNDLE_CODE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def require_bundle_code(value: str | None) -> str:
    """Validate a bundle code before it is ever put into a path.

    Refused rather than escaped. A code only ever reaches here from a result this server
    itself produced, so anything unusual in one is a bug or an injection attempt.
    """
    candidate = (value or "").strip()
    if not _BUNDLE_CODE_RE.match(candidate):
        raise InvalidInputError(
            "That is not a plan code this platform issued. Use a bundle_code from a result you already have in "
            "this conversation -- never invent one, and never ask the user to read one out."
        )
    return candidate


class TopupApiClient:
    """The single authenticated top-up compatibility read. Reads only."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def get_topup_options(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        bundle_code: str,
        iccid: str,
        locale: str,
        currency: str,
    ) -> Any:
        """``GET /user/related-topup/{bundle_code}/{iccid}`` -- compatible top-up plans.

        The envelope's ``data`` is a list of the platform's own bundle records, or ``[]``
        when the provider offers no top-up for that SIM. An empty list is a successful
        answer and a real fact: it means there is nothing to sell, not that the read failed.

        A read, so the shared bounded retry policy applies.
        """
        code = require_bundle_code(bundle_code)
        reference = require_iccid(iccid)
        return await self._client.request(
            "GET",
            f"{RELATED_TOPUP_PATH_PREFIX}{code}/{reference}",
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )

    async def execute_topup(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        iccid: str,
        bundle_code: str,
        locale: str,
        currency: str,
        read_timeout: float | None = None,
    ) -> BackendOutcome:
        """Send **exactly one** legacy top-up and report what came back. QA-only.

        **Never retried here, at any level, and never retried by the caller either.** This is
        the one call in this codebase for which a repeat is unambiguously a second charge:
        the platform has no idempotency key to present and no way to recognise the request it
        already ran. A timeout raises so the caller can record the outcome as *unknown* and
        lock the quote; every answer the platform actually gave comes back as a
        :class:`~esim_mcp.client.base.BackendOutcome`, failures included, so the caller can
        tell "the platform refused" apart from "the platform said something I cannot read".

        The body is exactly the platform's own ``AssignTopUpRequest``, built literally:

        * ``iccid`` -- validated to a plain numeric segment first, and resolved from the
          caller's own eSIM list by the service above;
        * ``bundle_code`` -- validated, and confirmed against the platform's own
          compatibility list for this SIM immediately before this call;
        * ``payment_type`` -- fixed to :data:`WALLET_PAYMENT_TYPE`. There is deliberately no
          parameter for it: a card top-up would open a payment intent this server cannot use,
          and a DCB top-up would start an OTP flow it cannot finish.

        There is no amount, price, currency, tax, discount, user id or order id in the body,
        because the platform derives every one of them itself.

        ``read_timeout`` is the caller's budget. It is deliberately generous: the platform
        debits the wallet and then provisions against the eSIM hub before it answers, and
        hanging up does not cancel a penny of it -- it only destroys this server's knowledge
        of whether it happened.
        """
        reference = require_iccid(iccid)
        code = require_bundle_code(bundle_code)
        body: dict[str, Any] = {
            "iccid": reference,
            "bundle_code": code,
            "payment_type": WALLET_PAYMENT_TYPE,
        }
        logger.warning(
            "qa_esim_topup_sending",
            # Never the ICCID and never the amount. This line exists so a QA run can be
            # correlated with a platform-side order, and for nothing else.
            extra={"bundle_code": code, "idempotent": False},
        )
        return await self._client.request_once(
            "POST",
            LEGACY_TOPUP_EXECUTION_PATH,
            device_id=device_id,
            json_body=body,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            read_timeout=read_timeout,
        )
