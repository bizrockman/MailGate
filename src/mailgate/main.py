"""FastAPI application entrypoint."""

from __future__ import annotations

import base64
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosmtplib
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .auth import (
    CAPTCHA_FIELD_NAMES,
    RateLimiter,
    authenticate,
    check_from_address,
    check_ip_blocklist,
    check_origin,
    check_to_addresses,
    get_client_ip,
    verify_captcha,
)
from . import metrics
from .body_builder import render_html, render_text
from .config import ClientConfig, Settings, load_clients
from .models import EmailRequest, EmailResponse, ErrorResponse
from .smtp import send as smtp_send

log = logging.getLogger("mailgate")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = Settings()  # type: ignore[call-arg]
    _setup_logging(settings.log_level)
    clients = load_clients(settings)
    log.info(
        "loaded %d client(s): %s",
        len(clients),
        ", ".join(c.name for c in clients.values()),
    )
    app.state.settings = settings
    app.state.clients = clients
    app.state.rate_limiter = RateLimiter()
    yield


# ---------------------------------------------------------------------------
# shared validation pipeline (used by /emails and /forms)
# ---------------------------------------------------------------------------

async def _validate(
    request: Request,
    client: ClientConfig,
    *,
    captcha_token: str,
    skip_captcha: bool = False,
) -> str:
    """Run all pre-SMTP checks. Returns the client IP, raises HTTPException on fail.

    Honeypot is checked at the route level (different field semantics per endpoint).
    """
    ip = get_client_ip(request)
    try:
        check_ip_blocklist(client, ip)
    except HTTPException as e:
        metrics.requests_blocked.labels(reason="ip_blocked").inc()
        raise e
    try:
        check_origin(client, request)
    except HTTPException as e:
        metrics.requests_blocked.labels(reason="origin_denied").inc()
        raise e

    if client.captcha_provider and client.captcha_secret and not skip_captcha:
        ok = await verify_captcha(
            client.captcha_provider, client.captcha_secret, captcha_token, ip
        )
        if not ok:
            metrics.requests_blocked.labels(reason="captcha_failed").inc()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="captcha verification failed",
            )

    limiter: RateLimiter = request.app.state.rate_limiter
    reason = limiter.check(client, ip)
    if reason:
        metrics.requests_blocked.labels(reason="rate_limited").inc()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)

    return ip


def _apply_client_defaults(client: ClientConfig, body: EmailRequest) -> EmailRequest:
    """Apply per-client default_from / default_to / subject_prefix when missing."""
    update: dict[str, Any] = {}
    if not body.from_ and client.default_from:
        update["from_"] = client.default_from
    if not body.to and client.default_to:
        update["to"] = list(client.default_to)
    if client.subject_prefix:
        prefix = client.subject_prefix
        if not body.subject.startswith(prefix):
            update["subject"] = prefix + body.subject
    if not update:
        return body
    return body.model_copy(update=update)


async def _do_send(
    request: Request,
    client: ClientConfig,
    body: EmailRequest,
    *,
    endpoint: str,
) -> str:
    """Send via SMTP, record metrics + rate-limit success. Returns short id."""
    settings: Settings = request.app.state.settings
    started = time.perf_counter()
    try:
        short_id = await smtp_send(settings, body)
    except aiosmtplib.SMTPException as e:  # type: ignore[attr-defined]
        log.exception("smtp error: %s", e)
        metrics.emails_failed.labels(client=client.name, endpoint=endpoint, reason="smtp").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"smtp error: {e}"
        ) from e
    except OSError as e:
        log.exception("network error: %s", e)
        metrics.emails_failed.labels(client=client.name, endpoint=endpoint, reason="network").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"smtp connection failed: {e}",
        ) from e

    metrics.emails_sent.labels(client=client.name, endpoint=endpoint).inc()
    metrics.send_duration.labels(client=client.name).observe(time.perf_counter() - started)
    ip = get_client_ip(request)
    request.app.state.rate_limiter.record(client, ip)
    return short_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Form fields that are metadata, never copied into the email body.
_FORM_META_FIELDS = {
    "api_key",
    "redirect",
    "redirect_error",
    "subject",
    "from",
    "to",
    "cc",
    "bcc",
    "reply_to",
    "botcheck",
    *CAPTCHA_FIELD_NAMES.values(),
}


def create_app() -> FastAPI:
    app = FastAPI(
        title="mailgate",
        version=__version__,
        description="Lightweight self-hostable mail relay with a Resend-compatible HTTP API.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _cors(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS":
            return JSONResponse(
                status_code=204,
                content=None,
                headers={
                    "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Captcha-Token",
                    "Access-Control-Max-Age": "600",
                },
            )
        response = await call_next(request)
        origin = request.headers.get("origin")
        if origin:
            response.headers.setdefault("Access-Control-Allow-Origin", origin)
            response.headers.setdefault("Vary", "Origin")
        return response

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        code_map = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            413: "payload_too_large",
            422: "validation_error",
            429: "rate_limited",
            500: "internal_error",
            502: "smtp_error",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=str(exc.detail), code=code_map.get(exc.status_code, "error")
            ).model_dump(),
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    async def metrics_endpoint():  # type: ignore[no-untyped-def]
        return metrics.metrics_response()

    # -------------------------- /emails (JSON, Resend-compatible) --------------------------

    @app.post(
        "/emails",
        response_model=EmailResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def send_email(
        request: Request,
        body: EmailRequest,
        client: ClientConfig = Depends(_authenticate_dep),
    ) -> EmailResponse:
        body = _apply_client_defaults(client, body)
        if not body.from_:
            raise HTTPException(422, "from is required (no default_from configured)")
        if not body.to_list:
            raise HTTPException(422, "to is required (no default_to configured)")

        check_from_address(client, body.from_)
        check_to_addresses(client, body.to_list + body.cc_list + body.bcc_list)

        captcha_token = request.headers.get("x-captcha-token") or request.headers.get(
            "x-turnstile-token", ""
        )
        await _validate(request, client, captcha_token=captcha_token)

        short_id = await _do_send(request, client, body, endpoint="emails")
        return EmailResponse(id=f"msg_{short_id}")

    # -------------------------- /forms (HTML form, multipart) ------------------------------

    @app.post("/forms")
    async def submit_form(request: Request):  # type: ignore[no-untyped-def]
        clients: dict[str, ClientConfig] = request.app.state.clients
        form = await request.form()

        # Auth: Bearer header OR hidden api_key field
        auth = request.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
        else:
            token = str(form.get("api_key") or "").strip()
        if not token or token not in clients:
            metrics.requests_blocked.labels(reason="auth_failed").inc()
            raise HTTPException(401, "missing or invalid api_key")
        client = clients[token]

        # Honeypot (instant fake-success — bots get a 200 redirect, never see SMTP)
        if str(form.get("botcheck") or "").strip():
            log.info("honeypot tripped client=%s", client.name)
            metrics.requests_blocked.labels(reason="honeypot").inc()
            ok_redirect = str(form.get("redirect") or "")
            if ok_redirect:
                return RedirectResponse(ok_redirect, status_code=303)
            return JSONResponse({"id": "msg_honeypot"}, 200)

        # Captcha token: from provider-specific field name
        captcha_token = ""
        if client.captcha_provider:
            field = CAPTCHA_FIELD_NAMES.get(client.captcha_provider, "")
            captcha_token = str(form.get(field) or "")
        await _validate(request, client, captcha_token=captcha_token)

        # Build EmailRequest from form fields
        from_addr = str(form.get("from") or "").strip() or client.default_from
        if not from_addr:
            raise HTTPException(422, "from is required (no default_from configured)")

        to_field = form.getlist("to")
        if not to_field:
            to_field = list(client.default_to or [])
        if not to_field:
            raise HTTPException(422, "to is required (no default_to configured)")

        cc_field = form.getlist("cc")
        bcc_field = form.getlist("bcc")
        reply_to = str(form.get("reply_to") or form.get("email") or "").strip() or None
        subject_user = str(form.get("subject") or "").strip()
        subject = subject_user or "Form submission"
        if client.subject_prefix and not subject.startswith(client.subject_prefix):
            subject = client.subject_prefix + subject

        # Body construction: every non-meta, non-file field becomes a row
        body_fields: list[tuple[str, str]] = []
        attachments_data: list[dict[str, str]] = []

        for key, value in form.multi_items():
            if key in _FORM_META_FIELDS:
                continue
            if isinstance(value, UploadFile):
                content = await value.read()
                attachments_data.append(
                    {
                        "filename": value.filename or key,
                        "content": base64.b64encode(content).decode("ascii"),
                        "content_type": value.content_type or "application/octet-stream",
                    }
                )
                continue
            body_fields.append((key, str(value)))

        intro = f"Neue Anfrage über {client.name} ({client.subject_prefix or 'Formular'})"
        html = render_html(body_fields, intro=intro)
        text = render_text(body_fields, intro=intro)

        email_req = EmailRequest.model_validate({
            "from": from_addr,
            "to": to_field,
            "cc": cc_field or None,
            "bcc": bcc_field or None,
            "reply_to": reply_to,
            "subject": subject,
            "html": html,
            "text": text,
            "attachments": attachments_data or None,
        })

        check_from_address(client, email_req.from_)
        check_to_addresses(
            client, email_req.to_list + email_req.cc_list + email_req.bcc_list
        )

        try:
            short_id = await _do_send(request, client, email_req, endpoint="forms")
        except HTTPException as e:
            err_redirect = str(form.get("redirect_error") or "")
            if err_redirect:
                return RedirectResponse(err_redirect, status_code=303)
            raise e

        ok_redirect = str(form.get("redirect") or "")
        if ok_redirect:
            return RedirectResponse(ok_redirect, status_code=303)
        return JSONResponse({"id": f"msg_{short_id}"}, 200)

    return app


async def _authenticate_dep(request: Request) -> ClientConfig:
    auth = request.headers.get("authorization")
    return authenticate(request.app.state.clients, auth)


# ASGI entrypoint
app = create_app()


def run() -> None:
    """CLI entrypoint: `mailgate` -> uvicorn on env-configured host:port.

    Honors X-Forwarded-For when run behind a reverse proxy (Coolify/Traefik/nginx).
    """
    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run(
        "mailgate.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
