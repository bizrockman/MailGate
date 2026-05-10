"""Request and response schemas for the /emails endpoint.

Compatible with Resend's API shape: same field names so existing client SDKs work.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# RFC-5322-ish: "Name <addr@host>" or just "addr@host"
_FROM_PATTERN = re.compile(
    r"^(?:(?P<name>.+?)\s*<(?P<addr>[^>]+)>|(?P<bare>[^<>\s]+@[^<>\s]+))$"
)


def parse_from(value: str) -> tuple[Optional[str], str]:
    """Split 'Name <addr@host>' into (name, addr). Returns (None, addr) for bare addresses."""
    m = _FROM_PATTERN.match(value.strip())
    if not m:
        raise ValueError(f"Invalid from format: {value!r}")
    if m.group("bare"):
        return None, m.group("bare")
    return m.group("name").strip(), m.group("addr").strip()


class Attachment(BaseModel):
    """One email attachment, content base64-encoded."""

    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(description="Base64-encoded file content")
    content_type: Optional[str] = None
    """Defaults to application/octet-stream if not provided."""

    @field_validator("content")
    @classmethod
    def _validate_b64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError("attachment content must be valid base64") from e
        return v

    def decoded(self) -> bytes:
        return base64.b64decode(self.content, validate=True)


class EmailRequest(BaseModel):
    """Resend-compatible email send request.

    ``from`` and ``to`` are technically optional in the schema so the server can fill
    them from per-client defaults (default_from / default_to). The route handler
    enforces presence after defaults are applied.
    """

    from_: Optional[str] = Field(
        default=None, alias="from", description="Sender, e.g. 'Name <addr@host>'"
    )
    to: Optional[list[EmailStr] | EmailStr] = None
    cc: Optional[list[EmailStr] | EmailStr] = None
    bcc: Optional[list[EmailStr] | EmailStr] = None
    reply_to: Optional[list[EmailStr] | EmailStr] = None
    subject: str = Field(min_length=1, max_length=998)
    html: Optional[str] = None
    text: Optional[str] = None
    attachments: Optional[list[Attachment]] = None
    headers: Optional[dict[str, str]] = None

    model_config = {"populate_by_name": True}

    @field_validator("html", "text")
    @classmethod
    def _at_least_one_body(cls, v: Optional[str]) -> Optional[str]:
        return v

    def model_post_init(self, __context: object) -> None:  # type: ignore[override]
        if not self.html and not self.text:
            raise ValueError("Either 'html' or 'text' (or both) must be provided")

    @staticmethod
    def _to_list(v: list[str] | str | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @property
    def to_list(self) -> list[str]:
        return self._to_list(self.to)

    @property
    def cc_list(self) -> list[str]:
        return self._to_list(self.cc)

    @property
    def bcc_list(self) -> list[str]:
        return self._to_list(self.bcc)

    @property
    def reply_to_list(self) -> list[str]:
        return self._to_list(self.reply_to)


class EmailResponse(BaseModel):
    id: str


class ErrorResponse(BaseModel):
    error: str
    code: str
