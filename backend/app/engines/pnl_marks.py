"""Shared mark-to-market session rules so open P&L doesn't drift while the
cash market is closed.

Root cause of "open P&L moving while the market is closed": the live-P&L
endpoints re-marked every 60s from a Polygon snapshot and always preferred
`lastTrade.p`, which keeps ticking on thin after-hours / pre-market prints.
A stock closed at 16:00 ET would then show a different unrealized P&L at
18:30 ET purely from an after-hours trade.

Rule implemented here:
  * EQUITIES + equity OPTIONS are marked LIVE only during the regular cash
    session (9:30-16:00 ET on a trading day). Outside RTH the mark is FROZEN
    at the official session close — Polygon `day.c` once today has settled,
    else `prevDay.c`. `lastTrade` is never used outside RTH.
  * FUTURES (ES/NQ/RTY/YM + micros) trade ~23h/day on Globex, so they keep
    marking; callers just label the session (rth / globex / closed).

POLYGON-EXIT note: with REALTIME_FEED=fmp the callers now build the snapshot
dict from FMP instead of Polygon (fmp_feed.fmp_equity_snapshot_sync — live
quote-short during RTH, last SETTLED EOD close otherwise) and still route it
through pick_equity_mark(), so the freeze rule below is enforced unchanged
regardless of provider. Polygon remains the fallback shape.

Pure helpers, no I/O except reading the market calendar.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional, Tuple

from app.engines.market_calendar import market_status

FUTURES_ROOTS = ("MES", "MNQ", "M2K", "MYM", "ES", "NQ", "RTY", "YM")
# Futures month code (F G H J K M N Q U V X Z) + 1-2 year digits, e.g. Z5, H26.
_FUT_SUFFIX = re.compile(r"^[FGHJKMNQUVXZ]\d{1,2}$")


def is_futures_symbol(instrument: str) -> bool:
    """True for an index-futures root or micro — exactly the root (NQ, MNQ) or
    the root plus a futures month/year code (NQZ5, ESH26). Deliberately does
    NOT match equity tickers that merely start with a root (e.g. ESTC, NQXT)."""
    s = (instrument or "").upper().strip()
    if not s:
        return False
    for root in FUTURES_ROOTS:
        if s == root:
            return True
        if s.startswith(root) and _FUT_SUFFIX.match(s[len(root):]):
            return True
    return False


def equity_session(now: Optional[datetime] = None) -> str:
    """'regular' | 'premarket' | 'afterhours' | 'closed' for US equities."""
    try:
        return market_status(now).get("session", "closed")
    except Exception:
        # Fail OPEN to live marking rather than freezing P&L forever.
        return "regular"


def equity_market_live(now: Optional[datetime] = None) -> bool:
    return equity_session(now) == "regular"


def market_session_label(instrument: str, now: Optional[datetime] = None) -> str:
    """Human/operator label for the instrument's current session."""
    if is_futures_symbol(instrument):
        sess = equity_session(now)
        # Futures trade through equity pre/post/overnight; only the equity
        # 'regular' block overlaps the index cash session.
        return "rth" if sess == "regular" else "globex"
    return equity_session(now)


def pick_equity_mark(ticker_json: dict,
                     session: Optional[str] = None) -> Tuple[Optional[float], str]:
    """Return (price, source) for an equity from a Polygon stocks-snapshot
    'ticker' object, honoring the market session.

    regular  -> lastTrade.p (live), then min.c / day.c / prevDay.c.
    otherwise -> the official close first: day.c (today's settled close) then
                 prevDay.c, then min.c, then lastTrade.p. This FREEZES the
                 mark outside RTH so after-hours prints can't move open P&L.
    """
    t = ticker_json or {}
    sess = session or equity_session()

    def _g(fld: str, sub: str) -> Optional[float]:
        try:
            v = (t.get(fld) or {}).get(sub)
            return float(v) if v is not None and float(v) > 0 else None
        except Exception:
            return None

    if sess == "regular":
        order = (("lastTrade", "p", "last_trade"), ("min", "c", "minute"),
                 ("day", "c", "day_close"), ("prevDay", "c", "prev_close"))
    else:
        order = (("day", "c", "day_close"), ("prevDay", "c", "prev_close"),
                 ("min", "c", "minute"), ("lastTrade", "p", "last_trade"))
    for fld, sub, label in order:
        v = _g(fld, sub)
        if v is not None:
            return v, f"{label}/{sess}"
    return None, f"none/{sess}"
