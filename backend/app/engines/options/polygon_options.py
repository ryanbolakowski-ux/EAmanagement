"""Polygon.io options adapter.

What this user's Polygon plan supports (verified by live probe):
  • /v3/reference/options/contracts          — chain enumeration (free)
  • /v2/aggs/ticker/{O:...}/range/...        — historical option bars (free)
  • /v2/aggs/ticker/{O:...}/prev             — previous-day close (free)

What it doesn't:
  • /v3/snapshot/options/{underlying}        — live greeks/IV/quotes (paid)
  • Live NBBO quotes                         — paid

The adapter falls back to BS-priced "synthetic quotes" when live snapshots are
unavailable: derive IV from the most recent traded bar, then mark-to-model
against current spot. For paper/backtest that's accurate within a few percent;
for live we'd need to either upgrade Polygon or route through the broker's
quote stream.
"""
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Literal
import httpx
from loguru import logger

from app.config import settings


OPTIONS_API = "https://api.polygon.io"
OptionRight = Literal["call", "put"]


@dataclass
class OptionContract:
    ticker: str                # e.g. "O:SPY260618C00500000"
    underlying: str            # e.g. "SPY"
    expiration: date           # 2026-06-18
    strike: float              # 500.0
    right: OptionRight         # "call" or "put"
    multiplier: int = 100      # shares per contract


@dataclass
class OptionBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float]


def _parse_occ_ticker(ticker: str) -> Optional[OptionContract]:
    """Decode an OCC-style ticker like `O:SPY260618C00500000`:
      - `O:` prefix
      - underlying (1-6 letters)
      - YYMMDD expiration
      - C/P side
      - 8-digit strike × 1000 (00500000 = 500.000)"""
    if not ticker.startswith("O:"):
        return None
    body = ticker[2:]
    if len(body) < 15:
        return None
    # Walk from the right: strike is fixed-width 8 digits, side is 1 char before
    strike_int = int(body[-8:])
    side_char  = body[-9]
    yymmdd     = body[-15:-9]
    underlying = body[:-15]
    try:
        yy = int(yymmdd[:2])
        mm = int(yymmdd[2:4])
        dd = int(yymmdd[4:6])
        # OCC: 2-digit year, century inferred (20xx for now)
        year = 2000 + yy
        exp = date(year, mm, dd)
    except Exception:
        return None
    return OptionContract(
        ticker=ticker,
        underlying=underlying,
        expiration=exp,
        strike=strike_int / 1000.0,
        right="call" if side_char.upper() == "C" else "put",
    )


class PolygonOptionsClient:
    """Thin async wrapper around Polygon's options endpoints. Designed to fail
    loudly on auth issues and degrade gracefully on rate-limits (Polygon
    free-tier is 5 calls/min, so any caller-side throttling lives upstream)."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self.api_key = api_key or settings.POLYGON_API_KEY
        self.timeout = timeout

    async def _get(self, path: str, params: dict = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{OPTIONS_API}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(url, params=params)
            if r.status_code == 403:
                # Polygon's "not entitled" — bubble up so caller can fall back
                logger.warning(f"[Polygon] 403 on {path}: tier doesn't include this endpoint")
                raise PermissionError(r.text)
            if r.status_code == 429:
                raise RuntimeError("Polygon rate-limit hit; back off and retry")
            r.raise_for_status()
            return r.json()

    async def list_contracts(self, underlying: str,
                              expiration_after: Optional[date] = None,
                              expiration_before: Optional[date] = None,
                              right: Optional[OptionRight] = None,
                              limit: int = 250) -> list[OptionContract]:
        """List option contracts for `underlying` filtered by expiration range
        and optional side. Walks the cursor-paginated response."""
        params = {
            "underlying_ticker": underlying.upper(),
            "limit": limit,
            "expired": "false",
        }
        if expiration_after:
            params["expiration_date.gte"] = expiration_after.isoformat()
        if expiration_before:
            params["expiration_date.lte"] = expiration_before.isoformat()
        if right:
            params["contract_type"] = right

        out: list[OptionContract] = []
        path = "/v3/reference/options/contracts"
        while True:
            resp = await self._get(path, params)
            for row in (resp.get("results") or []):
                c = OptionContract(
                    ticker=row["ticker"],
                    underlying=row.get("underlying_ticker", underlying.upper()),
                    expiration=date.fromisoformat(row["expiration_date"]),
                    strike=float(row["strike_price"]),
                    right=row["contract_type"],
                    multiplier=int(row.get("shares_per_contract", 100)),
                )
                out.append(c)
            # Pagination
            next_url = resp.get("next_url")
            if not next_url or len(out) >= 5000:  # hard cap to avoid runaway
                break
            # next_url already includes apiKey; strip our prefix and just request the rest
            path = next_url.replace(OPTIONS_API, "")
            params = {}  # next_url is fully-formed
        return out

    async def get_aggs(self, option_ticker: str,
                       start: date, end: date,
                       timespan: str = "day", multiplier: int = 1) -> list[OptionBar]:
        """Historical bars for one option contract. `timespan` is one of
        polygon's enum: minute / hour / day / week."""
        path = f"/v2/aggs/ticker/{option_ticker}/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}"
        resp = await self._get(path, {"adjusted": "true"})
        out = []
        for row in (resp.get("results") or []):
            out.append(OptionBar(
                timestamp=datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc),
                open=row["o"], high=row["h"], low=row["l"], close=row["c"],
                volume=int(row.get("v", 0)), vwap=row.get("vw"),
            ))
        return out

    async def get_prev_close(self, option_ticker: str) -> Optional[OptionBar]:
        path = f"/v2/aggs/ticker/{option_ticker}/prev"
        try:
            resp = await self._get(path, {"adjusted": "true"})
        except Exception as e:
            logger.warning(f"[Polygon] prev_close failed for {option_ticker}: {e}")
            return None
        rows = resp.get("results") or []
        if not rows:
            return None
        row = rows[0]
        return OptionBar(
            timestamp=datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc),
            open=row["o"], high=row["h"], low=row["l"], close=row["c"],
            volume=int(row.get("v", 0)), vwap=row.get("vw"),
        )

    async def get_snapshot(self, underlying: str) -> Optional[dict]:
        """Live snapshot with greeks/IV/quotes. Returns None on a 403 (caller
        should fall back to BS-priced synthetic quotes)."""
        try:
            return await self._get(f"/v3/snapshot/options/{underlying.upper()}")
        except PermissionError:
            return None
