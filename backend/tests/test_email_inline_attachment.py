"""Unit tests for Resend inline-attachment support on _send_tracked.

Run standalone:
    pytest backend/tests/test_email_inline_attachment.py -v -p no:cacheprovider

Covers the contract used by the futures + stock trade-chart emails:
  - When `inline_png=<bytes>` is passed, the outbound Resend payload carries
    exactly one attachment whose `content_id` is "tradechart" (so the HTML can
    reference it via <img src="cid:tradechart">) and whose `content` is the
    base64 encoding of the PNG bytes.
  - A custom `inline_cid` is honored.
  - When no `inline_png` is passed, the payload has NO `attachments` key — a
    plain email is unchanged.
"""
import base64


def _stub_resend_ok():
    """Patch target for httpx.post that captures the outbound payload and
    pretends Resend accepted the send, without ever leaving the box."""
    captured = {}

    class _StubResp:
        status_code = 200
        text = ""
        def json(self):
            return {"id": "stub-resend-id"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _StubResp()

    return _post, captured


def _patch_send(monkeypatch):
    """Common setup: enter the send branch + capture the payload."""
    monkeypatch.delenv("EMAIL_KILL_SWITCH", raising=False)
    from app.services import email as email_mod
    monkeypatch.setattr(email_mod.settings, "RESEND_API_KEY", "stub-key", raising=False)
    monkeypatch.setattr(email_mod.settings, "EMAIL_FROM", "test@thetaalgos.test", raising=False)
    _post, captured = _stub_resend_ok()
    import httpx
    monkeypatch.setattr(httpx, "post", _post, raising=False)
    return email_mod, captured


def test_inline_png_adds_attachment(monkeypatch):
    """inline_png -> attachments[0].content_id == 'tradechart' + base64 content."""
    email_mod, captured = _patch_send(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"fake-chart-bytes-0123456789"

    result = email_mod._send_tracked(
        to="user@example.com",
        subject="Theta Scanner (Futures): LONG ES @ 5000.00",
        html='<img src="cid:tradechart"/>',
        inline_png=png,
    )
    assert result["sent"] is True, f"send should succeed; got {result!r}"

    payload = captured["payload"]
    assert "attachments" in payload, f"attachments key missing; payload keys={list(payload)}"
    atts = payload["attachments"]
    assert isinstance(atts, list) and len(atts) == 1, f"expected exactly 1 attachment; got {atts!r}"
    att = atts[0]
    assert att["content_id"] == "tradechart", f"content_id must be 'tradechart'; got {att.get('content_id')!r}"
    assert att["filename"] == "trade.png", f"filename should be trade.png; got {att.get('filename')!r}"
    # content must be the base64 of the PNG bytes and round-trip exactly.
    assert att["content"] == base64.b64encode(png).decode(), "content is not base64(png)"
    assert base64.b64decode(att["content"]) == png, "base64 content does not decode back to the PNG"


def test_custom_inline_cid_is_honored(monkeypatch):
    """A non-default inline_cid is used verbatim as the attachment content_id."""
    email_mod, captured = _patch_send(monkeypatch)
    png = b"\x89PNG\r\n\x1a\nxyz"

    result = email_mod._send_tracked(
        to="user@example.com",
        subject="Theta Scanner (Futures): LONG ES @ 5000.00",
        html='<img src="cid:mychart"/>',
        inline_png=png,
        inline_cid="mychart",
    )
    assert result["sent"] is True
    att = captured["payload"]["attachments"][0]
    assert att["content_id"] == "mychart", f"custom cid not honored; got {att.get('content_id')!r}"


def test_no_inline_png_has_no_attachments(monkeypatch):
    """Without inline_png the payload must NOT contain an attachments key."""
    email_mod, captured = _patch_send(monkeypatch)

    result = email_mod._send_tracked(
        to="user@example.com",
        subject="Theta Scanner (Futures): LONG ES @ 5000.00",
        html="<p>no chart here</p>",
    )
    assert result["sent"] is True
    assert "attachments" not in captured["payload"], (
        "plain emails must not carry an attachments key; "
        f"payload keys={list(captured['payload'])}"
    )
