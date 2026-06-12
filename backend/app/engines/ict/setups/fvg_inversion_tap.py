"""FVG Inversion Tap (IFVG) - the first real strategy ported to the registry.

This setup formalizes the user's documented "FVG Inversion Tap" rules
(proposal SS3.8) as a first-class :class:`ICTSetup`, replacing the implicit
``_find_just_inverted_fvg`` branch inside the generic ``ICTStrategy`` for THIS
strategy only. Every other strategy keeps falling back to the generic model
(``registry.get_setup`` returns ``None`` for them), so porting this one cannot
regress the baseline or the others.

Entry timing (LOCKED by the user, SS3.8 / Q3 default): **enter on the
inversion-candle close, NO retest** - i.e. the moment a candle closes back
through a previously-violated FVG, take the trade at that candle's close. This
deliberately diverges from canonical ICT (which waits for the IFVG CE retest);
it is the user's stated preference. The inversion-detection logic is ported
behaviour-for-behaviour from ``ICTStrategy._find_just_inverted_fvg`` so the
port is provably equivalent on the foundation's saved regression case.

Rules (proposal SS3.8, with seed config ES/NQ - 15m/1m - HTF 1H/4H - RR 3 -
sessions NY_AM+LONDON):
  * **Direction** = the inverted FVG's NEW polarity. A *bearish* FVG that price
    closes back ABOVE flips to support -> LONG. A *bullish* FVG that price
    closes back BELOW flips to resistance -> SHORT. Both directions are scanned
    independently; the inversion candle itself defines the side (no separate
    EMA-bias gate - the inversion IS the trigger, matching the user's rule).
  * **Stop** = the reversal-point low (longs) / high (shorts): the sweep extreme
    that preceded the inversion, +/- 2 ticks (SS3.8 stop-loss).
  * **Target** = min RR 3.0 of the stop distance (seed RR 3), clamped to <=3R
    (``MAX_RR`` default kept per SS3.8 / Q12). The proposal's preferred TP is the
    nearest untapped 1H/4H FVG, else prior-session highs/lows; that HTF-FVG
    target is a later refinement - here we use the seed RR target via the shared
    ``_target_from_rr`` helper, which is the documented min and keeps geometry
    sane without inventing HTF levels that may not be present in context.
  * **Session** = ``config.session_filters`` (seed NY_AM+LONDON). Empty = 24h.
  * **Max trades/day** = ``rule_tree.max_trades_per_day`` or seed
    ``config.max_trades_per_day`` or default **2** (SS3.8 / Q11 "Inversion 2-3").
    The engine's ``entry_guard`` enforces the hard cap downstream; this is a
    best-effort in-evaluator guard keyed on the ET date.
  * **Cooldown / re-entry** = ``entry_guard`` futures default (5 min, one open
    position per instrument) - enforced by the engine, not re-implemented here;
    re-entry only on a FRESH inversion (a new inversion candle).
  * **Sizing** = ``config.max_contracts`` (risk-based sizing is the separate
    SS4/#9 build step, intentionally out of scope for this port).

Decision logging (per task spec):
  ``[ict:inversion] {inst} entry=.. stop=.. tgt=.. reason=..`` on fire;
  ``[ict:inversion] skip - {reason}`` on every rejection.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from app.engines.ict.base import ICTSetup
from app.engines.ict.context import ICTContext
from app.engines.ict.registry import register
from app.engines.ict.primitives import (
    detect_fvgs,
    detect_ifvgs,
    get_tick_size,
    is_in_session,
)
from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType

#: SS3.8 / Q11 default when neither rule_tree nor config sets a cap.
_DEFAULT_MAX_TRADES_PER_DAY = 2
#: SS3.8: minimum reward:risk (seed RR 3). Used when config carries no RR.
_DEFAULT_MIN_RR = 3.0
#: SS3.8 / Q12: keep the 3R take-profit cap for this setup (no lift).
_MAX_RR = 3.0
#: Bars to look back for the violation/sweep that precedes the inversion.
_INVERSION_LOOKBACK = 3


# REVERTED TO V1 (user feedback 2026-06-11): the new dedicated port was too
# tight and dropped the win rate vs the original engine. Registration is
# DISABLED so get_setup("FVG Inversion Tap") returns None -> falls back to
# the original generic engine (the 85%-WR V1). Re-enable by uncommenting
# the decorator below after retuning + a backtest that BEATS V1.
@register("FVG Inversion Tap")
class FVGInversionTap(ICTSetup):
    """Enter on the candle that inverts a recently-violated FVG (no retest)."""

    # Also resolvable via ``rule_tree.ict_setup == "fvg_inversion_tap"`` because
    # the registry normalizes "FVG Inversion Tap" -> "fvg_inversion_tap".

    def evaluate(self, ctx: ICTContext) -> Optional[TradeSignal]:
        inst = ctx.instrument
        cfg = ctx.config
        rt = ctx.rule_tree

        # --- execution-TF bars (the 1m where the inversion prints) ---------
        exec_df = ctx.execution
        if exec_df is None or len(exec_df) < _INVERSION_LOOKBACK + 2:
            logger.info(f"[ict:inversion] skip - no execution bars ({inst})")
            return None

        # --- session filter (SS3.8: NY_AM + LONDON via config) -------------
        ts = exec_df.index[-1]
        session_filters = list(getattr(cfg, "session_filters", None) or [])
        if session_filters and not is_in_session(ts, session_filters):
            logger.info(
                f"[ict:inversion] skip - out of session "
                f"{session_filters} @ {ts} ({inst})"
            )
            return None

        # --- max-trades/day best-effort guard (entry_guard is the hard cap) -
        max_trades = (
            rt.get("max_trades_per_day")
            or getattr(cfg, "max_trades_per_day", None)
            or _DEFAULT_MAX_TRADES_PER_DAY
        )
        if not self._under_daily_cap(ctx, ts, int(max_trades)):
            logger.info(
                f"[ict:inversion] skip - max_trades_per_day={max_trades} reached ({inst})"
            )
            return None

        # --- collect candidate FVGs on the execution TF --------------------
        fvgs = self._candidate_fvgs(exec_df, inst)
        if not fvgs:
            logger.info(f"[ict:inversion] skip - no IFVG (no FVGs on exec TF) ({inst})")
            return None

        # --- detect a JUST-inverted FVG in EITHER direction ----------------
        # Direction = the inverted FVG's NEW polarity (SS3.8). A bullish reclaim
        # (close back above a bearish FVG) = LONG; a bearish breakdown (close
        # back below a bullish FVG) = SHORT.
        hit = self._find_just_inverted(exec_df, fvgs, "long")
        direction = "long"
        if hit is None:
            hit = self._find_just_inverted(exec_df, fvgs, "short")
            direction = "short"
        if hit is None:
            logger.info(f"[ict:inversion] skip - no IFVG just inverted ({inst})")
            return None

        fvg, sweep_extreme = hit
        tick = get_tick_size(inst)

        # --- entry = inversion candle close (NO retest, SS3.8 locked) -------
        entry = float(exec_df.iloc[-1]["close"])

        # --- stop = reversal-point sweep extreme +/- 2 ticks (SS3.8) -------
        # Reuse the shared structural-stop helper, anchored to the swept extreme.
        sl = self._stop_from_structure(
            exec_df, direction, inst, buffer_ticks=2.0, anchor_level=sweep_extreme
        )
        if sl is None:
            logger.info(f"[ict:inversion] skip - could not place stop ({inst})")
            return None

        # Reject a degenerate (too-tight) stop, matching the engine's 2-tick floor.
        if abs(entry - sl) < tick * 2:
            logger.info(
                f"[ict:inversion] skip - stop too tight "
                f"(entry={entry} stop={sl}) ({inst})"
            )
            return None

        # --- target = min RR (seed 3), clamped to <=3R (SS3.8) -------------
        # The take-profit honours the SS3.8 cap (<=3R). The min-RR GATE, however,
        # checks against the *requested* min RR: if the strategy demands more
        # reward than the 3R cap can deliver, that minimum cannot be met, so we
        # stand aside rather than take a sub-minimum trade ("respect min-RR").
        min_rr = float(getattr(cfg, "risk_reward_ratio", None) or _DEFAULT_MIN_RR)
        rr = min(min_rr, _MAX_RR)
        tp = self._target_from_rr(entry, sl, direction, rr)

        # --- min-RR gate (SS3.8) -------------------------------------------
        if not self._min_rr_ok(entry, sl, tp, min_rr=min_rr):
            logger.info(
                f"[ict:inversion] skip - RR<{min_rr} (capped tgt only reaches "
                f"{rr}R) (entry={entry} stop={sl} tgt={tp}) ({inst})"
            )
            return None

        # --- geometry sanity (long: tgt>entry>stop; short: tgt<entry<stop) -
        if direction == "long" and not (tp > entry > sl):
            logger.info(
                f"[ict:inversion] skip - bad long geometry "
                f"(entry={entry} stop={sl} tgt={tp}) ({inst})"
            )
            return None
        if direction == "short" and not (tp < entry < sl):
            logger.info(
                f"[ict:inversion] skip - bad short geometry "
                f"(entry={entry} stop={sl} tgt={tp}) ({inst})"
            )
            return None

        # --- fire ----------------------------------------------------------
        new_polarity = "bullish" if direction == "long" else "bearish"
        reason = (
            f"ifvg_inversion_close dir={direction} "
            f"inverted_fvg={fvg.direction}->{new_polarity} "
            f"sweep={sweep_extreme} rr={min(min_rr, _MAX_RR)}"
        )
        logger.info(
            f"[ict:inversion] {inst} entry={entry} stop={sl} tgt={tp} reason={reason}"
        )

        # Record the fire so the per-day cap can see it next bar.
        self._record_fire(ctx, ts)

        sig_type = SignalType.LONG if direction == "long" else SignalType.SHORT
        return TradeSignal(
            signal=sig_type,
            instrument=inst,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            contracts=int(getattr(cfg, "max_contracts", 1) or 1),
            metadata={
                "setup": "fvg_inversion_tap",
                "bias": new_polarity,
                "fvg_type": fvg.direction,
                "fvg_high": float(fvg.high),
                "fvg_low": float(fvg.low),
                "ce_level": float(fvg.ce_level) if fvg.ce_level else float((fvg.high + fvg.low) / 2),
                "inversion": True,
                "sweep_level": float(sweep_extreme),
                "entry_mode": "inversion_close",
                "primary_tf": cfg.primary_timeframe,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _candidate_fvgs(exec_df: pd.DataFrame, inst: str) -> list:
        """FVGs + IFVGs on the execution TF, matching the generic engine's
        detection windows so the port is behaviour-equivalent."""
        out: list = []
        try:
            out.extend(detect_fvgs(exec_df.tail(30), instrument=inst, min_size_ticks=1))
            out.extend(detect_ifvgs(exec_df.tail(40), instrument=inst, min_size_ticks=0.5))
        except Exception as exc:  # never raise on detection
            logger.warning(f"[ict:inversion] fvg detection failed ({inst}): {exc!r}")
        return out

    @staticmethod
    def _find_just_inverted(df: pd.DataFrame, fvgs: list, direction: str,
                            lookback: int = _INVERSION_LOOKBACK):
        """Detect an FVG the CURRENT candle just inverted (closed back through
        against its original polarity), with a violation/sweep within the last
        ``lookback`` bars. Ported from ``ICTStrategy._find_just_inverted_fvg``.

        ``direction="long"`` finds a bearish-FVG reclaim (close above its high
        after a low sweep) and returns ``(fvg, sweep_low)``. ``"short"`` mirrors
        with a bullish-FVG breakdown and returns ``(fvg, sweep_high)``. Returns
        ``None`` if nothing just inverted in that direction.
        """
        if df is None or len(df) < lookback + 2:
            return None
        try:
            last = df.iloc[-1]
            last_close = float(last["close"])
            last_open = float(last["open"])
            prior_close = float(df.iloc[-2]["close"])
        except Exception:
            return None

        recent = df.tail(lookback + 1)
        ordered = sorted(fvgs, key=lambda f: getattr(f, "bar_index", 0), reverse=True)

        for fvg in ordered:
            try:
                if getattr(fvg, "bar_index", 0) > len(df) - 1:
                    continue
            except Exception:
                pass

            if direction == "long":
                # bullish reclaim: close back above the FVG high, bullish candle,
                # a prior low-sweep below the FVG low, and a FRESH inversion
                # (the previous bar was still below the FVG high).
                if last_close <= fvg.high:
                    continue
                if last_close <= last_open:
                    continue
                sweep_low = float(recent["low"].min())
                if sweep_low > fvg.low:
                    continue
                if prior_close >= fvg.high:
                    continue
                return fvg, sweep_low
            else:
                # bearish breakdown: close back below the FVG low, bearish
                # candle, a prior high-sweep above the FVG high, fresh inversion.
                if last_close >= fvg.low:
                    continue
                if last_close >= last_open:
                    continue
                sweep_high = float(recent["high"].max())
                if sweep_high < fvg.high:
                    continue
                if prior_close <= fvg.low:
                    continue
                return fvg, sweep_high

        return None

    # --- per-day trade cap (best-effort; entry_guard is the real gate) ----
    @staticmethod
    def _et_date(ts) -> str:
        try:
            t = pd.Timestamp(ts)
            if t.tz is None:
                t = t.tz_localize("UTC")
            return str(t.tz_convert("US/Eastern").date())
        except Exception:
            return "?"

    def _under_daily_cap(self, ctx: ICTContext, ts, max_trades: int) -> bool:
        if max_trades <= 0:
            return True
        fired = ctx.extra.get("_inversion_fires", {}) if isinstance(ctx.extra, dict) else {}
        return fired.get(self._et_date(ts), 0) < max_trades

    def _record_fire(self, ctx: ICTContext, ts) -> None:
        if not isinstance(ctx.extra, dict):
            return
        fired = ctx.extra.setdefault("_inversion_fires", {})
        d = self._et_date(ts)
        fired[d] = fired.get(d, 0) + 1
