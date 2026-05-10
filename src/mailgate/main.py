"""FastAPI application entrypoint.

MailGate is a thin SMTP forwarder. It exposes a single Resend-compatible
``POST /emails`` endpoint that takes a fully-formed email object (from, to,
subject, html/text, attachments) and ships it through configured SMTP.

Body construction, form parsing, and any application-specific concerns belong
to the caller. MailGate's job is auth + scope enforcement + SMTP.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiosmtplib
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from . import __version__
from .auth import (
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
# shared validation pipeline
# ---------------------------------------------------------------------------

async def _validate(
    request: Request,
    client: ClientConfig,
    *,
    captcha_token: str,
) -> str:
    """Run all pre-SMTP checks. Returns the client IP, raises HTTPException on fail."""
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

    if client.captcha_provider and client.captcha_secret:
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


def _join_prefix(prefix: Optional[str], subject: str) -> str:
    """Prepend a prefix to a subject. Auto-inserts a single space separator unless
    the prefix already ends with whitespace or the subject already begins with it.
    Idempotent: if subject already starts with the prefix, returned unchanged."""
    if not prefix:
        return subject
    if subject.startswith(prefix):
        return subject
    sep = "" if prefix.endswith((" ", "\t")) or subject.startswith((" ", "\t")) else " "
    return f"{prefix}{sep}{subject}"


def _apply_client_defaults(client: ClientConfig, body: EmailRequest) -> EmailRequest:
    """Apply per-client default_from / default_to / subject_prefix when missing."""
    update: dict[str, Any] = {}
    if not body.from_ and client.default_from:
        update["from_"] = client.default_from
    if not body.to and client.default_to:
        update["to"] = list(client.default_to)
    if client.subject_prefix:
        new_subject = _join_prefix(client.subject_prefix, body.subject)
        if new_subject != body.subject:
            update["subject"] = new_subject
    if not update:
        return body
    return body.model_copy(update=update)


async def _do_send(
    request: Request,
    client: ClientConfig,
    body: EmailRequest,
) -> str:
    """Send via SMTP, record metrics + rate-limit success. Returns short id."""
    settings: Settings = request.app.state.settings
    started = time.perf_counter()
    try:
        short_id = await smtp_send(settings, body)
    except aiosmtplib.SMTPException as e:  # type: ignore[attr-defined]
        log.exception("smtp error: %s", e)
        metrics.emails_failed.labels(client=client.name, reason="smtp").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"smtp error: {e}"
        ) from e
    except OSError as e:
        log.exception("network error: %s", e)
        metrics.emails_failed.labels(client=client.name, reason="network").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"smtp connection failed: {e}",
        ) from e

    metrics.emails_sent.labels(client=client.name).inc()
    metrics.send_duration.labels(client=client.name).observe(time.perf_counter() - started)
    ip = get_client_ip(request)
    request.app.state.rate_limiter.record(client, ip)
    return short_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="mailgate",
        version=__version__,
        description="Self-hostable SMTP forwarder with a Resend-compatible HTTP API.",
        lifespan=lifespan,
        # Disable auto-generated docs/schema endpoints. They leak the full API
        # surface to anyone who can hit the deployment. Read README.md instead.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def _cors(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS":
            # 204 No Content MUST have an empty body. Using JSONResponse with
            # content=None serializes b'null' (4 bytes) and trips uvicorn's
            # Content-Length sanity check. Use plain Response to send no body.
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Captcha-Token",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
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
    async def health(
        client: ClientConfig = Depends(_authenticate_dep),  # noqa: ARG001
    ) -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    async def metrics_endpoint(  # type: ignore[no-untyped-def]
        client: ClientConfig = Depends(_authenticate_dep),  # noqa: ARG001
    ):
        return metrics.metrics_response()

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

        short_id = await _do_send(request, client, body)
        return EmailResponse(id=f"msg_{short_id}")

    return app


async def _authenticate_dep(request: Request) -> ClientConfig:
    auth = request.headers.get("authorization")
    return authenticate(request.app.state.clients, auth)


# ASGI entrypoint
app = create_app()


def run() -> None:
    """CLI entrypoint: ``mailgate`` -> uvicorn on env-configured host:port.

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
