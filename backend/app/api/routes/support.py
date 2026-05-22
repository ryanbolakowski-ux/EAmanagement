"""Support contact endpoint — receives a message from the chat bubble
and forwards it to support@thetaalgos.com via Resend.

Anonymous users can also send, but we capture their email + IP for
follow-up. Rate-limited via a simple per-IP in-memory bucket so
nobody can mailbomb us."""
import time
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from loguru import logger

from app.config import settings
from app.services.email import _send, _logo_header


router = APIRouter()


SUPPORT_INBOX = "support@thetaalgos.com"


class ContactRequest(BaseModel):
    from_email: EmailStr
    from_name: Optional[str] = None
    subject: Optional[str] = None
    message: str
    chat_transcript: Optional[str] = None  # last few Q&A turns for context


# Per-IP rate limit — 3 sends per 10 min
_RATE: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 600
_RATE_LIMIT  = 3


def _rate_check(ip: str) -> bool:
    now = time.time()
    bucket = _RATE[ip]
    bucket[:] = [t for t in bucket if (now - t) < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        return False
    bucket.append(now)
    return True


@router.post("/contact", status_code=201)
async def contact_support(data: ContactRequest, request: Request):
    """Forward a user message to the support inbox."""
    ip = request.client.host if request.client else "unknown"
    if not _rate_check(ip):
        raise HTTPException(status_code=429,
                             detail="Too many messages — try again in 10 minutes.")

    user_subject = (data.subject or "").strip() or "New support request"
    safe_subject = f"[Theta Algos Support] {user_subject[:120]}"

    name_block  = f"<strong>{data.from_name}</strong> &lt;{data.from_email}&gt;" if data.from_name else f"&lt;{data.from_email}&gt;"
    transcript_block = ""
    if data.chat_transcript:
        # Pre-format the transcript so it's readable
        transcript_block = f"""
        <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0;"/>
        <div style="font-size:11px;font-weight:800;color:#64748b;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px;">Chat transcript</div>
        <pre style="white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;color:#0f172a;line-height:1.5;">{data.chat_transcript}</pre>
        """

    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Support request — {user_subject}</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">From {name_block} · IP {ip} · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px;">
        <pre style="white-space:pre-wrap;font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.6;color:#0f172a;margin:0;">{data.message}</pre>
      </div>

      {transcript_block}
    </div>
    """

    ok = _send(SUPPORT_INBOX, safe_subject, html)
    if not ok:
        raise HTTPException(status_code=500,
                             detail="Could not send right now — try emailing support@thetaalgos.com directly.")
    logger.info(f"[Support] message from {data.from_email} (IP {ip}) — forwarded to {SUPPORT_INBOX}")
    return {"status": "sent", "support_email": SUPPORT_INBOX}
