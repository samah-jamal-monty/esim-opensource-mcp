"""The backend response envelope.

Every eSIM backend route answers with the same wrapper::

    {"status": "success", "totalCount": 0, "data": {}, "title": "Success",
     "message": null, "developerMessage": null, "responseCode": 200}

Success is decided from the envelope, not from the HTTP status alone: the backend can
answer ``200`` with ``status: "failed"``, and ``developerMessage`` must never leave the
process.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SUCCESS_STATUSES = frozenset({"success", "succeeded", "ok"})


class BackendEnvelope(BaseModel):
    """Parsed backend envelope. ``data`` stays untyped; callers validate their own slice."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str | None = None
    total_count: int | None = Field(default=None, alias="totalCount")
    data: Any = None
    title: str | None = None
    message: str | None = None
    developer_message: str | None = Field(default=None, alias="developerMessage")
    response_code: int | None = Field(default=None, alias="responseCode")

    @property
    def is_success(self) -> bool:
        """True only when the envelope itself reports success."""
        if self.status is not None and self.status.strip().lower() not in SUCCESS_STATUSES:
            return False
        if self.response_code is not None and not 200 <= self.response_code < 300:
            return False
        return self.status is not None or self.response_code is not None

    @property
    def error_hint(self) -> str:
        """Backend-supplied error label used *only* for internal error classification.

        The value is a localized string or an untranslated error key; it is never
        returned verbatim to an MCP client.
        """
        return (self.title or self.message or "").strip()
