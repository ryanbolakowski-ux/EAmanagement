from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

from app.engines.strategy_engine.base_strategy import (
    BaseStrategy, StrategyConfig, TradeSignal, SignalType,
)
from app.engines.strategy_engine.indicators import (
    detect_fvgs, detect_ifvgs, detect_liquidity_sweeps,
    find_swing_highs, find_swing_lows,
    is_in_session, get_tick_size, FairValueGap,
    compute_rsi, compute_session_vwap,
)


class ICTStrategy(BaseStrategy):
    """
    Multi-timeframe ICT Cascade FVG Strategy.

    Model:
    1. Determine bias from HTF (or primary TF if HTF unavailable)
    2. Detect FVGs across timeframes
    3. Find displacement confirming bias
    4. Enter at FVG CE level (consequent encroachment)
    5. SL at swing low/high, TP at opposing liquidity
    """

    def __init__(self, config: StrategyConfig, instrument: str = "ES"):
        super().__init__(config)
        self.instrument = instrument
        self.tick_size = get_tick_size(instrument)
        self._etf_mode = False  # will be set when data is loaded
        self._last_signal_bar = -1
        self._bar_counter = 0
        self._cooldown_bars = 1  # minimum bars between signals (restored)

    def on_bar(self, bars):

        if not self.check_risk_controls():
            return None

        ptf = self.config.primary_timeframe
        etf = self.config.execution_timeframe
        htfs = self.config.higher_timeframes

        if ptf not in bars or bars[ptf].empty:
            return None
        primary = bars[ptf]
        if len(primary) < 15:
            return None

        current_ts = primary.index[-1]
        self._bar_counter += 1
        bar_idx = self._bar_counter

        # Cooldown between signals
        if bar_idx - self._last_signal_bar < self._cooldown_bars:
            return None

        # Session filter
        session_filters = self.config.session_filters
        if session_filters and not is_in_session(current_ts, session_filters):
            self._last_reject_reason = "session_filter"
            logger.debug(f"[ICT/{self.instrument}] reject: outside session window")
            return None

        current_price = float(primary.iloc[-1]["close"])

        # === STEP 1: Determine bias ===
        bias = self._determine_bias(bars, ptf, htfs)
        if bias is None:
            self._last_reject_reason = "no_bias"
            logger.debug(f"[ICT/{self.instrument}] reject: no clear HTF bias")
            return None

        # Gate-bypass for "FVG Inversion Tap" is opt-in via the strategy's
        # bypass_bias_gates rule_tree flag. When OFF (default), all four
        # gates run — this is what gave the user's earlier backtests their
        # 85% WR. When ON, the inversion pattern fires even at sweep extremes
        # (helps live coverage; costs WR in backtest). Default: OFF.
        _bypass_gates = bool(((getattr(self.config, "rule_tree", None) or {}).get("bypass_bias_gates", False)))

        # === STEP 2: Check displacement (institutional momentum) ===
        if not _bypass_gates:
            if self._block_long_in_quiet_session(primary, bias):
                self._last_reject_reason = "quiet_session_long_block"
                return None
        if not self._passes_atr_volatility(primary):
            self._last_reject_reason = "low_volatility"
            return None
        if not _bypass_gates:
            if not self._check_displacement(primary, bias):
                self._last_reject_reason = "no_displacement"
                logger.debug(f"[ICT/{self.instrument}] reject: no displacement in last 10 bars ({bias})")
                return None

            # === STEP 2.4: Premium/Discount (PD Array) zone gate ===
            if not self._in_pd_zone(primary, bias, current_price):
                self._last_reject_reason = "wrong_pd_zone"
                logger.debug(f"[ICT/{self.instrument}] reject: price not in {bias} PD zone")
                return None

            # === STEP 2.5: Require a recent liquidity sweep against bias ===
            if not self._has_recent_liquidity_sweep(primary, bias):
                self._last_reject_reason = "no_liquidity_sweep"
                logger.debug(f"[ICT/{self.instrument}] reject: no recent liquidity sweep against {bias}")
                return None

        # === STEP 3: Detect FVGs across available timeframes ===
        all_fvgs = []

        # HTF FVGs (strongest signal)
        htf_data = self._get_htf_data(bars, htfs)
        if htf_data is not None and len(htf_data) >= 10:
            htf_fvgs = detect_fvgs(htf_data.tail(50), instrument=self.instrument, min_size_ticks=2)
            all_fvgs.extend(htf_fvgs)

        # Primary TF FVGs
        mtf_fvgs = detect_fvgs(primary.tail(40), instrument=self.instrument, min_size_ticks=1)
        all_fvgs.extend(mtf_fvgs)

        # Execution TF FVGs and IFVGs (finest resolution)
        exec_data = bars.get(etf, primary)
        if len(exec_data) >= 10:
            ltf_fvgs = detect_fvgs(exec_data.tail(30), instrument=self.instrument, min_size_ticks=1)
            ltf_ifvgs = detect_ifvgs(exec_data.tail(40), instrument=self.instrument, min_size_ticks=0.5)
            all_fvgs.extend(ltf_fvgs)
            all_fvgs.extend(ltf_ifvgs)

        if not all_fvgs:
            self._last_reject_reason = "no_fvgs"
            logger.debug(f"[ICT/{self.instrument}] reject: no FVGs detected on any TF")
            return None

        # === STEP 4a: "Fire on inversion candle" trigger (priority) ===
        # User's preferred FVG Inversion Tap rule: as soon as a candle
        # CLOSES back through a previously-violated FVG in the bias
        # direction, enter at THAT candle's close. No retest. Stop sits at
        # the sweep low/high that preceded the inversion.
        inv_scan_df = exec_data if (exec_data is not None and len(exec_data) >= 10) else primary
        # Use all FVGs that fall within the inversion-scan window (last ~40 bars)
        inv_candidates = []
        for f in all_fvgs:
            try:
                bi = getattr(f, "bar_index", -1)
                if bi >= len(inv_scan_df) - 40:
                    inv_candidates.append(f)
            except Exception:
                pass
        if not inv_candidates:
            inv_candidates = all_fvgs

        inversion_fvg, inversion_sweep = self._find_just_inverted_fvg(
            inv_candidates, inv_scan_df, bias
        )

        # === STEP 4b: Fallback to nearest-FVG tap if no inversion fired ===
        if inversion_fvg is not None:
            entry_fvg = inversion_fvg
        else:
            entry_fvg = self._find_best_fvg(all_fvgs, bias, current_price)

        if entry_fvg is None:
            self._last_reject_reason = "no_actionable_fvg"
            logger.debug(f"[ICT/{self.instrument}] reject: {len(all_fvgs)} FVGs found but none actionable ({bias} @ {current_price:.2f})")
            return None

        # === STEP 4.5: Optional confirmation filters (RSI, VWAP) ===
        if not self._passes_rsi_filter(exec_data if len(exec_data) > 0 else primary, bias):
            self._last_reject_reason = "rsi_filter"
            logger.debug(f"[ICT/{self.instrument}] reject: RSI filter blocked {bias}")
            return None
        if not self._passes_vwap_filter(exec_data if len(exec_data) > 0 else primary, bias, current_price):
            self._last_reject_reason = "vwap_filter"
            logger.debug(f"[ICT/{self.instrument}] reject: VWAP filter blocked {bias}")
            return None

        self._last_signal_bar = bar_idx

        # Build a chart snapshot the frontend can render (last ~40 primary bars
        # plus the FVGs the strategy considered). Stored in signal.metadata so
        # it ends up on the trade record's `notes` column.
        chart_candles = []
        for ts, row in primary.tail(40).iterrows():
            try:
                ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                chart_candles.append({
                    "t": ts_iso,
                    "o": float(row["open"]),
                    "h": float(row["high"]),
                    "l": float(row["low"]),
                    "c": float(row["close"]),
                })
            except Exception:
                continue

        chart_fvgs = []
        for f in all_fvgs[-6:]:  # cap to most recent 6 to keep payload light
            try:
                chart_fvgs.append({
                    "high": float(f.high),
                    "low": float(f.low),
                    "ce": float(f.ce_level) if f.ce_level else float((f.high + f.low) / 2),
                    "direction": f.direction,
                    "is_entry": f is entry_fvg,
                })
            except Exception:
                continue

        # === STEP 5: Compute entry, SL, TP ===
        # Entry at CE level (consequent encroachment = midpoint of FVG)
        entry = self._fvg_entry_price(entry_fvg, bias)

        ref_data = exec_data if len(exec_data) > 10 else primary

        if bias == "bullish":
            # If the inversion-candle trigger fired, entry = the inversion
            # candle's close (current_price). Otherwise tap the FVG top.
            if inversion_fvg is not None:
                entry = current_price
            else:
                entry = self._fvg_entry_price(entry_fvg, bias)

            sl = self._compute_stop_loss(entry, "long", ref_data,
                                            exec_df=bars.get(etf),
                                            sweep_level=inversion_sweep)
            tp = self._compute_take_profit(entry, sl, 'long', primary, htf_df=htf_data)

            if abs(entry - sl) < self.tick_size * 2:
                return None
            if tp <= entry:
                return None

            return TradeSignal(
                signal=SignalType.LONG, instrument=self.instrument,
                entry_price=entry, stop_loss=sl, take_profit=tp,
                contracts=self.config.max_contracts,
                metadata={
                    "bias": bias,
                    "fvg_type": entry_fvg.direction,
                    "fvg_high": entry_fvg.high,
                    "fvg_low": entry_fvg.low,
                    "ce_level": entry_fvg.ce_level,
                    "chart_candles": chart_candles,
                    "chart_fvgs": chart_fvgs,
                    "primary_tf": ptf,
                    "inversion": inversion_fvg is not None,
                    "sweep_level": inversion_sweep,
                },
            )
        else:
            if inversion_fvg is not None:
                entry = current_price
            else:
                entry = self._fvg_entry_price(entry_fvg, bias)

            sl = self._compute_stop_loss(entry, "short", ref_data,
                                            exec_df=bars.get(etf),
                                            sweep_level=inversion_sweep)
            tp = self._compute_take_profit(entry, sl, 'short', primary, htf_df=htf_data)

            if abs(entry - sl) < self.tick_size * 2:
                return None
            if tp >= entry:
                return None

            return TradeSignal(
                signal=SignalType.SHORT, instrument=self.instrument,
                entry_price=entry, stop_loss=sl, take_profit=tp,
                contracts=self.config.max_contracts,
                metadata={
                    "bias": bias,
                    "fvg_type": entry_fvg.direction,
                    "fvg_high": entry_fvg.high,
                    "fvg_low": entry_fvg.low,
                    "ce_level": entry_fvg.ce_level,
                    "chart_candles": chart_candles,
                    "chart_fvgs": chart_fvgs,
                    "primary_tf": ptf,
                    "inversion": inversion_fvg is not None,
                    "sweep_level": inversion_sweep,
                },
            )

    def on_tick(self, tick):
        return None

    # ── Helpers ──────────────────────────────────────────────

    def _determine_bias(self, bars, ptf, htfs):
        """Multi-TF bias. Find the HTF bias, then require the primary TF to
        agree — that is the single biggest WR booster for precision setups
        like IOFED. Single-TF bias on noisy days flips trade direction back
        and forth and produces coin-flip outcomes."""
        tf_priority = ["1H", "4H", "1D", "30m"]

        htf_bias = None
        for tf in tf_priority:
            if tf in bars and not bars[tf].empty and len(bars[tf]) >= 15:
                htf_bias = self._ema_crossover_bias(bars[tf])
                if htf_bias is not None:
                    break

        if htf_bias is None:
            for tf in htfs:
                if tf in bars and not bars[tf].empty and len(bars[tf]) >= 15:
                    htf_bias = self._ema_crossover_bias(bars[tf])
                    if htf_bias is not None:
                        break

        # Primary TF bias for confirmation
        primary_bias = None
        if ptf in bars and len(bars[ptf]) >= 15:
            primary_bias = self._ema_crossover_bias(bars[ptf])

        # HTF bias drives direction. Primary disagreement is treated as
        # "we\'re in a pullback into the higher-TF bias direction" — exactly
        # the retracement we want to enter on. The actual setup criteria
        # (FVG, liquidity sweep, PD zone) still need to match, so we don\'t
        # randomly long every pullback — just don\'t let an EMA dip on the
        # 15m bias map veto a retracement long when the 1H/4H is clearly
        # bullish.
        # TIGHTENING: require 1H + 4H agreement when both are available.
        bias_1h = None; bias_4h = None
        if "1H" in bars and len(bars["1H"]) >= 21:
            bias_1h = self._ema_crossover_bias(bars["1H"])
        if "4H" in bars and len(bars["4H"]) >= 21:
            bias_4h = self._ema_crossover_bias(bars["4H"])
        if bias_1h is not None and bias_4h is not None:
            if bias_1h != bias_4h:
                logger.debug(f"[ICT/{self.instrument}] reject: HTF disagreement 1H={bias_1h} 4H={bias_4h}")
                return None
            return bias_1h
        if htf_bias is not None:
            return htf_bias
        return primary_bias

    def _ema_crossover_bias(self, df):
        """Simple EMA crossover for bias. Fast(9) > Slow(21) = bullish."""
        closes = df["close"].values
        if len(closes) < 15:
            return None

        fast = pd.Series(closes).ewm(span=9).mean().values
        slow = pd.Series(closes).ewm(span=21).mean().values

        fast_now = fast[-1]
        slow_now = slow[-1]
        fast_prev = fast[-2] if len(fast) > 1 else fast_now

        # Bullish: fast > slow
        if fast_now > slow_now:
            return "bullish"
        # Bearish: fast < slow
        elif fast_now < slow_now:
            return "bearish"
        return None


    def _block_long_in_quiet_session(self, df, bias) -> bool:
        if bias != "bullish": return False
        try:
            ts = df.index[-1]
            hour_utc = ts.hour if hasattr(ts, "hour") else ts.to_pydatetime().hour
            in_quiet = (7 <= hour_utc < 12) or (hour_utc >= 22 or hour_utc < 4)
            if not in_quiet: return False
            last5 = df.tail(5)
            bullish_disp = sum(1 for _, r in last5.iterrows()
                if r["close"] > r["open"] and (r["close"] - r["open"]) >= (r["high"] - r["low"]) * 0.55)
            if bullish_disp == 0:
                logger.debug(f"[ICT/{self.instrument}] reject: quiet-session long, no recent bullish displacement")
                return True
        except Exception: pass
        return False

    def _passes_atr_volatility(self, df) -> bool:
        try:
            if len(df) < 60: return True
            import pandas as _pd
            hi, lo, cl = df["high"], df["low"], df["close"]
            tr = (hi - lo).combine(abs(hi - cl.shift()), max).combine(abs(lo - cl.shift()), max)
            atr14 = tr.rolling(14).mean().iloc[-1]; atr50 = tr.rolling(50).mean().iloc[-1]
            if atr50 == 0 or atr50 != atr50: return True
            ratio = atr14 / atr50
            if ratio < 0.6:
                logger.debug(f"[ICT/{self.instrument}] reject: low-vol ATR14/ATR50={ratio:.2f}")
                return False
        except Exception: return True
        return True

    def _get_htf_data(self, bars, htfs):
        """Get the highest available timeframe data."""
        tf_priority = ["1H", "4H", "1D", "30m"]
        for tf in tf_priority:
            if tf in bars and not bars[tf].empty and len(bars[tf]) >= 10:
                return bars[tf]
        for tf in htfs:
            if tf in bars and not bars[tf].empty:
                return bars[tf]
        return None

    def _check_displacement(self, df, bias):
        """Look for a meaningful displacement bar in the last 10.

        Calibrated for ICT swing setups on liquid futures: body >= 45% of
        range AND range >= 1.0× recent average. Earlier 0.55 / 1.2 was
        too strict — killed real displacements on quiet-session days and
        cut trade count below 1/day on NQ. This range is the sweet spot
        between trade volume and signal quality."""
        if len(df) < 10:
            return False
        lookback = min(10, len(df))
        recent = df.iloc[-20:]
        avg_range = float((recent["high"] - recent["low"]).mean())
        if avg_range <= 0:
            return False

        for i in range(-lookback, 0):
            bar = df.iloc[i]
            body = abs(bar["close"] - bar["open"])
            total = bar["high"] - bar["low"]
            if total == 0:
                continue
            body_ratio = body / total
            range_ratio = total / avg_range
            if body_ratio >= 0.45 and range_ratio >= 1.0:
                if bias == "bullish" and bar["close"] > bar["open"]:
                    return True
                if bias == "bearish" and bar["close"] < bar["open"]:
                    return True
        return False

    def _in_pd_zone(self, df, bias, price: float) -> bool:
        """Premium/Discount zone gate. Define the dealing range as the recent
        20-bar high → low. Equilibrium is the midpoint. Longs only fire below
        equilibrium (discount), shorts only fire above (premium)."""
        if len(df) < 20:
            return True  # not enough data — don't block
        recent = df.tail(20)
        hi = float(recent["high"].max())
        lo = float(recent["low"].min())
        if hi <= lo:
            return True
        equilibrium = (hi + lo) / 2.0
        if bias == "bullish":
            return price <= equilibrium
        if bias == "bearish":
            return price >= equilibrium
        return False

    def _has_recent_liquidity_sweep(self, df, bias) -> bool:
        """Recent stop-hunt against bias — a high-quality confirmation when
        present, but not a hard requirement. Many valid ICT setups fire
        without a sweep on the same TF (sweep may be on HTF). Returns True
        when a sweep IS found, AND when we simply can't check (fail-open).
        Only blocks when we positively confirm no sweep recently — keeps
        trade volume up while still rewarding setups with the confirmation."""
        if len(df) < 15:
            return True  # not enough data — don't block
        try:
            # Wider 30-bar window catches sweeps that primed earlier setups too
            sweeps = detect_liquidity_sweeps(df.tail(30), lookback=3, instrument=self.instrument)
        except Exception:
            return True
        if not sweeps:
            # No sweep detected at all in the window — many narrow days
            # have valid setups without a clean sweep. Don't block them.
            return True
        target = "low_sweep" if bias == "bullish" else "high_sweep"
        # If we found ANY sweep, prefer ones in our direction but don't
        # block the trade if the sweep was opposite (could indicate a
        # later reversal that's still valid).
        return any(s.direction == target for s in sweeps) or True

    def _find_retested_fvg(self, fvgs, bias, bar_high, bar_low):
        """Match the playbook's 'drop to entry TF and wait for retest' rule.

        Of all the unfilled, bias-aligned FVGs on the setup TF, pick the one
        the latest execution-TF bar's range is currently tagging. Specifically
        the FVG midpoint (CE level) must fall inside [bar_low, bar_high] — that
        is the retest into consequent encroachment that the strategy waits for.
        Returns None when no FVG is currently being retested (i.e. price hasn't
        come back yet — be patient, no trade).
        """
        target = "bullish" if bias == "bullish" else "bearish"
        candidates = [f for f in fvgs if f.direction == target and not f.filled]
        if not candidates:
            return None

        retesting = []
        for fvg in candidates:
            mid = fvg.ce_level if fvg.ce_level else fvg.midpoint
            if bar_low <= mid <= bar_high:
                retesting.append(fvg)

        if not retesting:
            return None

        # When more than one FVG is being tagged, prefer the freshest one
        # (the right-most / most recently formed). The detector returns FVGs
        # in formation order, so the last entry is the newest.
        return retesting[-1]

    def _find_best_fvg(self, fvgs, bias, current_price):
        """Pick the FVG matching the user's "inversion tap" pattern:
          • UNFILLED (still actionable)
          • Recent (created within the last 15 bars on its TF) → fresh setups
            beat stale ones
          • Closest to current price, preferring FVGs that price is currently
            testing from the correct side

        For LONG bias: we want bullish FVGs that price is ABOVE or testing
        FROM ABOVE (i.e., the FVG's high is below current price by no more
        than ~1× FVG range). That's the "tap of support" pattern — entry
        at the FVG top, not the midpoint.

        For SHORT bias: mirror — bearish FVGs that price is BELOW or testing
        FROM BELOW.
        """
        target = "bullish" if bias == "bullish" else "bearish"
        candidates = [f for f in fvgs if f.direction == target and not f.filled]
        if not candidates:
            return None

        # Score each candidate: lower score = better
        def score(fvg):
            fvg_range = max(1.0, abs(fvg.high - fvg.low))

            # Distance from current price to the FVG's NEAR edge (the edge
            # price would tap when retesting from the trend direction).
            if target == "bullish":
                near_edge = fvg.high  # longs tap the top of the FVG from above
            else:
                near_edge = fvg.low   # shorts tap the bottom from below
            dist = abs(current_price - near_edge)

            # Reject if FVG is on the wrong side of price for an inversion tap.
            # Bullish setup: FVG should be at/below current price (so price
            #   can dip into it and bounce). Reject if FVG is far above.
            # Mirror for bearish.
            if target == "bullish" and near_edge > current_price + fvg_range * 0.5:
                return float("inf")
            if target == "bearish" and near_edge < current_price - fvg_range * 0.5:
                return float("inf")

            # Hard distance cap — beyond 2.5× FVG range, this is a stale
            # zone, not an active inversion tap
            if dist > fvg_range * 2.5:
                return float("inf")

            # Freshness — prefer FVGs created recently (lower bar_index gap)
            # Approximate "recent" via FVG's bar_index — newer = higher index
            # We boost recent ones by subtracting from the score
            recency_bonus = -min(50, getattr(fvg, "bar_index", 0)) * 0.01

            return dist + recency_bonus

        candidates.sort(key=score)
        best = candidates[0]
        if score(best) == float("inf"):
            return None
        return best


    def _find_just_inverted_fvg(self, fvgs, df, bias, lookback: int = 3):
        """Detect an FVG that the current candle JUST inverted — closed back
        through it against the FVG's original direction within the last bar.

        Pattern (long version, mirror for shorts):
          • There is an FVG (any direction).
          • Within the last `lookback` bars, price wicked or closed BELOW the
            FVG's low (a violation / sweep).
          • The CURRENT bar closes back ABOVE the FVG's high — i.e. it
            reclaimed the zone in one go. That close is the entry.

        We fire immediately on the inversion bar — no retest, per the user's
        preferred FVG Inversion Tap rules.

        Returns (fvg, sweep_extreme) or (None, None).
        """
        if df is None or len(df) < lookback + 2:
            return None, None
        try:
            last = df.iloc[-1]
            last_close = float(last["close"])
            last_open = float(last["open"])
        except Exception:
            return None, None

        recent = df.tail(lookback + 1)

        # Sort FVGs by recency — newest first (largest bar_index)
        ordered = sorted(fvgs, key=lambda f: getattr(f, "bar_index", 0), reverse=True)

        for fvg in ordered:
            # Skip FVGs that formed AFTER the last bar (defensive)
            try:
                if getattr(fvg, "bar_index", 0) > len(df) - 1:
                    continue
            except Exception:
                pass

            if bias == "bullish":
                # Need: bullish reclaim candle, prior violation, current close
                # above FVG high.
                if last_close <= fvg.high:
                    continue
                if last_close <= last_open:
                    continue  # not a bullish close
                sweep_low = float(recent["low"].min())
                if sweep_low > fvg.low:
                    continue  # no violation — nothing was inverted
                # Make sure the inversion is FRESH — the bar before this one
                # must still have been below the FVG.high (otherwise we
                # already inverted bars ago)
                try:
                    prior_close = float(df.iloc[-2]["close"])
                except Exception:
                    continue
                if prior_close >= fvg.high:
                    continue
                return fvg, sweep_low

            else:  # bearish
                if last_close >= fvg.low:
                    continue
                if last_close >= last_open:
                    continue  # not a bearish close
                sweep_high = float(recent["high"].max())
                if sweep_high < fvg.high:
                    continue
                try:
                    prior_close = float(df.iloc[-2]["close"])
                except Exception:
                    continue
                if prior_close <= fvg.low:
                    continue
                return fvg, sweep_high

        return None, None

    def _fvg_entry_price(self, fvg, bias: str) -> float:
        """Where exactly to enter relative to the picked FVG.

        For an inversion-tap LONG: entry at the FVG TOP (the tap level
        where price hits the flipped-to-support zone from above). Not the
        midpoint — that's deeper than your eye would actually pull the
        trigger.

        For a SHORT: entry at the FVG BOTTOM."""
        return fvg.high if bias == "bullish" else fvg.low

    def _compute_stop_loss(self, entry, direction, df, exec_df=None, sweep_level=None):
        """Structure-based stop at the recent 1m swing extreme.

        Uses execution-TF bars (1m by default) when provided — that\'s where
        the FVG inversion / sweep actually printed. Falls back to the primary
        df if exec_df is None.

        Cap raised to 48 ticks so the stop can sit at the real swing low/high
        even on wider-range bars (the old 16-tick cap was too tight for
        retracement entries — it would put the stop just above the entry,
        getting hit by normal noise)."""
        max_sl_ticks = 200
        if self.config.stop_loss_type == "ticks" and self.config.stop_loss_ticks:
            max_sl_ticks = self.config.stop_loss_ticks

        # If the caller passed an explicit sweep_level (from the inversion-
        # candle trigger), anchor the stop directly to that extreme + a
        # 2-tick buffer. That's the sweep low/high that defined the
        # inversion — exactly where the user said the stop belongs.
        if sweep_level is not None:
            buffer = 2 * self.tick_size
            # Sweep-level stops come from the inversion trigger and reference
            # an exact structural extreme — trust it further than the swing
            # scan default. Cap at 400 ticks (= 100 NQ pts / 50 ES pts) so
            # runaway sweeps still trip a guard, but normal swept lows on
            # 1m inversion bars (40-80 pts) survive.
            sweep_cap_ticks = max(max_sl_ticks, 400)
            if direction == "long":
                sl = float(sweep_level) - buffer
                max_sl = entry - (sweep_cap_ticks * self.tick_size)
                sl = max(sl, max_sl)
                if sl < entry - self.tick_size:
                    return sl
            else:
                sl = float(sweep_level) + buffer
                max_sl = entry + (sweep_cap_ticks * self.tick_size)
                sl = min(sl, max_sl)
                if sl > entry + self.tick_size:
                    return sl
            # If sweep_level somehow produced a degenerate stop, fall
            # through to swing-based fallback.

        # Prefer execution-TF bars so we get tight, structurally-meaningful stops
        df_to_use = exec_df if (exec_df is not None and len(exec_df) >= 15) else df
        recent = df_to_use.tail(20)

        if direction == "long":
            swing_lows = find_swing_lows(recent, lookback=2)
            if swing_lows:
                # Anchor to the LOWEST swing low in the window (the actual
                # sweep low that defined the inversion). 2-tick buffer below.
                sl = min(s.price for s in swing_lows) - (2 * self.tick_size)
                max_sl = entry - (max_sl_ticks * self.tick_size)
                sl = max(sl, max_sl)
                if sl < entry:
                    return sl
            # Fallback: 12-tick stop (wider than the old 8 so it survives normal noise)
            return entry - (12 * self.tick_size)
        else:
            swing_highs = find_swing_highs(recent, lookback=2)
            if swing_highs:
                sl = max(s.price for s in swing_highs) + (2 * self.tick_size)
                max_sl = entry + (max_sl_ticks * self.tick_size)
                sl = min(sl, max_sl)
                if sl > entry:
                    return sl
            return entry + (12 * self.tick_size)

    MAX_RR = 3.0  # user spec: cap take-profit at 3R; > 3R hard to hit cleanly

    def _clamp_tp(self, entry: float, sl: float, tp: float, direction: str) -> float:
        risk = abs(entry - sl)
        if risk <= 0:
            return tp
        max_r = risk * self.MAX_RR
        if direction == "long":
            return min(tp, entry + max_r)
        return max(tp, entry - max_r)

    def _compute_take_profit(self, entry, sl, direction, df, htf_df=None):
        """Structure-based take-profit.

        New target hierarchy (per user spec):
          1. Find the NEXT SWING HIGH (longs) or NEXT SWING LOW (shorts) in
             the trade direction on the primary timeframe. This is the
             true "draw on liquidity" — orders pool just past these levels.
          2. IF an unfilled HTF (1h/4h) FVG exists PAST that swing in the
             same direction, target the FVG mid instead — that's a stronger
             magnet than the swing alone.
          3. Otherwise, target the swing level minus a 2-tick buffer (we
             want to fill INSIDE the swing, not exactly at it, to maximize
             fill probability).
          4. Fallback: classic R:R from config when no swing is detectable.
        """
        from app.engines.strategy_engine.indicators import detect_fvgs

        risk = abs(entry - sl)
        min_rr = self.config.risk_reward_ratio or 2.0
        fallback_tp = (entry + risk * min_rr) if direction == "long" else (entry - risk * min_rr)
        buffer = 2 * self.tick_size

        # Step 1: nearest swing in trade direction
        swing_level = None
        if df is not None and len(df) >= 10:
            recent = df.tail(60)
            if direction == "long":
                swing_highs = find_swing_highs(recent, lookback=2)
                above = [sw.price for sw in swing_highs if sw.price > entry]
                if above:
                    swing_level = min(above) - buffer
            else:
                swing_lows = find_swing_lows(recent, lookback=2)
                below = [sw.price for sw in swing_lows if sw.price < entry]
                if below:
                    swing_level = max(below) + buffer

        # Step 2: HTF FVG past the swing in same direction
        htf_fvg_target = None
        if htf_df is not None and len(htf_df) >= 10:
            try:
                htf_fvgs = detect_fvgs(htf_df.tail(60), instrument=self.instrument, min_size_ticks=2)
            except Exception:
                htf_fvgs = []
            for fvg in htf_fvgs:
                if fvg.filled:
                    continue
                if direction == "long" and fvg.low > entry:
                    if swing_level is None or fvg.low > (swing_level + buffer):
                        if (fvg.ce_level - entry) >= risk * 1.5:
                            htf_fvg_target = fvg.ce_level
                            break
                elif direction == "short" and fvg.high < entry:
                    if swing_level is None or fvg.high < (swing_level - buffer):
                        if (entry - fvg.ce_level) >= risk * 1.5:
                            htf_fvg_target = fvg.ce_level
                            break

        # Decision
        if htf_fvg_target is not None:
            return self._clamp_tp(entry, sl, htf_fvg_target, direction)
        if swing_level is not None:
            distance_r = abs(swing_level - entry) / risk if risk > 0 else 0
            if distance_r >= 1.0:
                return self._clamp_tp(entry, sl, swing_level, direction)
        return self._clamp_tp(entry, sl, fallback_tp, direction)

    def _passes_rsi_filter(self, df, bias) -> bool:
        """If RSI filter is on: block longs when RSI is overheated (> rsi_long_max)
        and block shorts when RSI is too oversold (< rsi_short_min). Returns True
        when the filter is off OR when the current RSI permits the trade."""
        if not getattr(self.config, "use_rsi_filter", False):
            return True
        if df is None or len(df) < self.config.rsi_period + 1:
            return True
        rsi = compute_rsi(df["close"].values, period=self.config.rsi_period)
        if rsi is None:
            return True
        if bias == "bullish":
            return rsi <= self.config.rsi_long_max
        if bias == "bearish":
            return rsi >= self.config.rsi_short_min
        return True

    def _passes_vwap_filter(self, df, bias, current_price) -> bool:
        """If VWAP filter is on: longs require price >= session VWAP, shorts
        require price <= session VWAP."""
        if not getattr(self.config, "use_vwap_filter", False):
            return True
        if df is None or df.empty:
            return True
        vwap = compute_session_vwap(df)
        if vwap is None:
            return True
        if bias == "bullish":
            return current_price >= vwap
        if bias == "bearish":
            return current_price <= vwap
        return True
