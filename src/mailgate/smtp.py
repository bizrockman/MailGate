"""SMTP send helper. Builds a MIME message and ships it via aiosmtplib."""

from __future__ import annotations

import logging
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import aiosmtplib

from .config import Settings
from .models import EmailRequest, parse_from

log = logging.getLogger(__name__)


def _format_recipient_list(addresses: list[str]) -> str:
    return ", ".join(addresses)


def build_message(req: EmailRequest, *, msg_id_domain: str) -> tuple[EmailMessage, str]:
    """Build a MIME EmailMessage from the request. Returns (message, message_id)."""
    msg = EmailMessage()
    name, addr = parse_from(req.from_)
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = _format_recipient_list(req.to_list)
    if req.cc_list:
        msg["Cc"] = _format_recipient_list(req.cc_list)
    if req.reply_to_list:
        msg["Reply-To"] = _format_recipient_list(req.reply_to_list)
    msg["Subject"] = req.subject
    msg["Date"] = formatdate(localtime=True)
    msg_id = make_msgid(domain=msg_id_domain)
    msg["Message-ID"] = msg_id

    if req.headers:
        for k, v in req.headers.items():
            # Don't allow overriding our managed headers
            if k.lower() in {"from", "to", "cc", "bcc", "subject", "date", "message-id"}:
                continue
            msg[k] = v

    if req.text and req.html:
        msg.set_content(req.text)
        msg.add_alternative(req.html, subtype="html")
    elif req.html:
        msg.set_content(req.html, subtype="html")
    else:
        assert req.text  # validated in model
        msg.set_content(req.text)

    if req.attachments:
        for att in req.attachments:
            ct = att.content_type or "application/octet-stream"
            maintype, _, subtype = ct.partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(
                att.decoded(),
                maintype=maintype,
                subtype=subtype,
                filename=att.filename,
            )

    return msg, msg_id


async def send(settings: Settings, req: EmailRequest) -> str:
    """Send an email via SMTP. Returns a short message id (without angle brackets)."""
    sender_domain = req.from_.rsplit("@", 1)[-1].rstrip(">").strip()
    msg, full_msg_id = build_message(req, msg_id_domain=sender_domain)

    rcpts = req.to_list + req.cc_list + req.bcc_list
    if not rcpts:
        raise ValueError("at least one recipient is required")

    log.info(
        "sending mail msg_id=%s from=%s to=%s subject=%r",
        full_msg_id,
        req.from_,
        rcpts,
        req.subject,
    )

    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        start_tls=settings.smtp_use_tls,
        timeout=settings.smtp_timeout,
        recipients=rcpts,
    )

    short = uuid.uuid4().hex[:24]
    log.info("sent mail short_id=%s smtp_msg_id=%s", short, full_msg_id)
    return short
