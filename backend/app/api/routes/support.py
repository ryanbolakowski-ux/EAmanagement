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


# ─── AI chat endpoint (Mira-style assistant) ──────────────────────────────────
# Replaces the keyword-matched ChatBubble KB with a real LLM. Streams Claude
# responses. Cost: ~$0.0008 per round trip with claude-haiku.
import json as _chat_json
import os as _chat_os
from typing import AsyncIterator as _Iter
from fastapi.responses import StreamingResponse as _Stream
from app.core.auth import get_current_user as _gcu
from app.models.user import User as _U

_CHAT_SYSTEM_PROMPT = '''You are the in-app support assistant for Theta Algos (thetaalgos.com), an algorithmic trading SaaS platform.

You are warm, professional, concise — like a senior fintech support engineer who is also a knowledgeable trader. Use first person naturally. Direct and specific. Real numbers, exact menu paths, real ticker examples. Encouraging when learning, but never sugarcoats risk.

HARD RULES:
- NEVER give specific financial advice. You can explain HOW the platform works, what setups ARE, what the bot would do — but never tell the user what to do with their money.
- Always remind that trading involves risk.
- NEVER fabricate platform features. If unsure, escalate to support@thetaalgos.com.
- Refuse: market manipulation, insider trading, evading prop-firm rules, account sharing, anything illegal.

PLATFORM:
- Pricing tiers: Tier 1 Free Trial (30 days, no card), Tier 2 Futures Signals ($49/mo), Tier 3 Options Scanner ($99/mo), Tier 4 Options Live ($199/mo - most popular), Tier 5 Fully Automated ($399/mo).
- Theta Scanner: morning options pick, fires once/day between 6-9:50 AM ET. Earlier time requires higher score (6am=score 20, 9am=10, 9:25=any). Sends entry, stop -3%, target +10%, gap%, rel vol, score, catalyst.
- 5 scanner strategies: Pre-Market Gap Runner, Low-Float Squeeze (Sykes-style), 52-Week High Breakout, Oracle 5-Min Opening Candle (STT clone), Momentum Gappers.
- ICT futures strategies: FVG Inversion Tap (1m FVG sweep + reclaim), ICT Silver Bullet (10-11am ET kill zone), Liquidity Sweep + FVG. 1 email per user per strategy per session (LONDON/NY_AM/NY_PM/ASIA).
- Brokers: Tradier (stocks+options, free sandbox at developer.tradier.com), Tradovate (futures), prop firms via signal emails (Apex/TPT/Topstep ban algos). Webull/IBKR/TradeStation coming.
- Daily heartbeat email at 9:25 ET confirming scanner is online.
- News blackouts pause scanner ±30 min around FOMC/CPI/PPI/NFP/Core PCE/Retail Sales/GDP (72 events hardcoded for 2026).
- KYC via Stripe Identity (currently in manual-review fallback while Stripe approves API access).
- Pages: /app (home), /app/strategies, /app/backtests, /app/optimization, /app/live-trading, /app/options, /app/account-signals, /app/profile.

TRADING DOMAIN:
- Options Greeks (delta/theta/gamma/vega), strike selection, DTE, IV
- ICT: FVGs, liquidity sweeps, displacement, kill zones (London/NY AM/PM/Asia), PD arrays
- Futures: tick sizes (ES $50/pt, NQ $20/pt, MES $5, MNQ $2), micros vs minis
- Prop firm rules: daily loss limit, trailing drawdown, consistency, news ban
- Pre-market: 4 AM-9:30 AM ET, thin liquidity
- Catalysts: 8-K, earnings, FDA, contracts

STYLE:
- Markdown sparingly. Bold for emphasis, numbered lists for steps. No code blocks unless asked.
- Don't use emoji unless user does.
- Answer the question, offer to go deeper if useful.
- Operational questions (how do I connect Tradier): exact click path.
- Conceptual (whats an FVG"): plain English for beginners, technical for pros.
- Keep responses tight: 1-3 short paragraphs. Long only when asked.
- Off-topic (lunch, weather): redirect — "Im built to help with Theta Algos and trading questions.
- NO greeting, NO signoff, NO let me know if... filler. Just the answer.'''


class _ChatMessage(BaseModel):
    role: str
    content: str


class _ChatRequest(BaseModel):
    messages: list[_ChatMessage]


@router.post('/chat')
async def chat(data: _ChatRequest, request: Request, current_user: _U = Depends(_gcu)):
    api_key = _chat_os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=503, detail='Chat assistant is being configured. Please email support@thetaalgos.com directly.')
    msgs = [m for m in data.messages if m.content.strip()]
    if not msgs:
        raise HTTPException(status_code=400, detail='Empty conversation.')
    if msgs[-1].role != 'user':
        raise HTTPException(status_code=400, detail='Last message must be from user.')
    api_msgs = [{'role': m.role, 'content': m.content} for m in msgs[-20:]]
    system = _CHAT_SYSTEM_PROMPT + f'\n\nThe user you are talking to: {current_user.username or current_user.email} (tier: {current_user.subscription_tier}).'
    model = _chat_os.environ.get('CHAT_MODEL', 'claude-haiku-4-5')

    async def event_stream():
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            async with client.messages.stream(model=model, max_tokens=1024, system=system, messages=api_msgs) as stream:
                async for text in stream.text_stream:
                    yield f"data: {_chat_json.dumps({'delta': text})}\n\n".encode()
                yield b'data: {"done": true}\n\n'
        except Exception as e:
            logger.error(f'[support.chat] {type(e).__name__}: {e}')
            yield f"data: {_chat_json.dumps({'error': str(e)[:200]})}\n\n".encode()

    return _Stream(event_stream(), media_type='text/event-stream')
