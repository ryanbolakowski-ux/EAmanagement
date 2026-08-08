"""TRADE-HORIZON-V1 — Day/Swing trade-type resolution + shared email fragments.

Ryan's rule (2026-08-08): every ENTRY email must explicitly state the trade
type so the recipient knows what to do into the close:

  * Day Trade   -> be flat by the close. PLATFORM TRUTH: stock picks are
                   force-closed at 15:55 ET (3:55 PM ET — 5 minutes before the
                   bell), with an overnight safety-net at the next open plus a
                   catch-up close window. The copy below says 3:55 PM ET, never
                   4:30.
  * Swing Trade -> holding overnight (and over weekends) is EXPECTED; the bot
                   manages the exit.

Source of truth for per-strategy horizon: strategies.trade_horizon VARCHAR(8)
('day' | 'swing'), NULL = 'day'. The column reaches the live DB via the lazy
ALTER below (entry_guard / origin precedent). Stock scanner picks HARD-CODE
'day' — the 15:55 auto-close is unconditional regardless of any column.

Flip a strategy to swing (until the strategy editor grows a control):

    ALTER TABLE strategies ADD COLUMN IF NOT EXISTS trade_horizon VARCHAR(8);
    UPDATE strategies SET trade_horizon='swing' WHERE id='<uuid>';

This module must stay dependency-light: it is imported from the email builders
(app/services/email.py, account_signals routes, theta_scanner) and from the
watcher/paper hot paths. DB access happens only inside the async helpers, via
lazy imports.
"""
import asyncio

from loguru import logger

VALID_HORIZONS = ("day", "swing")

#: Subject suffixes — APPENDED after the existing killswitch-whitelisted
#: prefixes ("🎯 Saro...", "🔥 Saro Signal..."). The killswitch is a pure
#: substring-anywhere check, so appending never breaks delivery. Never touch
#: the prefixes themselves.
SUBJECT_SUFFIX = {"day": " · Day Trade", "swing": " · Swing Trade"}

#: Imperative action lines (plaintext-safe — also reusable outside HTML).
DAY_ACTION_LINE = (
    "This is an intraday setup — the bot exits by 3:55 PM ET today "
    "(auto-close at 15:55 ET, 5 minutes before the bell). "
    "If you mirror it manually, be flat before the close."
)
# Futures + paper books have NO time-based auto-close — the 15:55 ET force-
# close exists ONLY for stock picks (open_positions_watch). The day copy must
# never promise an exit the platform does not perform (2026-08-08 verify).
DAY_ACTION_LINE_FUTURES = (
    "This is an intraday setup \u2014 the bot manages the stop/target exits but does "
    "NOT auto-close futures at a set time. If you mirror it manually, plan to be "
    "flat within your session."
)
FUTURES_ROOTS = {"ES", "NQ", "YM", "RTY", "MES", "MNQ", "M2K", "MYM",
                 "CL", "GC", "SI", "6E", "6J", "ZB", "ZN"}


def surface_for_instrument(instrument) -> str:
    """'futures' when the ticker is a known futures root, else 'stock'."""
    try:
        return "futures" if str(instrument or "").upper() in FUTURES_ROOTS else "stock"
    except Exception:
        return "stock"


SWING_ACTION_LINE = (
    "This is a swing setup — holding overnight (and over weekends) is "
    "expected. The bot manages the exit."
)
ACTION_LINE = {"day": DAY_ACTION_LINE, "swing": SWING_ACTION_LINE}

#: Pill palette: DAY = amber, SWING = indigo (Ryan's spec).
_PILL_STYLE = {
    "day":   {"label": "DAY TRADE",   "accent": "#f59e0b",
              "bg": "#fef3c7", "border": "#f59e0b", "text": "#92400e"},
    "swing": {"label": "SWING TRADE", "accent": "#6366f1",
              "bg": "#e0e7ff", "border": "#6366f1", "text": "#3730a3"},
}


def get_trade_horizon(strategy_or_none=None, source: str = "") -> str:
    """Resolve 'day' | 'swing' with the hard rules:

    * source == 'stock_pick' -> ALWAYS 'day' (the 15:55 ET force-close is
      unconditional; no strategy column can override it).
    * Otherwise: only an explicit 'swing' (from a Strategy row's
      .trade_horizon, or a raw string) returns 'swing'. None / NULL /
      'day' / anything unrecognised -> 'day' (safe default).

    Accepts a Strategy ORM row, a raw string, or None.
    """
    if (source or "").strip().lower() == "stock_pick":
        return "day"
    raw = strategy_or_none
    if raw is not None and not isinstance(raw, str):
        raw = getattr(raw, "trade_horizon", None)
    val = raw.strip().lower() if isinstance(raw, str) else ""
    return "swing" if val == "swing" else "day"


def horizon_subject_suffix(horizon: str) -> str:
    """' · Day Trade' / ' · Swing Trade' — append AFTER the whitelisted
    subject prefix, never before it."""
    return SUBJECT_SUFFIX.get(get_trade_horizon(horizon), SUBJECT_SUFFIX["day"])


def horizon_action_line(horizon: str, surface: str = "stock") -> str:
    """surface='futures' swaps the day line for the no-auto-close truth."""
    h = get_trade_horizon(horizon)
    if h == "day" and surface == "futures":
        return DAY_ACTION_LINE_FUTURES
    return ACTION_LINE.get(h, DAY_ACTION_LINE)


def _legacy_horizon_action_line(horizon: str) -> str:
    """The one imperative instruction line for this horizon (plain text)."""
    return ACTION_LINE.get(get_trade_horizon(horizon), DAY_ACTION_LINE)


def horizon_block_html(horizon: str, surface: str = "stock") -> str:
    """Prominent pill + action line block, injected near the top of every
    entry-email template. Inline-styled (email-safe), no images."""
    h = get_trade_horizon(horizon)
    s = _PILL_STYLE[h]
    return (
        f'<div style="background:{s["bg"]};border:1px solid {s["border"]};'
        f'border-radius:10px;padding:12px 14px;margin:0 0 14px;">'
        f'<span style="display:inline-block;background:{s["accent"]};color:#ffffff;'
        f'font-weight:900;font-size:11px;letter-spacing:0.12em;'
        f'padding:4px 10px;border-radius:6px;">{s["label"]}</span>'
        f'<div style="margin-top:8px;color:{s["text"]};font-size:13px;'
        f'line-height:1.55;font-weight:600;">{horizon_action_line(h, surface)}</div>'
        f'</div>'
    )


async def ensure_trade_horizon_column() -> None:
    """Idempotent lazy ALTER (origin / entry_guard precedent). Warn-and-
    continue on failure — a DDL hiccup must never block a signal path."""
    if getattr(ensure_trade_horizon_column, "_done", False):
        return
    try:
        from sqlalchemy import text
        from app.database import async_session_factory
        async with async_session_factory() as db:
            await db.execute(text(
                "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS trade_horizon VARCHAR(8)"
            ))
            await db.commit()
        ensure_trade_horizon_column._done = True  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning(f"[trade-horizon] ensure_trade_horizon_column failed: {e}")


async def _fetch_horizon_row(strategy_id) -> str:
    await ensure_trade_horizon_column()
    from sqlalchemy import text
    from app.database import async_session_factory
    async with async_session_factory() as db:
        row = (await db.execute(
            text("SELECT trade_horizon FROM strategies WHERE id = :sid"),
            {"sid": str(strategy_id)},
        )).fetchone()
    return get_trade_horizon(row[0] if row else None)


async def fetch_strategy_horizon(strategy_id, timeout_s: float = 3.0) -> str:
    """Async DB lookup of strategies.trade_horizon by id. NULL / missing row /
    missing column / DB down / slow DB (> timeout_s) ALL resolve to 'day' —
    the horizon label must never delay or break an email send path."""
    if not strategy_id:
        return "day"
    try:
        return await asyncio.wait_for(_fetch_horizon_row(strategy_id), timeout=timeout_s)
    except Exception as e:
        logger.warning(
            f"[trade-horizon] lookup failed sid={strategy_id}: "
            f"{type(e).__name__}: {e} — defaulting to 'day'"
        )
        return "day"
