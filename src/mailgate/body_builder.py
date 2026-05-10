"""Render an HTML+text email body from key/value form fields.

Used by the /forms endpoint to give callers a sensible default body without
forcing them to assemble HTML themselves.
"""

from __future__ import annotations

from html import escape


def _format_label(key: str) -> str:
    """`your-name` -> 'Your Name', `firma` -> 'Firma'."""
    return key.replace("_", " ").replace("-", " ").strip().title()


def render_html(fields: list[tuple[str, str]], *, intro: str | None = None) -> str:
    parts: list[str] = [
        '<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;max-width:600px;color:#1f1f1f">'
    ]
    if intro:
        parts.append(f"<p>{escape(intro)}</p>")
    parts.append('<table cellpadding="6" cellspacing="0" border="0" style="border-collapse:collapse;width:100%">')
    for key, value in fields:
        label = escape(_format_label(key))
        # Multiline values rendered with <br>
        rendered = "<br>".join(escape(line) for line in value.splitlines() or [""])
        parts.append(
            f'<tr style="border-bottom:1px solid #e5e5e5">'
            f'<td style="font-weight:600;vertical-align:top;width:30%">{label}</td>'
            f"<td>{rendered or '<em style=\"color:#999\">(leer)</em>'}</td>"
            f"</tr>"
        )
    parts.append("</table></body></html>")
    return "".join(parts)


def render_text(fields: list[tuple[str, str]], *, intro: str | None = None) -> str:
    out: list[str] = []
    if intro:
        out.append(intro)
        out.append("")
    for key, value in fields:
        label = _format_label(key)
        if "\n" in value:
            out.append(f"{label}:")
            for line in value.splitlines():
                out.append(f"  {line}")
        else:
            out.append(f"{label}: {value}")
    return "\n".join(out)
