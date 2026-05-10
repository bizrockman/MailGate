"""Bearer-token API key auth + per-client origin allowlist + multi-layer rate limit."""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import Header, HTTPException, Request, status

from .config import ClientConfig

log = logging.getLogger(__name__)

_CAPTCHA_VERIFY_URL = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "hcaptcha": "https://hcaptcha.com/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}


class RateLimiter:
    """Two-phase rate limiter: per-key + optional per-IP, sliding-window minute + day.

    Not cluster-safe (in-memory). For multi-instance deploys, swap for a Redis backend.
    """

    def __init__(self) -> None:
        # Keys are (api_key, ip_or_star).
        self._minute: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._day: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    @staticmethod
    def _trim(bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def check(self, client: ClientConfig, ip: str) -> Optional[str]:
        """Returns None if all limits OK, else a human-readable reason."""
        now = time.time()
        m_cutoff, d_cutoff = now - 60, now - 86400

        # Per-key per-minute (always on)
        b = self._minute[(client.api_key, "*")]
        self._trim(b, m_cutoff)
        if len(b) >= client.rate_limit_per_minute:
            return f"per-minute limit ({client.rate_limit_per_minute}) for key reached"

        # Per-key daily
        if client.daily_limit is not None:
            d = self._day[(client.api_key, "*")]
            self._trim(d, d_cutoff)
            if len(d) >= client.daily_limit:
                return f"daily limit ({client.daily_limit}) for key reached"

        # Per-IP per-minute
        if client.rate_limit_per_minute_per_ip is not None:
            b = self._minute[(client.api_key, ip)]
            self._trim(b, m_cutoff)
            if len(b) >= client.rate_limit_per_minute_per_ip:
                return f"per-minute limit ({client.rate_limit_per_minute_per_ip}) for IP reached"

        # Per-IP daily
        if client.daily_limit_per_ip is not None:
            d = self._day[(client.api_key, ip)]
            self._trim(d, d_cutoff)
            if len(d) >= client.daily_limit_per_ip:
                return f"daily limit ({client.daily_limit_per_ip}) for IP reached"

        return None

    def record(self, client: ClientConfig, ip: str) -> None:
        """Commit a successful pass through all limit windows."""
        now = time.time()
        self._minute[(client.api_key, "*")].append(now)
        self._day[(client.api_key, "*")].append(now)
        if client.rate_limit_per_minute_per_ip is not None:
            self._minute[(client.api_key, ip)].append(now)
        if client.daily_limit_per_ip is not None:
            self._day[(client.api_key, ip)].append(now)


def get_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header (expected 'Bearer <key>')",
        )
    return authorization.split(None, 1)[1].strip()


def authenticate(
    clients: dict[str, ClientConfig],
    authorization: str | None = Header(default=None),
) -> ClientConfig:
    token = get_bearer_token(authorization)
    client = clients.get(token)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )
    return client


def check_origin(client: ClientConfig, request: Request) -> None:
    """Verify request's Origin (or Referer-derived origin) against client's allowed_origins."""
    if not client.allowed_origins:
        return
    if "*" in client.allowed_origins:
        return
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if referer:
            try:
                p = urlparse(referer)
                origin = f"{p.scheme}://{p.netloc}"
            except Exception:  # noqa: BLE001
                origin = None
    if origin not in client.allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"origin {origin!r} is not allowed for this api key",
        )


def check_from_address(client: ClientConfig, from_header: str) -> None:
    if client.allowed_from_addresses is None:
        return
    addr = from_header.rsplit("<", 1)[-1].rstrip(">").strip().lower()
    if addr not in {a.lower() for a in client.allowed_from_addresses}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"sender address {addr!r} is not allowed for this api key",
        )


def check_to_addresses(client: ClientConfig, recipients: list[str]) -> None:
    if client.allowed_to_addresses is None:
        return
    allowed = {a.lower() for a in client.allowed_to_addresses}
    for addr in recipients:
        if addr.lower() not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"recipient {addr!r} is not allowed for this api key",
            )


def check_ip_blocklist(client: ClientConfig, ip: str) -> None:
    """Reject hard-blocked IPs / CIDR ranges."""
    if not client.ip_blocklist:
        return
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return  # cannot parse - let it through; should be rare
    for entry in client.ip_blocklist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    raise HTTPException(status_code=403, detail="ip is blocked")
            else:
                if addr == ipaddress.ip_address(entry):
                    raise HTTPException(status_code=403, detail="ip is blocked")
        except ValueError:
            continue


async def verify_captcha(
    provider: str, secret: str, token: str, remote_ip: str | None = None
) -> bool:
    """Verify a captcha token against the provider's siteverify endpoint.

    Supports: turnstile (Cloudflare), hcaptcha, recaptcha (Google v2/v3).
    All three providers happen to use the same form-data payload shape.
    """
    if not token:
        return False
    url = _CAPTCHA_VERIFY_URL.get(provider)
    if not url:
        log.warning("unknown captcha provider: %s", provider)
        return False
    payload: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.post(url, data=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("%s verify failed: %s", provider, e)
        return False
    return bool(data.get("success") is True)


# Per-provider expected form field names (used by the multipart /forms endpoint).
CAPTCHA_FIELD_NAMES = {
    "turnstile": "cf-turnstile-response",
    "hcaptcha": "h-captcha-response",
    "recaptcha": "g-recaptcha-response",
}


def get_client_ip(request: Request) -> str:
    """Return the client IP. Honors X-Forwarded-For if uvicorn was started with
    --proxy-headers (recommended when running behind Coolify/Traefik/nginx)."""
    if request.client is not None:
        return request.client.host
    return "0.0.0.0"
