"""Smoke tests. Run with `pytest`. SMTP is mocked."""

from __future__ import annotations

import base64
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mailgate.config import ClientConfig
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
    monkeypatch.setenv("MAILGATE_CLIENT_FORM_INTRO", "Neue Anfrage")
    monkeypatch.setenv("MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE", "20")
    return create_app()


def test_health_requires_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        # No auth -> 401
        assert c.get("/health").status_code == 401
        # With auth -> 200
        r = c.get("/health", headers={"Authorization": "Bearer mg_test_key_1234567890"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_docs_endpoints_disabled(app):  # type: ignore[no-untyped-def]
    """Auto-generated FastAPI docs and OpenAPI schema must not be reachable."""
    with TestClient(app) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/redoc").status_code == 404
        assert c.get("/openapi.json").status_code == 404


def test_missing_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        r = c.post(
            "/emails",
            json={
                "from": "a@b.de",
                "to": "x@y.de",
                "subject": "hi",
                "text": "test",
            },
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
    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(return_value=None)):
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
    """allowed_to_addresses is the most important lockdown - verify it works."""
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


def test_metrics_requires_auth(app):  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer mg_test_key_1234567890"})
        assert r.status_code == 200
        assert "mailgate_emails_sent_total" in r.text


def test_form_endpoint_with_defaults_and_attachment(app):  # type: ignore[no-untyped-def]
    """The /forms endpoint: HTML-form-mode with multipart, defaults applied,
    file attachment, redirect on success."""
    files = {
        # Form-style POST with one file and several text fields
        "api_key": (None, "mg_test_key_1234567890"),
        "firma": (None, "ACME GmbH"),
        "email": (None, "max@example.com"),
        "notiz": (None, "Bitte um Rückruf"),
        "redirect": (None, "https://example.com/danke/"),
        "gewerbenachweis": ("nachweis.pdf", BytesIO(b"%PDF-1.4 fake"), "application/pdf"),
    }
    captured: dict = {}

    async def fake_send(*args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["kwargs"] = kwargs
        return None

    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(side_effect=fake_send)):
        with TestClient(app) as c:
            r = c.post(
                "/forms",
                files=files,
                headers={"Origin": "https://example.com"},
                follow_redirects=False,
            )

    assert r.status_code == 303, r.text
    assert r.headers["location"] == "https://example.com/danke/"
    # SMTP was called -> defaults filled (default_from, default_to, subject_prefix)
    assert "kwargs" in captured


def test_form_honeypot_silently_succeeds(app):  # type: ignore[no-untyped-def]
    """Bots fill all fields including honeypot; we fake-success without sending."""
    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock()) as mock_send:
        with TestClient(app) as c:
            r = c.post(
                "/forms",
                files={
                    "api_key": (None, "mg_test_key_1234567890"),
                    "firma": (None, "ACME"),
                    "botcheck": (None, "yes-i-am-a-bot"),
                    "redirect": (None, "https://example.com/danke/"),
                },
                headers={"Origin": "https://example.com"},
                follow_redirects=False,
            )
        assert r.status_code == 303
        mock_send.assert_not_called()


def test_form_attachment_actually_attached_not_stringified(app):  # type: ignore[no-untyped-def]
    """Regression: UploadFile values must become real attachments, never get
    rendered as text in the body. Uses Starlette's UploadFile under the hood."""
    captured: dict = {}

    async def fake_send(message, **kwargs):  # noqa: ANN001
        captured["message"] = message
        return None

    with patch("mailgate.smtp.aiosmtplib.send", new=AsyncMock(side_effect=fake_send)):
        with TestClient(app) as c:
            r = c.post(
                "/forms",
                files={
                    "api_key": (None, "mg_test_key_1234567890"),
                    "firma": (None, "ACME GmbH"),
                    "gewerbenachweis": (
                        "doc.pdf",
                        BytesIO(b"%PDF-1.4 attached"),
                        "application/pdf",
                    ),
                    "redirect": (None, "https://example.com/danke/"),
                },
                headers={"Origin": "https://example.com"},
                follow_redirects=False,
            )
    assert r.status_code == 303
    msg = captured["message"]
    # The MIME message must have an attachment part
    attachments = [
        part for part in msg.iter_attachments() if part.get_filename() == "doc.pdf"
    ]
    assert attachments, "PDF attachment missing from outgoing email"
    # Body must NOT contain the python repr of UploadFile
    body_text = msg.get_body(("plain",)).get_content() if msg.get_body(("plain",)) else ""
    assert "UploadFile(" not in body_text
    assert "gewerbenachweis" not in body_text.lower()


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
