"""Smoke tests. Run with `pytest`. SMTP is mocked."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mailgate.main import create_app


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Env-only config (single-client mode)."""
    monkeypatch.setenv("MAILGATE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAILGATE_SMTP_USER", "u")
    monkeypatch.setenv("MAILGATE_SMTP_PASSWORD", "p")
    monkeypatch.setenv("MAILGATE_CLIENT_NAME", "test")
    monkeypatch.setenv("MAILGATE_CLIENT_API_KEY", "mg_test_key_1234567890")
    monkeypatch.setenv("MAILGATE_CLIENT_ALLOWED_ORIGINS", "https://example.com")
    monkeypatch.setenv("MAILGATE_CLIENT_ALLOWED_TO_ADDRESSES", "allowed@y.de")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_FROM", "Test <a@b.de>")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_TO", "allowed@y.de")
    # Note: prefix without trailing space - the server should auto-insert a separator
    monkeypatch.setenv("MAILGATE_CLIENT_SUBJECT_PREFIX", "[Test]")
    monkeypatch.setenv("MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE", "20")
    return create_app()


def test_health_requires_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        assert c.get("/health").status_code == 401
        r = c.get("/health", headers={"Authorization": "Bearer mg_test_key_1234567890"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_metrics_requires_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer mg_test_key_1234567890"})
        assert r.status_code == 200
        assert "mailgate_emails_sent_total" in r.text


def test_docs_endpoints_disabled(app):  # type: ignore[no-untyped-def]
    """Auto-generated FastAPI docs and OpenAPI schema must not be reachable."""
    with TestClient(app) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/redoc").status_code == 404
        assert c.get("/openapi.json").status_code == 404


def test_forms_endpoint_is_gone(app):  # type: ignore[no-untyped-def]
    """Regression: the multipart /forms endpoint was removed in v0.3.

    MailGate is a forwarder, not a form handler. Callers should build the email
    object themselves and POST JSON to /emails.
    """
    with TestClient(app) as c:
        assert c.post("/forms").status_code == 404


def test_options_preflight_has_empty_body(app):  # type: ignore[no-untyped-def]
    """Regression: 204 No Content responses must have an empty body. A previous
    version returned JSONResponse(content=None) which serializes b'null' and
    tripped uvicorn's Content-Length sanity check (RuntimeError in logs)."""
    with TestClient(app) as c:
        r = c.options(
            "/emails",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 204
        assert r.content == b""
        assert r.headers["access-control-allow-origin"] == "https://example.com"
        assert "POST" in r.headers["access-control-allow-methods"]


def test_missing_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            json={"from": "a@b.de", "to": "x@y.de", "subject": "hi", "text": "test"},
        )
        assert r.status_code == 401


def test_invalid_key(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            headers={"Authorization": "Bearer mg_wrong_key_xxxxx"},
            json={"from": "a@b.de", "to": "x@y.de", "subject": "hi", "text": "t"},
        )
        assert r.status_code == 401


def test_origin_blocked(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            headers={
                "Authorization": "Bearer mg_test_key_1234567890",
                "Origin": "https://evil.com",
            },
            json={"from": "a@b.de", "to": "x@y.de", "subject": "hi", "text": "t"},
        )
        assert r.status_code == 403


def test_send_with_attachment(app):  # type: ignore[no-untyped-def]
    payload = {
        "from": "Test <a@b.de>",
        "to": "allowed@y.de",
        "subject": "with attachment",
        "html": "<p>hi</p>",
        "text": "hi",
        "attachments": [
            {
                "filename": "doc.pdf",
                "content": base64.b64encode(b"%PDF-1.4 fake").decode(),
                "content_type": "application/pdf",
            }
        ],
    }
    captured: dict = {}

    async def fake_send(message, **kwargs):  # noqa: ANN001
        captured["message"] = message
        return None

    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(side_effect=fake_send)):
        with TestClient(app) as c:
            r = c.post(
                "/emails",
                headers={
                    "Authorization": "Bearer mg_test_key_1234567890",
                    "Origin": "https://example.com",
                },
                json=payload,
            )
    assert r.status_code == 200, r.text
    assert r.json()["id"].startswith("msg_")
    msg = captured["message"]
    attachments = [
        part for part in msg.iter_attachments() if part.get_filename() == "doc.pdf"
    ]
    assert attachments, "PDF attachment missing from outgoing email"


def test_validation_error_body_required(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            headers={
                "Authorization": "Bearer mg_test_key_1234567890",
                "Origin": "https://example.com",
            },
            json={"from": "a@b.de", "to": "allowed@y.de", "subject": "hi"},
        )
        assert r.status_code == 422


def test_recipient_not_allowed(app):  # type: ignore[no-untyped-def]
    """allowed_to_addresses lockdown."""
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            headers={
                "Authorization": "Bearer mg_test_key_1234567890",
                "Origin": "https://example.com",
            },
            json={
                "from": "a@b.de",
                "to": "evil@elsewhere.com",
                "subject": "phish",
                "text": "...",
            },
        )
        assert r.status_code == 403
        assert "not allowed" in r.json()["error"]


def test_ip_allowlist_rejects_outsiders(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A client with ip_allowlist set rejects all other IPs - the server-side
    'API-key + IP lock' pattern (different from the browser scope-lockdown)."""
    monkeypatch.setenv("MAILGATE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAILGATE_SMTP_USER", "u")
    monkeypatch.setenv("MAILGATE_SMTP_PASSWORD", "p")
    monkeypatch.setenv("MAILGATE_CLIENT_API_KEY", "mg_locked_key_1234567890")
    monkeypatch.setenv("MAILGATE_CLIENT_ALLOWED_TO_ADDRESSES", "allowed@y.de")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_FROM", "Test <a@b.de>")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_TO", "allowed@y.de")
    # Allowlist a CIDR that DOES NOT contain 127.0.0.1 (TestClient's IP)
    monkeypatch.setenv("MAILGATE_CLIENT_IP_ALLOWLIST", "10.0.0.0/8,203.0.113.42")
    app = create_app()
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            headers={"Authorization": "Bearer mg_locked_key_1234567890"},
            json={"subject": "blocked", "text": "..."},
        )
        assert r.status_code == 403
        assert "allowlist" in r.json()["error"]


def test_ip_allowlist_accepts_matching_ip(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """When the caller's IP is in the allowlist (or CIDR), request proceeds.

    Starlette's TestClient sets request.client.host to the literal string
    'testclient' (not a real IP). We patch get_client_ip so the test exercises
    a realistic value.
    """
    monkeypatch.setenv("MAILGATE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAILGATE_SMTP_USER", "u")
    monkeypatch.setenv("MAILGATE_SMTP_PASSWORD", "p")
    monkeypatch.setenv("MAILGATE_CLIENT_API_KEY", "mg_locked_key_1234567890")
    monkeypatch.setenv("MAILGATE_CLIENT_ALLOWED_TO_ADDRESSES", "allowed@y.de")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_FROM", "Test <a@b.de>")
    monkeypatch.setenv("MAILGATE_CLIENT_DEFAULT_TO", "allowed@y.de")
    monkeypatch.setenv("MAILGATE_CLIENT_IP_ALLOWLIST", "203.0.113.0/24")
    app = create_app()
    with (
        patch("mailgate.main.get_client_ip", return_value="203.0.113.42"),
        patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(return_value=None)),
    ):
        with TestClient(app) as c:
            r = c.post(
                "/emails",
                headers={"Authorization": "Bearer mg_locked_key_1234567890"},
                json={"subject": "ok", "text": "..."},
            )
    assert r.status_code == 200, r.text


def test_subject_prefix_auto_spaces(app):  # type: ignore[no-untyped-def]
    """Regression: prefix '[Test]' + subject 'Hello' -> '[Test] Hello' (auto-space)."""
    captured: dict = {}

    async def fake_send(message, **kwargs):  # noqa: ANN001
        captured["subject"] = message["Subject"]
        return None

    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(side_effect=fake_send)):
        with TestClient(app) as c:
            r = c.post(
                "/emails",
                headers={
                    "Authorization": "Bearer mg_test_key_1234567890",
                    "Origin": "https://example.com",
                },
                json={
                    "from": "a@b.de",
                    "to": "allowed@y.de",
                    "subject": "Hello",
                    "text": "x",
                },
            )
    assert r.status_code == 200, r.text
    assert captured["subject"] == "[Test] Hello"


def test_defaults_applied_when_caller_omits(app):  # type: ignore[no-untyped-def]
    """default_from / default_to / subject_prefix all kick in for a minimal payload."""
    captured: dict = {}

    async def fake_send(message, **kwargs):  # noqa: ANN001
        captured["message"] = message
        return None

    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(side_effect=fake_send)):
        with TestClient(app) as c:
            r = c.post(
                "/emails",
                headers={
                    "Authorization": "Bearer mg_test_key_1234567890",
                    "Origin": "https://example.com",
                },
                json={"subject": "Minimal", "text": "ping"},
            )
    assert r.status_code == 200, r.text
    msg = captured["message"]
    assert "Test" in msg["From"] and "a@b.de" in msg["From"]
    assert "allowed@y.de" in msg["To"]
    assert msg["Subject"] == "[Test] Minimal"
