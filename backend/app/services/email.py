"""Email sending via Resend.

All sends are best-effort — failures are logged, not raised, so transactional
flows (registration, password reset request) don't fall over when Resend is
flaky or rate-limited.
"""
import resend
from loguru import logger

from app.config import settings



# TRADE_RECEIPT_FIREWALL — single chokepoint cap for all trade-receipt emails.
# Centralized here so we don't have to patch every emit path individually.
import redis.asyncio as _redis_async
import asyncio as _asyncio_lib
from datetime import datetime as _dt_fw, timezone as _tz_fw

_fw_redis = None
def _fw_get_redis():
    global _fw_redis
    if _fw_redis is None:
        url = os.environ.get("REDIS_URL", "redis://edge_redis:6379")
        _fw_redis = _redis_async.from_url(url, decode_responses=True)
    return _fw_redis


def _fw_session_label() -> str:
    """STT-style strict session windows. DEAD outside business windows."""
    try:
        import zoneinfo
        et = _dt_fw.now(_tz_fw.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        et = _dt_fw.now(_tz_fw.utc)
    t = et.hour * 60 + et.minute
    if t >= 18*60 or t < 3*60:      return "ASIA"
    if 3*60 <= t < 9*60:            return "LONDON"
    if 9*60+30 <= t < 12*60:        return "NY_AM"
    if 14*60+30 <= t < 16*60+30:    return "NY_PM"
    return "DEAD"


async def _fw_claim_async(to: str) -> tuple[bool, str]:
    """Atomic claim. Returns (allowed, reason)."""
    sess = _fw_session_label()
    if sess == "DEAD":
        return False, "DEAD_ZONE"
    r = _fw_get_redis()
    day = _dt_fw.now(_tz_fw.utc).date().isoformat()
    # Per-session cap (1 trade receipt per user per session)
    sess_key = f"fw:{to}:{sess}:{day}"
    sess_claimed = await r.set(sess_key, "1", ex=4*3600, nx=True)
    if not sess_claimed:
        return False, f"SESSION_CAP_{sess}"
    # Per-day cap (4 trade receipts total per user per day)
    day_key = f"fw:day:{to}:{day}"
    cur = await r.incr(day_key)
    if cur == 1:
        await r.expire(day_key, 86400)
    if cur > 4:
        # Roll back the session claim so a higher-scored signal in the same
        # session later isn't permanently blocked by an over-cap one
        try: await r.delete(sess_key)
        except Exception: pass
        return False, "DAILY_CAP_4"
    return True, sess


def _fw_check(to: str) -> tuple[bool, str]:
    """Sync wrapper around the async claim. Returns (allowed, reason)."""
    try:
        loop = _asyncio_lib.new_event_loop()
        try:
            return loop.run_until_complete(_fw_claim_async(to))
        finally:
            loop.close()
    except RuntimeError:
        # Already inside an event loop — fall back to running inline
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as ex:
            fut = ex.submit(lambda: _asyncio_lib.run(_fw_claim_async(to)))
            return fut.result(timeout=5)


def _ensure_configured() -> bool:
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set; skipping email send")
        return False
    resend.api_key = settings.RESEND_API_KEY
    return True


def _send(to: str, subject: str, html: str) -> bool:
    import os as _os_es
    if _os_es.environ.get("EMAIL_KILL_SWITCH", "0") == "1":
        s = subject or ""
        # WHITELIST mode: ONLY transactional + Theta Scanner emails pass.
        # Everything else (position logs, signal alerts, trade receipts) is dropped.
        transactional_keywords = ["Reset your", "Verify your", "Welcome to", "2FA",
                                   "verification", "Comp ", "tier change", "Daily digest"]
        is_transactional = any(k in s for k in transactional_keywords)
        is_theta = "Theta Scanner" in s
        if not is_transactional and not is_theta:
            logger.info("[killswitch] dropped (non-whitelist) to=" + str(to) + " subj=" + s[:80])
            return False
    if not _ensure_configured():
        return False
    try:
        resp = resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        logger.info(f"Email sent to {to}: {subject} (id={resp.get('id', '?')})")
        return True
    except Exception as e:
        logger.error(f"Email send failed to {to} ({subject}): {e}")
        return False


def _logo_header() -> str:
    """Text-based purple wordmark — renders identically across light/dark
    email clients (Gmail, Outlook, Yahoo, Apple Mail). Previous version
    used a PNG with black letters that was invisible in dark mode."""
    return """
      <div style="text-align:center;padding:12px 0 24px;">
        <div style="display:inline-block;">
          <span style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
                       font-size:28px;font-weight:900;letter-spacing:0.18em;
                       background:linear-gradient(135deg,#7c3aed 0%,#a78bfa 50%,#c026d3 100%);
                       -webkit-background-clip:text;-webkit-text-fill-color:transparent;
                       background-clip:text;color:#7c3aed;">
            THETA ALGOS
          </span>
          <div style="font-size:9px;font-weight:800;letter-spacing:0.3em;color:#7c3aed;margin-top:2px;">
            EST. 2026
          </div>
        </div>
      </div>
    """


def send_welcome_email(to: str, username: str) -> bool:
    subject = "Welcome to Theta Algos"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 16px;font-size:22px;">Thanks for signing up, {username}!</h1>
      <p style="margin:0 0 12px;color:#475569;line-height:1.55;">
        Your Theta Algos account is ready. You're on the Free Trial — paper trading and 1 year of backtesting are unlocked from day one.
      </p>
      <p style="margin:16px 0 24px;">
        <a href="{settings.FRONTEND_URL}/app" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:10px;">Open the dashboard</a>
      </p>
      <p style="margin:0;color:#94a3b8;font-size:12px;">
        Questions? Just reply to this email.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_password_reset_email(to: str, username: str, token: str) -> bool:
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    subject = "Reset your Theta Algos password"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 16px;font-size:22px;">Reset your password</h1>
      <p style="margin:0 0 12px;color:#475569;line-height:1.55;">
        Hi {username}, we got a request to reset your Theta Algos password. Click the button below to set a new one. This link expires in 1 hour.
      </p>
      <p style="margin:16px 0 24px;">
        <a href="{reset_url}" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:10px;">Reset password</a>
      </p>
      <p style="margin:0 0 8px;color:#94a3b8;font-size:12px;">If the button doesn't work, paste this link into your browser:</p>
      <p style="margin:0 0 16px;color:#475569;font-size:12px;word-break:break-all;">{reset_url}</p>
      <p style="margin:0;color:#94a3b8;font-size:12px;">
        Didn't request this? You can safely ignore this email — your password won't change.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_consistency_hit_email(
    to: str,
    username: str,
    account_name: str,
    daily_pnl: float,
    daily_limit: float,
    profit_target: float,
    consistency_pct: float,
) -> bool:
    """Notify the user that an account hit its daily consistency cap and was paused."""
    subject = f"Theta Algos — {account_name} paused (daily consistency limit hit)"
    fmt = lambda v: f"${v:,.2f}"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 12px;font-size:22px;">Account paused: {account_name}</h1>
      <p style="margin:0 0 16px;color:#475569;line-height:1.55;">
        Hi {username}, your <strong>{account_name}</strong> account has hit its daily consistency cap and trading has been paused for the rest of the trading day.
      </p>
      <div style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:18px;">
        <div style="display:flex;justify-content:space-between;margin-bottom:6px;color:#475569;font-size:13px;"><span>Today's P&amp;L</span><strong style="color:#16a34a;">{fmt(daily_pnl)}</strong></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:6px;color:#475569;font-size:13px;"><span>Daily limit</span><strong>{fmt(daily_limit)}</strong></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:6px;color:#475569;font-size:13px;"><span>Profit target</span><strong>{fmt(profit_target)}</strong></div>
        <div style="display:flex;justify-content:space-between;color:#475569;font-size:13px;"><span>Consistency rule</span><strong>{consistency_pct:.0f}% per day</strong></div>
      </div>
      <p style="margin:0 0 16px;color:#475569;line-height:1.55;">
        The account stays paused until you manually re-enable it from Live Trading. Existing positions are untouched — only new orders are blocked.
      </p>
      <p style="margin:16px 0;">
        <a href="{settings.FRONTEND_URL}/app/live" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:10px;">Review account</a>
      </p>
      <p style="margin:0;color:#94a3b8;font-size:12px;">
        This is an automated message from Theta Algos.
      </p>
    </div>
    """
    return _send(to, subject, html)


# Friendly tier names used in the upgrade/downgrade email body. Keep in sync
# with TIER_LABELS in the frontend so the email reads the same as the UI.
_TIER_NAMES = {
    "free_trial": "Tier 1 (Free Trial)",
    "tier_2":     "Tier 2 (Futures Signals)",
    "tier_3":     "Tier 3 (Options Scanner)",
    "tier_4":     "Tier 4 (Options Live)",
    "tier_5":     "Tier 5 (Fully Automated)",
}
_TIER_RANK = {"free_trial": 0, "tier_2": 2, "tier_3": 3, "tier_4": 4, "tier_5": 5}


def send_daily_digest_email(
    to: str,
    username: str,
    date_str: str,
    total_trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    win_rate: float,
    largest_win: float,
    largest_loss: float,
    paper_pnl: float,
    live_pnl: float,
) -> bool:
    """End-of-day P&L summary, fired by the 4:30 PM ET scheduler."""
    pnl_color = "#16a34a" if net_pnl >= 0 else "#dc2626"
    pnl_sign  = "+" if net_pnl >= 0 else "−"
    fmt = lambda v: f"${abs(v):,.2f}"
    subject = f"Daily summary — {date_str} — {pnl_sign}{fmt(net_pnl).lstrip('$')} P&L"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Daily summary</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">{date_str} · session close</p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px;">
        <div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;">Net P&amp;L</div>
        <div style="font-size:32px;font-weight:800;color:{pnl_color};line-height:1;margin-top:4px;">{pnl_sign}{fmt(net_pnl)}</div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px;">
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;">
          <div style="font-size:10px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;">Trades</div>
          <div style="font-size:20px;font-weight:800;color:#0f172a;margin-top:2px;">{total_trades}</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;">
          <div style="font-size:10px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;">Win rate</div>
          <div style="font-size:20px;font-weight:800;color:#0f172a;margin-top:2px;">{win_rate:.1f}%</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;">
          <div style="font-size:10px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;">Wins / Losses</div>
          <div style="font-size:20px;font-weight:800;margin-top:2px;"><span style="color:#16a34a;">{wins}</span> <span style="color:#cbd5e1;">/</span> <span style="color:#dc2626;">{losses}</span></div>
        </div>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;">
          <div style="font-size:10px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;">Best / Worst</div>
          <div style="font-size:14px;font-weight:700;margin-top:2px;line-height:1.3;">
            <span style="color:#16a34a;">+{fmt(largest_win)}</span><br/>
            <span style="color:#dc2626;">−{fmt(largest_loss)}</span>
          </div>
        </div>
      </div>

      <div style="background:#f1f5f9;border-radius:10px;padding:14px;margin-bottom:18px;font-size:13px;color:#475569;">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px;"><span>Paper P&amp;L</span><strong style="color:{'#16a34a' if paper_pnl >= 0 else '#dc2626'};">{'+' if paper_pnl >= 0 else '−'}{fmt(paper_pnl)}</strong></div>
        <div style="display:flex;justify-content:space-between;"><span>Live P&amp;L</span><strong style="color:{'#16a34a' if live_pnl >= 0 else '#dc2626'};">{'+' if live_pnl >= 0 else '−'}{fmt(live_pnl)}</strong></div>
      </div>

      <p style="margin:16px 0;">
        <a href="{settings.FRONTEND_URL}/app" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:10px;">Review the day</a>
      </p>
      <p style="margin:0;color:#94a3b8;font-size:12px;">
        Sent automatically at 4:30 PM ET. To stop these, reply with "unsubscribe daily".
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_tier_change_email(to: str, username: str, old_tier: str, new_tier: str) -> bool:
    """Notify a user that their plan changed. Used by the admin tier-update
    endpoint and (later) the Stripe webhook on subscription changes."""
    old_label = _TIER_NAMES.get(old_tier, old_tier)
    new_label = _TIER_NAMES.get(new_tier, new_tier)
    upgraded = _TIER_RANK.get(new_tier, 0) > _TIER_RANK.get(old_tier, 0)
    verb = "upgraded" if upgraded else "changed"
    accent = "#16a34a" if upgraded else "#2563eb"
    subject = f"Your Theta Algos plan has been {verb} to {new_label}"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 12px;font-size:22px;">Plan {verb}: {new_label}</h1>
      <p style="margin:0 0 16px;color:#475569;line-height:1.55;">
        Hi {username}, your Theta Algos subscription has been {verb} from
        <strong>{old_label}</strong> to <strong style="color:{accent};">{new_label}</strong>.
        The change is effective immediately — sign in and the new features should already be unlocked.
      </p>
      <p style="margin:16px 0 24px;">
        <a href="{settings.FRONTEND_URL}/app" style="display:inline-block;background:{accent};color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:10px;">Open the dashboard</a>
      </p>
      <p style="margin:0;color:#94a3b8;font-size:12px;">
        Didn't expect this change? Reply to this email and we'll look into it.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_pending_trade_confirm_email(*, to: str, username: str, ticker: str,
                                       direction: str, entry: float, stop: float,
                                       target: float, bias: str, reason: str,
                                       confirm_token: str, expires_at_human: str,
                                       strategy_name: str) -> bool:
    """Pre-market notification with one-click Confirm / Skip buttons.

    The recipient gets this at ~08:30 ET. If they click Confirm, the trade
    fires at the user's auto_execute_delay_min mark (default 08:45 ET).
    """
    side_color = "#16a34a" if direction == "long" else "#dc2626"
    side_word  = "LONG" if direction == "long" else "SHORT"
    confirm_url = f"{settings.FRONTEND_URL}/app/pending/{confirm_token}?action=confirm"
    decline_url = f"{settings.FRONTEND_URL}/app/pending/{confirm_token}?action=decline"
    subject = f"Theta Algos — pre-market signal · {side_word} {ticker} · confirm by {expires_at_human}"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Pre-market signal — {strategy_name}</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">Scanned the universe at 08:30 ET. Top candidate:</p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:18px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          <span style="background:{side_color};color:#fff;font-weight:800;padding:4px 10px;border-radius:8px;font-size:13px;letter-spacing:0.05em;">{side_word}</span>
          <span style="font-weight:800;font-size:22px;color:#0f172a;">{ticker}</span>
        </div>
        <table style="width:100%;font-size:14px;border-collapse:collapse;">
          <tr><td style="padding:5px 0;color:#475569;">Entry price</td><td style="text-align:right;font-weight:700;color:#2563eb;font-size:16px;">{entry:.2f}</td></tr>
          <tr><td style="padding:5px 0;color:#475569;">Stop loss price</td><td style="text-align:right;font-weight:700;color:#dc2626;font-size:16px;">{stop:.2f}</td></tr>
          <tr><td style="padding:5px 0;color:#475569;">Take profit price</td><td style="text-align:right;font-weight:700;color:#16a34a;font-size:16px;">{target:.2f}</td></tr>
          <tr><td style="padding:5px 0;color:#475569;">Bias</td><td style="text-align:right;text-transform:capitalize;">{bias or '—'}</td></tr>
        </table>
        <p style="margin:14px 0 0;color:#64748b;font-size:13px;line-height:1.5;"><em>Why this signal:</em> {reason}</p>
      </div>

      <div style="text-align:center;margin-bottom:18px;">
        <a href="{confirm_url}" style="display:inline-block;background:#16a34a;color:#fff;padding:14px 32px;border-radius:12px;font-weight:800;text-decoration:none;font-size:15px;margin-right:8px;">✓ Confirm — execute at 08:45 ET</a>
        <br style="line-height:14px"/>
        <a href="{decline_url}" style="display:inline-block;color:#64748b;padding:8px 16px;font-size:13px;text-decoration:underline;margin-top:6px;">Skip this one</a>
      </div>

      <p style="margin:0 0 8px;color:#94a3b8;font-size:11px;line-height:1.6;">
        If you don't act by <strong>{expires_at_human}</strong>, the signal expires automatically. Strategies that have auto-execute enabled will fire on confirm only; otherwise the bot waits for your click.
      </p>

      <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0 14px;"/>
      <p style="margin:0;color:#94a3b8;font-size:11px;line-height:1.6;">
        <strong style="color:#64748b;">Disclosure.</strong> This communication reflects automated activity within the proprietary book of <strong>Theta Algos LLC</strong> and is for informational and recordkeeping purposes only. It is not investment advice, a recommendation, or a solicitation. Theta Algos LLC is not a registered investment adviser, broker-dealer, commodity trading advisor, or commodity pool operator. Any decision to confirm and execute this signal in your own account is made solely at your own discretion and risk. Trading involves substantial risk of loss; you may lose more than your initial deposit. Past or hypothetical performance is not indicative of future results.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_trade_receipt_email(*, to: str, username: str, ticker: str,
                               direction: str, entry: float, stop: float,
                               target: float, contracts: int, reason: str,
                               strategy_name: str, mode: str = "paper") -> bool:
    """Signal email: bot has entered (or would enter in paper). Urgent format
    so the user can mirror manually on a prop-firm account."""
    # Firewall: drop email if outside session window or cap hit
    allowed, reason = _fw_check(to)
    if not allowed:
        logger.info(f"[email-firewall] DROPPED {ticker} -> {to} reason={reason}")
        return False
    logger.info(f"[email-firewall] ALLOWED {ticker} -> {to} session={reason}")
    side_color = "#16a34a" if direction == "long" else "#dc2626"
    side_word  = "LONG" if direction == "long" else "SHORT"
    mode_pill  = "PAPER" if mode == "paper" else "LIVE"
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = (reward / risk) if risk > 0 else 0.0
    risk_pct = (risk / entry * 100.0) if entry > 0 else 0.0
    target_pct = (reward / entry * 100.0) if entry > 0 else 0.0
    subject = f"\U0001F525 {side_word} {ticker} @ {entry:.2f} \u00b7 Theta Algos signal (+{target_pct:.1f}% target)"
    urgency_line = (
        f"Bot is targeting +{target_pct:.1f}% continuation in this session. "
        f"Enter NOW at ${entry:.2f} or close to it \u2014 the longer you wait, the worse the entry."
    ) if mode == "paper" else (
        f"Order placed at the broker. The bot is targeting +{target_pct:.1f}% continuation. "
        f"Monitor your fill price; if slippage > 0.5%, consider cancelling."
    )
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}

      <div style="background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 100%);color:white;padding:18px;border-radius:14px;margin-bottom:14px;">
        <div style="font-size:10px;letter-spacing:0.2em;text-transform:uppercase;color:#a78bfa;font-weight:800;margin-bottom:6px;">{strategy_name} \u00b7 {mode_pill}</div>
        <div style="display:flex;align-items:baseline;gap:10px;">
          <span style="background:{side_color};color:#fff;font-weight:900;padding:5px 12px;border-radius:8px;font-size:14px;letter-spacing:0.05em;">{side_word}</span>
          <span style="font-weight:900;font-size:26px;">{ticker}</span>
          <span style="font-size:18px;font-weight:700;opacity:0.85;">@ ${entry:.2f}</span>
        </div>
        <div style="font-size:13px;color:#cbd5e1;margin-top:8px;line-height:1.5;">{urgency_line}</div>
      </div>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px;">
        <table style="width:100%;font-size:14px;border-collapse:collapse;">
          <tr><td style="padding:5px 0;color:#475569;">Entry</td><td style="text-align:right;font-weight:700;color:#2563eb;">${entry:.2f}</td><td style="text-align:right;color:#94a3b8;font-size:11px;">{contracts}\u00d7</td></tr>
          <tr><td style="padding:5px 0;color:#475569;">Stop loss</td><td style="text-align:right;font-weight:700;color:#dc2626;">${stop:.2f}</td><td style="text-align:right;color:#dc2626;font-size:11px;font-weight:600;">\u22121{risk_pct:.1f}%</td></tr>
          <tr><td style="padding:5px 0;color:#475569;">Take profit</td><td style="text-align:right;font-weight:700;color:#16a34a;">${target:.2f}</td><td style="text-align:right;color:#16a34a;font-size:11px;font-weight:600;">+{target_pct:.1f}%</td></tr>
          <tr style="border-top:1px solid #e2e8f0;"><td style="padding:8px 0 0;color:#475569;font-weight:700;">Risk:Reward</td><td style="text-align:right;font-weight:800;color:#0f172a;">1 : {rr:.1f}</td><td></td></tr>
        </table>
        <p style="margin:14px 0 0;color:#64748b;font-size:12px;line-height:1.5;"><strong>Why:</strong> {reason}</p>
      </div>

      <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;padding:16px;margin-bottom:14px;">
        <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:#1e40af;font-weight:800;margin-bottom:8px;">Trading plan</div>
        <table style="width:100%;font-size:12px;color:#1e3a8a;">
          <tr><td style="vertical-align:top;padding:3px 8px 3px 0;font-weight:800;width:90px;">When to enter</td>
              <td style="padding:3px 0;line-height:1.5;">Right now at <strong>${entry:.2f}</strong> or within 0.5%. If price moves more than ${(entry*0.005):.2f} past this level before you place the order, <strong>skip the trade</strong> \u2014 the setup is invalidated.</td></tr>
          <tr><td style="vertical-align:top;padding:3px 8px 3px 0;font-weight:800;">When to exit</td>
              <td style="padding:3px 0;line-height:1.5;"><strong>Take profit</strong> ${target:.2f} (+{target_pct:.1f}%) or <strong>stop loss</strong> ${stop:.2f} (\u22121{risk_pct:.1f}%). <strong>Time stop</strong>: close manually if neither hits within 60 minutes of entry \u2014 the move has stalled.</td></tr>
          <tr><td style="vertical-align:top;padding:3px 8px 3px 0;font-weight:800;">Why now</td>
              <td style="padding:3px 0;line-height:1.5;">{reason}</td></tr>
        </table>
      </div>

      <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:12px;margin-bottom:14px;color:#9a3412;font-size:12px;line-height:1.5;">
        <strong>Risk warning:</strong> Position-sized at {contracts} units for $XX risk. Don\u2019t increase size beyond your daily loss budget. The bot expects this setup to win ~70% of the time historically \u2014 your individual trade could be the 30%.
      </div>

      <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0 14px;"/>
      <p style="margin:0;color:#94a3b8;font-size:11px;line-height:1.55;">
        <strong style="color:#64748b;">Disclosure.</strong> Algorithmic signal from <strong>Theta Algos LLC</strong>. Not investment advice. Trading involves substantial risk of loss; you may lose more than your initial deposit. Past performance does not predict future results.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_consolidated_signals_email(
    *, to: str, username: str, strategy_name: str,
    primary: dict, runners_up: list[dict], expires_at_human: str,
    scan_time_human: str = ""
) -> bool:
    """One email per scan cycle with the top signal + the next ones that
    qualified but ranked lower. Each row gets its own Confirm + Skip link.

    Args expected shape (for both primary and each entry of runners_up):
        {
          "ticker": "NVDA",
          "direction": "long",
          "entry": 220.50, "stop": 215.20, "target": 230.00,
          "bias": "bullish", "reason": "plain-english why",
          "confirm_token": "url-safe-token-abc123",
          "score": 12.3,
        }
    """
    # Firewall: drop consolidated email if outside session or cap hit
    _fw_check_consolidated = True  # marker
    allowed, reason = _fw_check(to)
    if not allowed:
        logger.info(f"[email-firewall] DROPPED consolidated -> {to} reason={reason}")
        return False
    def _side_color(d):
        return "#16a34a" if d == "long" else "#dc2626"
    def _side_word(d):
        return "LONG" if d == "long" else "SHORT"

    p = primary
    side_c = _side_color(p["direction"])
    primary_block = f"""
    <div style="background:#f8fafc;border:2px solid {side_c};border-radius:14px;padding:18px;margin-bottom:18px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
        <span style="background:#fbbf24;color:#7c2d12;font-weight:800;padding:3px 8px;border-radius:6px;font-size:10px;letter-spacing:0.08em;">★ TOP PICK</span>
        <span style="background:{side_c};color:#fff;font-weight:800;padding:4px 10px;border-radius:8px;font-size:13px;letter-spacing:0.05em;">{_side_word(p['direction'])}</span>
        <span style="font-weight:800;font-size:22px;color:#0f172a;">{p['ticker']}</span>
      </div>
      <table style="width:100%;font-size:14px;border-collapse:collapse;margin-bottom:10px;">
        <tr><td style="padding:4px 0;color:#475569;">Entry</td><td style="text-align:right;font-weight:700;color:#2563eb;font-size:16px;">{p['entry']:.2f}</td></tr>
        <tr><td style="padding:4px 0;color:#475569;">Stop</td><td style="text-align:right;font-weight:700;color:#dc2626;font-size:16px;">{p['stop']:.2f}</td></tr>
        <tr><td style="padding:4px 0;color:#475569;">Target</td><td style="text-align:right;font-weight:700;color:#16a34a;font-size:16px;">{p['target']:.2f}</td></tr>
        <tr><td style="padding:4px 0;color:#475569;">Bias</td><td style="text-align:right;text-transform:capitalize;">{p.get('bias') or '—'}</td></tr>
      </table>
      <p style="margin:8px 0 14px;color:#64748b;font-size:13px;line-height:1.5;"><em>Why this signal:</em> {p['reason']}</p>
      <div style="text-align:center;">
        <a href="{settings.FRONTEND_URL}/app/pending/{p['confirm_token']}?action=confirm"
           style="display:inline-block;background:#16a34a;color:#fff;padding:12px 28px;border-radius:10px;font-weight:800;text-decoration:none;font-size:14px;margin-right:6px;">✓ Confirm — execute</a>
        <a href="{settings.FRONTEND_URL}/app/pending/{p['confirm_token']}?action=decline"
           style="display:inline-block;color:#64748b;padding:6px 12px;font-size:12px;text-decoration:underline;">Skip</a>
      </div>
    </div>
    """

    # Runners-up — compact rows, one-click links each
    runners_html = ""
    if runners_up:
        rows_html = ""
        for r in runners_up:
            sc = _side_color(r["direction"])
            rows_html += f"""
            <div style="border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;margin-bottom:8px;background:#ffffff;">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px;">
                <span style="background:{sc};color:#fff;font-weight:700;padding:2px 7px;border-radius:5px;font-size:10px;letter-spacing:0.04em;">{_side_word(r['direction'])}</span>
                <span style="font-weight:800;font-size:15px;color:#0f172a;">{r['ticker']}</span>
                <span style="font-size:11px;color:#94a3b8;">entry {r['entry']:.2f} · stop {r['stop']:.2f} · target {r['target']:.2f}</span>
              </div>
              <p style="margin:0 0 6px;font-size:11px;color:#64748b;line-height:1.45;">{r['reason']}</p>
              <div>
                <a href="{settings.FRONTEND_URL}/app/pending/{r['confirm_token']}?action=confirm"
                   style="display:inline-block;background:#16a34a;color:#fff;padding:5px 12px;border-radius:6px;font-weight:700;text-decoration:none;font-size:11px;margin-right:4px;">✓ Confirm</a>
                <a href="{settings.FRONTEND_URL}/app/pending/{r['confirm_token']}?action=decline"
                   style="display:inline-block;color:#64748b;padding:4px 8px;font-size:11px;text-decoration:underline;">Skip</a>
              </div>
            </div>
            """
        runners_html = f"""
        <div style="margin-bottom:18px;">
          <div style="font-size:11px;font-weight:800;color:#94a3b8;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:8px;">Also qualified — runners-up</div>
          {rows_html}
        </div>
        """

    subject = f"Theta Algos — top pick: {_side_word(primary['direction'])} {primary['ticker']} + {len(runners_up)} more"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">Today's setups — {strategy_name}</h1>
      <p style="margin:0 0 18px;color:#94a3b8;font-size:13px;">{scan_time_human or 'Scanned the universe'} · {len(runners_up) + 1} signals matched the strategy filters.</p>

      {primary_block}

      {runners_html}

      <p style="margin:0 0 8px;color:#94a3b8;font-size:11px;line-height:1.6;">
        If you don't click by <strong>{expires_at_human}</strong>, the signals expire automatically. Confirm any number of them — they're independent.
      </p>

      <hr style="border:none;border-top:1px solid #e2e8f0;margin:18px 0 14px;"/>
      <p style="margin:0;color:#94a3b8;font-size:11px;line-height:1.6;">
        <strong style="color:#64748b;">Disclosure.</strong> This communication reflects automated activity within the proprietary book of <strong>Theta Algos LLC</strong> and is for informational and recordkeeping purposes only. It is not investment advice, a recommendation, or a solicitation. Theta Algos LLC is not a registered investment adviser, broker-dealer, commodity trading advisor, or commodity pool operator. Any decision to confirm and execute a signal in your own account is made solely at your own discretion and risk. Trading involves substantial risk of loss; you may lose more than your initial deposit. Past or hypothetical performance is not indicative of future results.
      </p>
    </div>
    """
    return _send(to, subject, html)



def send_comp_granted_email(to: str, username: str, tier: str,
                              expires_at_human: str, note: str | None = None,
                              granted_by_email: str | None = None) -> bool:
    """Notify a user they've been granted free access to a paid tier."""
    label = _TIER_NAMES.get(tier, tier)
    subject = f"You've been granted free {label} access on Theta Algos"
    note_block = (
        f'<p style="margin:0 0 14px;color:#475569;line-height:1.6;"><em>Note from the team:</em> {note}</p>'
        if note else ""
    )
    granter_line = (f"granted by <strong>{granted_by_email}</strong>" if granted_by_email else "granted by an admin")
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 4px;font-size:22px;">🎁 Free access granted</h1>
      <p style="margin:0 0 14px;color:#94a3b8;font-size:13px;">{granter_line}</p>

      <div style="background:#faf5ff;border:1px solid #d8b4fe;border-radius:12px;padding:18px;margin-bottom:14px;">
        <div style="font-size:11px;font-weight:800;color:#7c3aed;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:6px;">Your plan</div>
        <div style="font-size:22px;font-weight:800;color:#0f172a;margin-bottom:6px;">{label}</div>
        <div style="font-size:13px;color:#475569;">Free until <strong>{expires_at_human}</strong></div>
      </div>

      <p style="margin:0 0 14px;color:#475569;line-height:1.6;">
        Hi {username}, you now have <strong>complimentary {label}</strong> on Theta Algos until <strong>{expires_at_human}</strong>. Every feature unlocked at that tier is yours — no credit card on file, nothing to pay. The morning scanner email, the dashboard, the Tradier-routed live trading — all of it.
      </p>
      {note_block}
      <p style="margin:18px 0 0;">
        <a href="{settings.FRONTEND_URL}/app" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;font-weight:700;padding:11px 20px;border-radius:10px;">Open the dashboard →</a>
      </p>
      <p style="margin:18px 0 0;color:#94a3b8;font-size:11px;line-height:1.55;">
        When the free window ends you'll be auto-downgraded to Tier 1 (Free Trial) — you can subscribe at any time from your Profile page to keep going.
      </p>
    </div>
    """
    return _send(to, subject, html)


def send_comp_revoked_email(to: str, username: str, prior_tier: str) -> bool:
    """Notify a user that their free access was ended early by an admin."""
    label = _TIER_NAMES.get(prior_tier, prior_tier)
    subject = "Your Theta Algos free access has ended"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      {_logo_header()}
      <h1 style="margin:0 0 12px;font-size:22px;">Free access ended</h1>
      <p style="margin:0 0 14px;color:#475569;line-height:1.6;">
        Hi {username}, your complimentary <strong>{label}</strong> access on Theta Algos has been ended by an admin. Your account is now on <strong>Tier 1 (Free Trial)</strong> — you can still log in, paper-trade, and preview the scanner.
      </p>
      <p style="margin:0 0 14px;color:#475569;line-height:1.6;">
        Want to keep the full scanner running? Pick a paid plan from your Profile and you're back online immediately.
      </p>
      <p style="margin:16px 0 0;">
        <a href="{settings.FRONTEND_URL}/app/profile" style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;font-weight:700;padding:11px 20px;border-radius:10px;">Manage plan →</a>
      </p>
      <p style="margin:18px 0 0;color:#94a3b8;font-size:11px;">
        If this happened by mistake, reply to this email and we'll restore your access.
      </p>
    </div>
    """
    return _send(to, subject, html)
