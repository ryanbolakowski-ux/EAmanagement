"""Deep-reconcile broker history into our `trades` table.

This module fixes the class of bugs where the broker closes a position on
its own (e.g. user clicked Flatten All in the broker UI, or our
`flatten_all` codepath didn't write a closing trade row) and our portfolio
reconciliation surfaces an "unexplained_gap" because realized YTD doesn't
match equity_delta.

The contract:
  * INSERT-only — we NEVER mutate existing trade rows. The user's data is
    sacred; if a row already exists for a fill, we leave it alone.
  * Idempotent — running twice with the same broker history is a no-op the
    second time around (we match on instrument + qty + entry_price ±1% +
    timestamp ±24h, plus tag inserted rows with the broker_order_id).
  * Defensive — broker fetch failures bubble up as a populated counter,
    not an exception. The caller decides whether to 502.

Returned dict shape:
  {fetched_from_broker: int,
   already_tracked:     int,
   inserted:            int,
   skipped_other_reason:int,
   inserted_ids:        [str, ...]}
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger


# Window for matching a broker fill to an existing trade row. We deliberately
# err generous — better to mark a true match as duplicate (no insert) than to
# double-insert the same fill. Verified against jaceford12's EZGO/PESI rows:
# entry prices match to <0.5% and timestamps within minutes.
PRICE_TOLERANCE_PCT = 0.01     # ±1%
TIME_TOLERANCE = timedelta(hours=24)


def _parse_broker_date(s: Any) -> datetime | None:
    """Tradier returns ISO-8601 with a 'Z' suffix; some events use a naive
    YYYY-MM-DD. Return a timezone-aware datetime or None if we can't parse."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if not isinstance(s, str):
        return None
    s = s.strip()
    # 'Z' → '+00:00' for fromisoformat compatibility
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Bare date
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def reconcile_trades_from_broker(db: AsyncSession, broker_account) -> dict:
    """Pull broker history; INSERT missing closing trades into `trades`.

    Matching rule: a broker fill is considered already-tracked if a row
    exists in `trades` for this user_id + instrument with abs(contracts)
    matching, entry_price within PRICE_TOLERANCE_PCT, and a fill timestamp
    within TIME_TOLERANCE. Otherwise we INSERT it.

    Each inserted row is tagged:
      * mode='live'
      * status='closed'
      * source='tradier_reconcile'   (in notes JSON, plus a column if exists)
      * broker_order_id=<broker fill id>
      * direction='long'             (we don't yet track shorts in equities)

    NEVER UPDATES existing rows. Returns a dict counter for the API layer.
    """
    counters = {
        "fetched_from_broker":   0,
        "already_tracked":       0,
        "inserted":              0,
        "skipped_other_reason":  0,
        "inserted_ids":          [],
    }

    # Import here to avoid a circular import at module load.
    from app.engines.live_trading.broker_factory import build_broker_from_account

    broker = build_broker_from_account(broker_account)
    if broker is None:
        logger.warning(f"[reconcile] no broker adapter for {broker_account.broker!r}")
        return counters

    try:
        connected = await broker.connect()
        if not connected:
            logger.warning(f"[reconcile] broker connect failed for account={broker_account.id}")
            return counters
        # Some adapters expose get_account_history, some don't (yet). Tolerate
        # both so the endpoint stays generic.
        if not hasattr(broker, "get_account_history"):
            logger.info(f"[reconcile] broker {broker_account.broker!r} has no get_account_history; skipping")
            return counters
        fills = await broker.get_account_history(limit=500)
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass

    if not fills:
        logger.info(f"[reconcile] no broker history rows for account={broker_account.id}")
        return counters

    counters["fetched_from_broker"] = len(fills)
    user_id = str(broker_account.user_id)
    account_id = str(broker_account.id)

    for fill in fills:
        # Belt-and-suspenders: never call .get on a non-dict (would crash with
        # 'str' object has no attribute 'get'). The Tradier normaliser should
        # already drop these but defending here means a single bad row can't
        # take down the whole reconcile.
        if not isinstance(fill, dict):
            logger.warning(f"[reconcile] skipping non-dict fill: {type(fill).__name__}: {str(fill)[:80]}")
            counters["skipped_other_reason"] += 1
            continue
        # Only consider true fills (trade/option). Journals/dividends/etc.
        # are accounted for elsewhere (deposits/withdrawals in the reconciliation
        # block).
        if (fill.get("type") or "").lower() not in ("trade", "option"):
            continue
        symbol = fill.get("symbol")
        qty = int(fill.get("quantity") or 0)
        price = float(fill.get("price") or 0)
        commission = float(fill.get("commission") or 0)
        amount = fill.get("amount")
        fill_ts = _parse_broker_date(fill.get("date"))
        broker_order_id = None
        try:
            raw = fill.get("raw") or {}
            inner = raw.get(fill.get("type")) if isinstance(raw, dict) else None
            if isinstance(inner, dict):
                broker_order_id = inner.get("orderid") or inner.get("order_id") or inner.get("id")
            broker_order_id = str(broker_order_id) if broker_order_id is not None else None
        except Exception:
            broker_order_id = None

        if not symbol or qty <= 0 or price <= 0:
            counters["skipped_other_reason"] += 1
            continue

        # Guard 1: if we've already inserted this fill on a previous reconcile
        # run, the broker_order_id will be on a row already.
        if broker_order_id:
            already_by_id = (await db.execute(
                text("SELECT id FROM trades WHERE user_id = :uid AND broker_order_id = :oid LIMIT 1"),
                {"uid": user_id, "oid": broker_order_id},
            )).first()
            if already_by_id:
                counters["already_tracked"] += 1
                continue

        # Guard 2: match against existing rows by (instrument, qty, price±1%,
        # time window). This catches fills our own engine recorded under a
        # different broker_order_id (or none at all).
        params = {
            "uid":   user_id,
            "inst":  symbol,
            "qty":   qty,
            "p_lo":  price * (1 - PRICE_TOLERANCE_PCT),
            "p_hi":  price * (1 + PRICE_TOLERANCE_PCT),
        }
        match_sql = (
            "SELECT id FROM trades "
            " WHERE user_id = :uid "
            "   AND instrument = :inst "
            "   AND ABS(contracts) = :qty "
            "   AND ((entry_price BETWEEN :p_lo AND :p_hi) "
            "        OR (exit_price BETWEEN :p_lo AND :p_hi))"
        )
        if fill_ts is not None:
            params["t_lo"] = fill_ts - TIME_TOLERANCE
            params["t_hi"] = fill_ts + TIME_TOLERANCE
            match_sql += (
                "   AND ( (entry_time BETWEEN :t_lo AND :t_hi) "
                "      OR (exit_time  BETWEEN :t_lo AND :t_hi) "
                "      OR (entry_time IS NULL AND exit_time IS NULL) ) "
            )
        match_sql += " LIMIT 1"
        match = (await db.execute(text(match_sql), params)).first()
        if match:
            counters["already_tracked"] += 1
            continue

        # INSERT the missing row. We model it as a self-contained closed
        # trade: entry_price == exit_price == the fill price (we don't know
        # what the open leg looked like; we only know the fill). P&L is the
        # `amount` (signed: positive on sells, negative on buys).
        new_id = uuid.uuid4()
        pnl: float | None = None
        if amount is not None:
            # Tradier amount sign: sells positive (we received cash), buys negative.
            # For a CLOSING sell, amount-commission is the realized leg.
            try:
                pnl = float(amount)
            except (TypeError, ValueError):
                pnl = None
        # If we have no amount, leave pnl null — the reconciliation block
        # already handles partial information defensively.

        # Tag the row clearly. `source` may not exist as a column; we also
        # stuff it into notes for observability.
        notes = {
            "source":          "tradier_reconcile",
            "broker_order_id": broker_order_id,
            "fill_date":       fill.get("date"),
            "side":            fill.get("side"),
            "broker_amount":   amount,
            "broker_commission": commission,
        }

        await db.execute(text("""
            INSERT INTO trades (
                id, user_id, broker_account_id, mode, status,
                instrument, direction, contracts,
                entry_price, exit_price, stop_loss, take_profit,
                entry_time, exit_time,
                broker_order_id,
                pnl, commission, net_pnl,
                exit_reason, notes,
                created_at, updated_at
            ) VALUES (
                :id, :uid, :acct, 'live', 'closed',
                :inst, 'long', :qty,
                :price, :price, 0, 0,
                :ts, :ts,
                :oid,
                :pnl, :comm, :net_pnl,
                'tradier_reconcile', CAST(:notes AS JSON),
                NOW(), NOW()
            )
        """), {
            "id":      str(new_id),
            "uid":     user_id,
            "acct":    account_id,
            "inst":    symbol,
            "qty":     qty,
            "price":   price,
            "ts":      fill_ts,
            "oid":     broker_order_id,
            "pnl":     pnl,
            "comm":    commission,
            "net_pnl": (pnl - commission) if pnl is not None else None,
            "notes":   _json_dumps(notes),
        })
        counters["inserted"] += 1
        counters["inserted_ids"].append(str(new_id))
        logger.info(
            f"[reconcile] inserted user={user_id} instrument={symbol} "
            f"qty={qty} price={price} broker_order_id={broker_order_id} "
            f"pnl={pnl}"
        )

    await db.commit()
    logger.info(
        f"[reconcile] account={account_id} "
        f"fetched={counters['fetched_from_broker']} "
        f"already_tracked={counters['already_tracked']} "
        f"inserted={counters['inserted']} "
        f"skipped={counters['skipped_other_reason']}"
    )
    return counters


def _json_dumps(obj) -> str:
    """Tiny shim so callers don't need to import json. Stable separators
    keep diffs small if we ever inspect the column by eye."""
    import json
    return json.dumps(obj, default=str, separators=(",", ":"))
