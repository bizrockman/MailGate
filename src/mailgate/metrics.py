"""Prometheus metrics. Exposed at GET /metrics."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response


emails_sent = Counter(
    "mailgate_emails_sent_total",
    "Number of emails successfully sent.",
    labelnames=["client", "endpoint"],
)
emails_failed = Counter(
    "mailgate_emails_failed_total",
    "Number of email sends that failed (SMTP error, validation, etc).",
    labelnames=["client", "endpoint", "reason"],
)
requests_blocked = Counter(
    "mailgate_requests_blocked_total",
    "Requests rejected before SMTP (auth, scope, rate-limit, captcha).",
    labelnames=["reason"],
)
send_duration = Histogram(
    "mailgate_send_duration_seconds",
    "End-to-end SMTP send latency.",
    labelnames=["client"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
