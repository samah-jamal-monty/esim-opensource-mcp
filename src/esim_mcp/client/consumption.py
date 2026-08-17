"""The one live-usage read this server may make.

``GET /user/consumption/{iccid}`` is the platform's own reading of an eSIM's data usage: the
backend resolves the ICCID against the **authenticated caller's own** profiles, picks the
started (or otherwise active) bundle on that SIM, and asks the eSIM hub for its live figures.
Nothing about the answer is derived from a catalogue allowance or from a validity date.

Ownership, twice
----------------
The backend refuses an ICCID that is not the caller's: it looks the profile up as
``{"user_id": user.id, "iccid": iccid}`` and raises ``USER_PROFILE_NOT_FOUND`` when there is
no row. That check is the authoritative one. This server nevertheless resolves the ICCID
against the caller's own ``GET /user/my-esim`` list *before* calling here, so a foreign or
invented identifier never reaches the platform at all -- see
:mod:`esim_mcp.tools.consumption`.

Why the path is allowlisted rather than blocked
-----------------------------------------------
``consumption`` is one of :data:`~esim_mcp.client.base.FORBIDDEN_PATH_MARKERS`, because for
the phases before this one there was no reason to reach any of it. It is admitted now by an
explicit, exact-prefix entry in
:data:`~esim_mcp.client.base.PERMITTED_REFERENCE_READ_ROUTES`, with the ICCID validated to a
single opaque segment first. It is a ``GET``: it provisions nothing, tops up nothing and
changes nothing, so it uses the shared bounded retry policy.

Deliberately absent, and never to be added here: the consumption *callback* the eSIM hub
posts to (``POST /callback/plan_status_callback``), which is the platform's own inbound
webhook and none of this server's business.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import SecretStr

from esim_mcp.client.base import BackendApiClient, RequestCredentials
from esim_mcp.errors import InvalidInputError

logger = logging.getLogger(__name__)

#: The live consumption read, without its trailing ICCID. The prefix is allowlisted in the
#: transport guard; the segment appended to it is validated below.
CONSUMPTION_PATH_PREFIX = "/user/consumption/"

#: The shape an ICCID may take before it is ever put into a URL. An ICCID is a 19-20 digit
#: number; the bound is widened a little because the platform stores what the provider gave
#: it, and narrowed to digits only because anything else is not an ICCID. Refused rather
#: than escaped: this server never generates one, so a value with a slash, a dot-segment or
#: a space in it came from somewhere it should not have.
_ICCID_RE = re.compile(r"^\d{15,22}$")


def require_iccid(value: str | None) -> str:
    """Validate an ICCID before it is ever put into a path.

    The message deliberately does not repeat the offending value: an ICCID is the handle to
    a provisioned SIM, and echoing one into a model's context is exactly what the rest of
    this codebase is careful not to do.
    """
    candidate = (value or "").strip()
    if not _ICCID_RE.match(candidate):
        raise InvalidInputError(
            "That is not an eSIM identifier this platform issued. Use get_my_esims to see the eSIMs the "
            "signed-in user owns and pick one of those -- never invent an identifier, and never take one from "
            "the user."
        )
    return candidate


class ConsumptionApiClient:
    """The single authenticated live-usage read. Reads only."""

    def __init__(self, client: BackendApiClient) -> None:
        self._client = client

    async def get_consumption(
        self,
        *,
        device_id: str,
        access_token: SecretStr,
        iccid: str,
        locale: str,
        currency: str,
    ) -> Any:
        """``GET /user/consumption/{iccid}`` -- the platform's live reading for one eSIM.

        The envelope's ``data`` is a ``ConsumptionResponse``, or ``null`` when the platform
        has nothing to report. Both are successful answers and neither is an error: a plan
        that has not started yet genuinely has no usage, and that is a fact to state rather
        than a zero to invent.

        A read, so the shared bounded retry policy applies: repeating it costs nothing and
        changes nothing.
        """
        reference = require_iccid(iccid)
        return await self._client.request(
            "GET",
            f"{CONSUMPTION_PATH_PREFIX}{reference}",
            device_id=device_id,
            locale=locale,
            currency=currency,
            credentials=RequestCredentials(access_token=access_token),
            allow_retry=True,
        )
