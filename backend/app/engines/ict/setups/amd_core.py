"""AMD Core - the shared Accumulation -> Manipulation -> Distribution engine.

Three named ICT strategies in the proposal are the **same** skeleton with
different session anchors (proposal SS3.9c explicitly recommends ONE shared
``amd_core`` parameterized by the accumulation / manipulation / entry windows
rather than four near-duplicate evaluators):

  * **Power of 3 (PO3)** (SS3.4) - accumulation = Asian 20:00-00:00 ET,
    manipulation + entry = London. ``reversal``. min RR 3. max 1/day.
  * **Judas Swing** (SS3.3) - the false move at the **London open** (02:00 ET)
    sweeps the Asian range then reverses with an MSS within 60 min of the open.
    ``reversal``. max <= 2/day.
  * **London Sweep into NY** (SS3.5) - London sweeps a side of the Asian range;
    the **NY session** (09:30-11:00 ET) reverses it; entry on the NY-session
    displacement + FVG. ``reversal`` (default). max 1/day.

This module holds ONLY the canonical AMD sequence (:class:`AMDCore`); the three
thin wrappers live in their own modules (``po3.py``, ``judas_swing.py``,
``london_into_ny.py``) and register themselves. Anything NOT registered keeps
falling back to the generic ``ICTStrategy`` model (``registry.get_setup``
returns ``None``), so porting these three cannot regress the baseline, Silver
Bullet, Inversion Tap, NY PM, or SMT.

The canonical AMD/PO3 sequence (proposal SS3.3/3.4/3.5/3.9c):

  1. **Accumulation.** Compute the ``accumulation_session`` high/low - the
     range that gets manipulated (e.g. the Asian 20:00-00:00 ET range).
  2. **Manipulation.** During ``manipulation_killzone`` require a **liquidity
     sweep** of the range HIGH (wick beyond, close back inside -> short bias)
     OR the range LOW (-> long bias), via ``detect_liquidity_sweeps``.
  3. **Distribution / entry.** AFTER the sweep, during ``entry_killzone``,
     require **displacement + MSS** (``detect_mss``) in the OPPOSITE direction
     leaving an **FVG**; enter at that FVG's CE. Long if the low was swept,
     short if the high was swept (= reversal). ``continuation`` mode mirrors
     the bias of the sweep instead.
  4. **Stop.** 2 ticks beyond the manipulation swing (the sweep extreme).
     **Target.** the opposing liquidity (the other side of the range / next
     pool); enforce ``min_rr``.
  5. **Max trades/day** + ET-date tracking (mirrors Silver Bullet).

Decision logging (per task spec):
  ``[ict:amd:{variant}] fire {inst} dir=.. entry=.. stop=.. tgt=.. rr=..`` on a
  fire; ``[ict:amd:{variant}] skip - {reason}`` on every rejection.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from loguru import logger

from app.engines.ict.base import ICTSetup
from app.engines.ict.context import ICTContext
from app.engines.ict.primitives import (
    detect_fvgs,
    detect_liquidity_sweeps,
    detect_mss,
    find_swing_highs,
    find_swing_lows,
    get_tick_size,
    is_in_session,
    price_in_fvg,
    session_range,
)
from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType

#: Stop sits 2 ticks beyond the manipulation (sweep) extreme (SS3.3.6/3.4.6).
_STOP_BUFFER_TICKS = 2.0
#: Swing-detection lookback for sweeps / structure / pools (engine default 3).
_SWING_LOOKBACK = 3
#: Execution-TF scan window (bars) for sweep + MSS + FVG detection. Generous
#: enough to span the manipulation killzone and the distribution leg.
_EXEC_SCAN_BARS = 120
#: Minimum sweep penetration (ticks) for the manipulation leg.
_MIN_SWEEP_TICKS = 0.5


class AMDCore(ICTSetup):
    """Shared Accumulation -> Manipulation -> Distribution evaluator.

    Subclasses (PO3 / Judas / London) set the class attributes below and
    register themselves; they do NOT override :meth:`evaluate`. Each is the
    same canonical AMD sequence anchored to different ET windows.
    """

    # --- variant identity / metadata --------------------------------------
    #: short id used in logs + signal metadata (e.g. "po3", "judas_swing").
    variant: str = "amd"

    # --- session anchors (subclasses override) ----------------------------
    #: (start_et, end_et) of the accumulation range to map (via session_range).
    accumulation_session: tuple[str, str] = ("20:00", "00:00")
    #: session keys (is_in_session) during which the manipulation sweep must
    #: occur (e.g. ["LONDON"]).
    manipulation_killzone: tuple[str, ...] = ("LONDON",)
    #: session keys during which the distribution entry may fire.
    entry_killzone: tuple[str, ...] = ("LONDON",)

    # --- behaviour knobs --------------------------------------------------
    #: "reversal" (default; trade opposite the sweep) or "continuation".
    mode: str = "reversal"
    #: Optional cap (minutes) between the manipulation sweep and the entry bar.
    #: ``None`` = no cap. Judas uses 60 (sweep + MSS within 60 min of the open).
    trap_window_min: Optional[int] = None
    #: Max trades per ET day for this variant.
    max_trades_day: int = 1
    #: Minimum reward:risk. Below this the opposing pool is too close -> SKIP.
    min_rr: float = 3.0

    # ------------------------------------------------------------------
    def evaluate(self, ctx: ICTContext) -> Optional[TradeSignal]:
        inst = ctx.instrument
        cfg = ctx.config
        v = self.variant

        # --- execution-TF bars (sweep / MSS / FVG print here) --------------
        exec_df = ctx.execution
        if exec_df is None or len(exec_df) < _SWING_LOOKBACK * 2 + 2:
            logger.info(f"[ict:amd:{v}] skip - no execution bars ({inst})")
            return None
        df = exec_df.tail(_EXEC_SCAN_BARS)
        ts = df.index[-1]

        # --- entry window gate: the distribution entry may only fire here ---
        if not is_in_session(ts, list(self.entry_killzone)):
            logger.info(
                f"[ict:amd:{v}] skip - outside entry killzone "
                f"{list(self.entry_killzone)} @ {ts} ({inst})"
            )
            return None

        # --- max-trades/day guard (ET date; entry_guard is the hard cap) ---
        if not self._under_daily_cap(ctx, ts):
            logger.info(
                f"[ict:amd:{v}] skip - max {self.max_trades_day}/day reached ({inst})"
            )
            return None

        # --- (1) Accumulation: the range to be manipulated ------------------
        acc_hi, acc_lo = session_range(df, self.accumulation_session[0], self.accumulation_session[1])
        if acc_hi is None or acc_lo is None or acc_hi <= acc_lo:
            logger.info(
                f"[ict:amd:{v}] skip - no accumulation range "
                f"{self.accumulation_session} ({inst})"
            )
            return None

        # --- (2) Manipulation: a sweep of the range in the killzone ---------
        swept_side, sweep_extreme, sweep_idx = self._manipulation_sweep(df, inst, acc_hi, acc_lo)
        if swept_side is None:
            logger.info(
                f"[ict:amd:{v}] skip - no range sweep in manipulation killzone "
                f"{list(self.manipulation_killzone)} ({inst})"
            )
            return None

        # Reversal: trade OPPOSITE the swept side (low swept -> long, high ->
        # short). Continuation: trade WITH the sweep.
        if self.mode == "continuation":
            direction = "short" if swept_side == "high" else "long"
        else:
            direction = "long" if swept_side == "low" else "short"

        # --- (3) Distribution: MSS + displacement leaving an FVG -----------
        mss = detect_mss(df, lookback=_SWING_LOOKBACK)
        want_mss = "up" if direction == "long" else "down"
        if mss is None or mss.direction != want_mss:
            logger.info(
                f"[ict:amd:{v}] skip - no {want_mss} MSS after {swept_side}-sweep ({inst})"
            )
            return None
        # The MSS must come AFTER the manipulation sweep (distribution follows
        # manipulation, never precedes it).
        if mss.bar_index <= sweep_idx:
            logger.info(
                f"[ict:amd:{v}] skip - MSS (bar {mss.bar_index}) not after sweep "
                f"(bar {sweep_idx}) ({inst})"
            )
            return None

        # Optional trap window: sweep -> entry within ``trap_window_min`` (Judas).
        if self.trap_window_min is not None:
            gap_min = self._minutes_between(df, sweep_idx, len(df) - 1)
            if gap_min is None or gap_min > self.trap_window_min:
                logger.info(
                    f"[ict:amd:{v}] skip - trap window exceeded "
                    f"({gap_min} > {self.trap_window_min} min) ({inst})"
                )
                return None

        # The FVG left by the displacement that broke structure, in the
        # distribution direction.
        fvg = self._distribution_fvg(df, inst, direction, sweep_idx)
        if fvg is None:
            logger.info(
                f"[ict:amd:{v}] skip - no {direction} FVG from distribution displacement ({inst})"
            )
            return None

        tick = get_tick_size(inst)

        # --- entry = FVG CE (consequent encroachment) ----------------------
        ce = float(fvg.ce_level) if fvg.ce_level else float((fvg.high + fvg.low) / 2.0)
        entry = ce

        # --- (4) stop = 2 ticks beyond the manipulation (sweep) extreme ----
        sl = self._stop_from_structure(
            df, direction, inst,
            buffer_ticks=_STOP_BUFFER_TICKS, anchor_level=float(sweep_extreme),
        )
        if sl is None:
            logger.info(f"[ict:amd:{v}] skip - could not place stop ({inst})")
            return None
        if abs(entry - sl) < tick * 2:
            logger.info(
                f"[ict:amd:{v}] skip - stop too tight (entry={entry} stop={sl}) ({inst})"
            )
            return None

        # --- target = opposing liquidity (the other side / next pool) ------
        tgt = self._opposing_liquidity(df, inst, direction, acc_hi, acc_lo, fvg)
        if tgt is None:
            logger.info(f"[ict:amd:{v}] skip - no opposing liquidity target ({inst})")
            return None

        # --- geometry sanity (long: tgt>entry>stop; short: tgt<entry<stop) -
        if direction == "long" and not (tgt > entry > sl):
            logger.info(
                f"[ict:amd:{v}] skip - bad long geometry "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None
        if direction == "short" and not (tgt < entry < sl):
            logger.info(
                f"[ict:amd:{v}] skip - bad short geometry "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None

        # --- min RR gate: opposing pool < min_rr away -> skip --------------
        min_rr = float(getattr(cfg, "risk_reward_ratio", None) or self.min_rr)
        # The proposal LOCKS the variant min RR (e.g. PO3 3.0); never accept a
        # looser gate than that even if config carries a smaller RR.
        min_rr = max(min_rr, self.min_rr)
        if not self._min_rr_ok(entry, sl, tgt, min_rr=min_rr):
            risk = abs(entry - sl)
            rr_now = abs(tgt - entry) / risk if risk > 0 else 0.0
            logger.info(
                f"[ict:amd:{v}] skip - RR<{min_rr} (pool {rr_now:.2f}R away) "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None

        risk = abs(entry - sl)
        rr = abs(tgt - entry) / risk if risk > 0 else 0.0

        # --- fire ----------------------------------------------------------
        logger.info(
            f"[ict:amd:{v}] fire {inst} dir={direction} entry={entry} "
            f"stop={sl} tgt={tgt} rr={rr:.2f}"
        )
        self._record_fire(ctx, ts)

        sig_type = SignalType.LONG if direction == "long" else SignalType.SHORT
        return TradeSignal(
            signal=sig_type,
            instrument=inst,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tgt,
            contracts=int(getattr(cfg, "max_contracts", 1) or 1),
            metadata={
                "setup": self.variant,
                "amd": True,
                "mode": self.mode,
                "swept_side": swept_side,
                "sweep_extreme": float(sweep_extreme),
                "accumulation_high": float(acc_hi),
                "accumulation_low": float(acc_lo),
                "accumulation_session": list(self.accumulation_session),
                "manipulation_killzone": list(self.manipulation_killzone),
                "entry_killzone": list(self.entry_killzone),
                "mss_level": float(mss.broken_level),
                "fvg_type": fvg.direction,
                "fvg_high": float(fvg.high),
                "fvg_low": float(fvg.low),
                "ce_level": ce,
                "entry_mode": "fvg_ce",
                "rr": float(rr),
                "max_trades_per_day": self.max_trades_day,
                "primary_tf": cfg.primary_timeframe,
            },
        )

    # ------------------------------------------------------------------
    # Steps 2-4 helpers
    # ------------------------------------------------------------------
    def _manipulation_sweep(
        self, df: pd.DataFrame, inst: str, acc_hi: float, acc_lo: float
    ) -> tuple[Optional[str], Optional[float], Optional[int]]:
        """Find the manipulation sweep of the accumulation range.

        Returns ``("high"|"low", sweep_extreme, sweep_bar_index)`` for the most
        recent sweep of the range high (wick above ``acc_hi``, close back below)
        or low (wick below ``acc_lo``, close back above) whose **sweep bar** is
        inside the ``manipulation_killzone``. ``(None, None, None)`` if none.

        The sweep extreme is the wick (``sweep_high`` for a high sweep, the
        ``sweep_low`` for a low sweep) - the price the protective stop sits
        beyond.
        """
        try:
            sweeps = detect_liquidity_sweeps(
                df, lookback=_SWING_LOOKBACK, instrument=inst,
                min_sweep_ticks=_MIN_SWEEP_TICKS,
            )
        except Exception as exc:
            logger.warning(f"[ict:amd:{self.variant}] sweep detection failed ({inst}): {exc!r}")
            return None, None, None

        best: tuple[Optional[str], Optional[float], Optional[int]] = (None, None, None)
        for s in sweeps:
            # The sweep must take liquidity of the ACCUMULATION RANGE itself,
            # not just any prior swing: a high sweep must wick above acc_hi and
            # close back below it; a low sweep must wick below acc_lo and close
            # back above it.
            if s.direction == "high_sweep":
                if not (s.sweep_high > acc_hi and s.sweep_close < acc_hi):
                    continue
                side, extreme = "high", float(s.sweep_high)
            elif s.direction == "low_sweep":
                if not (s.sweep_low < acc_lo and s.sweep_close > acc_lo):
                    continue
                side, extreme = "low", float(s.sweep_low)
            else:
                continue
            # The sweep BAR must fall inside the manipulation killzone.
            try:
                sweep_ts = df.index[s.sweep_bar_index]
            except Exception:
                continue
            if not is_in_session(sweep_ts, list(self.manipulation_killzone)):
                continue
            # Keep the most RECENT qualifying sweep (highest bar index).
            if best[2] is None or s.sweep_bar_index > best[2]:
                best = (side, extreme, int(s.sweep_bar_index))
        return best

    def _distribution_fvg(
        self, df: pd.DataFrame, inst: str, direction: str, sweep_idx: int
    ) -> Optional[object]:
        """The FVG left by the distribution displacement, in ``direction``.

        Bullish distribution (long) wants a **bullish** FVG; bearish wants a
        **bearish** FVG. We take the most-recent qualifying FVG that formed
        AFTER the manipulation sweep (its displacement is the move that broke
        structure). Returns the :class:`FairValueGap` or ``None``.
        """
        try:
            fvgs = detect_fvgs(df, instrument=inst, min_size_ticks=1)
        except Exception as exc:
            logger.warning(f"[ict:amd:{self.variant}] fvg detection failed ({inst}): {exc!r}")
            return None
        if not fvgs:
            return None
        want = "bullish" if direction == "long" else "bearish"
        # Formed after the sweep (distribution follows manipulation), matching
        # the requested polarity. Most recent first.
        eligible = [
            f for f in fvgs
            if f.direction == want and getattr(f, "bar_index", 0) > sweep_idx
        ]
        if not eligible:
            return None
        eligible.sort(key=lambda f: getattr(f, "bar_index", 0), reverse=True)
        return eligible[0]

    def _opposing_liquidity(
        self, df: pd.DataFrame, inst: str, direction: str,
        acc_hi: float, acc_lo: float, fvg: object,
    ) -> Optional[float]:
        """The opposing-side draw the distribution runs TOWARD.

        For a LONG (low was swept) the first draw is the **opposite extreme of
        the accumulation range** (its HIGH) and any old swing high resting
        above the FVG; we take the NEAREST such pool above the FVG high. For a
        SHORT, mirror below the FVG low using the range LOW. The boundary is the
        FVG's far edge so the FVG's own displacement candles never count.
        Returns the price or ``None`` if nothing rests on the opposing side.
        """
        fvg_high = float(fvg.high)
        fvg_low = float(fvg.low)
        levels: list[float] = []

        # The opposite side of the accumulation range is the primary draw.
        if direction == "long":
            levels.append(float(acc_hi))
        else:
            levels.append(float(acc_lo))

        # Plus confirmed structural old highs/lows (resting liquidity).
        try:
            if direction == "long":
                for s in find_swing_highs(df, _SWING_LOOKBACK):
                    levels.append(float(s.price))
            else:
                for s in find_swing_lows(df, _SWING_LOOKBACK):
                    levels.append(float(s.price))
        except Exception:
            pass

        if direction == "long":
            above = [p for p in levels if p > fvg_high]
            return min(above) if above else None
        below = [p for p in levels if p < fvg_low]
        return max(below) if below else None

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _minutes_between(df: pd.DataFrame, i: int, j: int) -> Optional[int]:
        """Whole minutes between bars ``i`` and ``j`` (abs). None on failure."""
        try:
            a = pd.Timestamp(df.index[i])
            b = pd.Timestamp(df.index[j])
            return int(abs((b - a).total_seconds()) // 60)
        except Exception:
            return None

    # --- per-day trade cap (entry_guard is the hard gate) -----------------
    @staticmethod
    def _et_date(ts) -> str:
        try:
            t = pd.Timestamp(ts)
            if t.tz is None:
                t = t.tz_localize("UTC")
            return str(t.tz_convert("US/Eastern").date())
        except Exception:
            return "?"

    def _ledger_key(self) -> str:
        return f"_amd_{self.variant}_fires"

    def _under_daily_cap(self, ctx: ICTContext, ts) -> bool:
        if self.max_trades_day <= 0:
            return True
        fired = ctx.extra.get(self._ledger_key(), {}) if isinstance(ctx.extra, dict) else {}
        return fired.get(self._et_date(ts), 0) < self.max_trades_day

    def _record_fire(self, ctx: ICTContext, ts) -> None:
        if not isinstance(ctx.extra, dict):
            return
        fired = ctx.extra.setdefault(self._ledger_key(), {})
        d = self._et_date(ts)
        fired[d] = fired.get(d, 0) + 1
