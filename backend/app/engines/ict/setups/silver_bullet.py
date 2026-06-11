"""ICT Silver Bullet - the second strategy ported to the registry.

This setup formalizes the user-approved ICT Silver Bullet rules (proposal
SS3.2) as a first-class :class:`ICTSetup`, scoping the entry to the single
canonical Silver Bullet hour (**10:00-11:00 AM ET**) and the *first* FVG that
forms after 10:00 in the direction of the higher-timeframe (1H) bias. Every
other strategy keeps falling back to the generic ``ICTStrategy`` model
(``registry.get_setup`` returns ``None`` for them), so porting this one cannot
regress the baseline or the other strategies.

Rules (proposal SS3.2, LOCKED by the user):
  * **Window** = 10:00-11:00 ET ONLY (session key ``SILVER_BULLET``). Outside
    that hour the setup stands aside - it never fires in the wider NY_AM band
    the generic model used. (SS3.2.8: PM 14:00-15:00 and London 03:00-04:00
    windows are OFF by default - single 10-11 ET window only.)
  * **Setup/entry** = the FIRST FVG (via ``detect_fvgs``) that *forms after
    10:00 ET* on the primary TF, in the direction of the 1H bias. A bullish
    FVG (1H bias up) -> LONG; a bearish FVG (1H bias down) -> SHORT. Entry is
    the FVG's CE (consequent encroachment / 50%), the retrace level price is
    expected to trade back into (SS3.2.4.iv ``entry_mode fvg_ce``).
  * **Bias** = require 1H agreement (SS3.2.4 default): longs only if the 1H
    EMA(9>21) bias is up; shorts only if down. If no 1H frame is available the
    setup stands aside (it cannot confirm the draw-on-liquidity direction).
  * **Stop** = 2 ticks beyond the swing that formed the entry FVG (SS3.2.6):
    below the FVG's protective low for longs, above its high for shorts -
    anchored via the shared ``_stop_from_structure`` helper.
  * **Target** = the nearest OPPOSING liquidity the displacement is running
    toward (old swing high/low or session high/low) - above for longs, below
    for shorts (SS3.2.7). Enforce **min RR 2.0**: if the nearest pool is < 2R
    away, SKIP (no trade) rather than invent a target.
  * **Max trades/day** = **1** (SS3.2.9): a single-window setup. Once it fires
    (or once 11:00 ET passes) it is done for the day. Tracked best-effort on
    the ET date via ``ctx.extra``; the engine's ``entry_guard`` is the hard cap.
  * **Sizing** = ``config.max_contracts`` (risk-based sizing is the separate
    SS4 build step, intentionally out of scope for this port).

Decision logging (per task spec):
  ``[ict:silver_bullet] fire {inst} dir=.. entry=.. stop=.. tgt=.. rr=..`` on
  a fire; ``[ict:silver_bullet] skip - {reason}`` on every rejection (outside
  window / no FVG after 10:00 / RR<2 / already fired today / bias disagree).
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
    find_swing_highs,
    find_swing_lows,
    get_tick_size,
    is_in_session,
    session_range,
)
from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType

#: SS3.2.1: the Silver Bullet window opens at 10:00 ET. Only FVGs that form at
#: or after this wall-clock minute (on the current ET date) qualify.
_WINDOW_OPEN_MIN = 10 * 60  # 10:00 ET, minutes since ET midnight
#: SS3.2 session key for the 10:00-11:00 ET window.
_SESSION_KEY = "SILVER_BULLET"
#: SS3.2.7: minimum reward:risk. Below this the liquidity pool is too close and
#: we stand aside rather than invent a target. User-locked default 2.0.
_DEFAULT_MIN_RR = 2.0
#: SS3.2.9: single-window setup -> one trade per ET day.
_MAX_TRADES_PER_DAY = 1
#: SS3.2.6: structural stop sits 2 ticks beyond the FVG-forming swing.
_STOP_BUFFER_TICKS = 2.0
#: Swing-detection lookback for liquidity pools / structure (engine default 3).
_SWING_LOOKBACK = 3
#: FVG scan window on the primary TF (bars). Generous enough to span the
#: pre-window context the bias needs plus the in-window FVGs.
_FVG_SCAN_BARS = 60


# Register under BOTH the seed name ("ICT Silver Bullet" -> "ict_silver_bullet")
# AND the short ``rule_tree.ict_setup`` id ("silver_bullet"). The two normalize
# to different keys, so we stack the decorator to resolve via either path:
#   get_setup("ICT Silver Bullet")                         -> this setup
#   get_setup("anything", {"ict_setup": "silver_bullet"})  -> this setup
# (Stacking applies bottom-up; the topmost @register wins for ``cls.name``.)
@register("silver_bullet")
@register("ICT Silver Bullet")
class SilverBullet(ICTSetup):
    """First FVG after 10:00 ET, with the 1H bias, into the nearest liquidity."""

    def evaluate(self, ctx: ICTContext) -> Optional[TradeSignal]:
        inst = ctx.instrument
        cfg = ctx.config

        # --- primary-TF bars (structure / FVG detection) -------------------
        pdf = ctx.primary
        if pdf is None or len(pdf) < _SWING_LOOKBACK * 2 + 2:
            logger.info(f"[ict:silver_bullet] skip - no primary bars ({inst})")
            return None

        ts = pdf.index[-1]

        # --- window gate: 10:00-11:00 ET ONLY (SS3.2.1) --------------------
        if not is_in_session(ts, [_SESSION_KEY]):
            logger.info(
                f"[ict:silver_bullet] skip - outside window "
                f"(not {_SESSION_KEY} 10:00-11:00 ET @ {ts}) ({inst})"
            )
            return None

        # --- max-1-trade/day guard (SS3.2.9) -------------------------------
        if not self._under_daily_cap(ctx, ts):
            logger.info(
                f"[ict:silver_bullet] skip - already fired today "
                f"(max {_MAX_TRADES_PER_DAY}/day) ({inst})"
            )
            return None

        # --- 1H bias (SS3.2.4: require agreement) --------------------------
        bias = self._htf_bias(ctx)
        if bias is None:
            logger.info(f"[ict:silver_bullet] skip - no 1H bias available ({inst})")
            return None

        # --- first FVG that FORMED after 10:00 ET, aligned with the bias ---
        fvg = self._first_fvg_after_open(pdf, inst, bias)
        if fvg is None:
            logger.info(
                f"[ict:silver_bullet] skip - no {bias} FVG after 10:00 ET ({inst})"
            )
            return None

        direction = "long" if fvg.direction == "bullish" else "short"
        # Redundant with the selection filter, but make the bias-disagree branch
        # explicit + logged (defensive; also documents the rule).
        if (direction == "long" and bias != "bullish") or (
            direction == "short" and bias != "bearish"
        ):
            logger.info(
                f"[ict:silver_bullet] skip - bias disagree "
                f"(fvg={fvg.direction} 1H_bias={bias}) ({inst})"
            )
            return None

        tick = get_tick_size(inst)

        # --- entry = FVG CE (the retrace level, SS3.2.4.iv) ----------------
        ce = float(fvg.ce_level) if fvg.ce_level else float((fvg.high + fvg.low) / 2.0)
        entry = ce

        # --- stop = 2 ticks beyond the FVG-forming swing (SS3.2.6) ---------
        # Anchor to the FVG's protective boundary (its low for a long, high for a
        # short) - that boundary IS the swing the displacement left behind.
        anchor = float(fvg.low) if direction == "long" else float(fvg.high)
        sl = self._stop_from_structure(
            pdf, direction, inst,
            buffer_ticks=_STOP_BUFFER_TICKS, anchor_level=anchor,
        )
        if sl is None:
            logger.info(f"[ict:silver_bullet] skip - could not place stop ({inst})")
            return None
        if abs(entry - sl) < tick * 2:
            logger.info(
                f"[ict:silver_bullet] skip - stop too tight "
                f"(entry={entry} stop={sl}) ({inst})"
            )
            return None

        # --- target = nearest opposing liquidity (SS3.2.7) -----------------
        tgt = self._nearest_opposing_liquidity(ctx, pdf, inst, direction, entry)
        if tgt is None:
            logger.info(
                f"[ict:silver_bullet] skip - no opposing liquidity target ({inst})"
            )
            return None

        # --- geometry sanity (long: tgt>entry>stop; short: tgt<entry<stop) -
        if direction == "long" and not (tgt > entry > sl):
            logger.info(
                f"[ict:silver_bullet] skip - bad long geometry "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None
        if direction == "short" and not (tgt < entry < sl):
            logger.info(
                f"[ict:silver_bullet] skip - bad short geometry "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None

        # --- min RR 2.0 gate (SS3.2.7): pool < 2R away -> skip -------------
        min_rr = float(getattr(cfg, "risk_reward_ratio", None) or _DEFAULT_MIN_RR)
        # The user LOCKED min RR 2.0 for this setup; never accept a looser gate
        # than that even if config carries a smaller RR.
        min_rr = max(min_rr, _DEFAULT_MIN_RR)
        if not self._min_rr_ok(entry, sl, tgt, min_rr=min_rr):
            risk = abs(entry - sl)
            rr_now = abs(tgt - entry) / risk if risk > 0 else 0.0
            logger.info(
                f"[ict:silver_bullet] skip - RR<{min_rr} "
                f"(liquidity {rr_now:.2f}R away) "
                f"(entry={entry} stop={sl} tgt={tgt}) ({inst})"
            )
            return None

        risk = abs(entry - sl)
        rr = abs(tgt - entry) / risk if risk > 0 else 0.0

        # --- fire ----------------------------------------------------------
        logger.info(
            f"[ict:silver_bullet] fire {inst} dir={direction} entry={entry} "
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
                "setup": "silver_bullet",
                "bias": bias,
                "fvg_type": fvg.direction,
                "fvg_high": float(fvg.high),
                "fvg_low": float(fvg.low),
                "ce_level": ce,
                "entry_mode": "fvg_ce",
                "session": _SESSION_KEY,
                "rr": float(rr),
                "max_trades_per_day": _MAX_TRADES_PER_DAY,
                "primary_tf": cfg.primary_timeframe,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _htf_bias(ctx: ICTContext) -> Optional[str]:
        """1H EMA(9>21) bias: "bullish" | "bearish" | None.

        Mirrors ``ICTStrategy._ema_crossover_bias`` (fast 9 > slow 21 = bullish)
        applied to the 1H frame, which the seed lists as the strategy's HTF.
        Returns ``None`` when no usable 1H frame is present so the caller stands
        aside (Silver Bullet requires bias agreement, SS3.2.4).
        """
        df = ctx.tf("1H")
        if df is None or len(df) < 15:
            return None
        closes = df["close"].values
        fast = pd.Series(closes).ewm(span=9).mean().values
        slow = pd.Series(closes).ewm(span=21).mean().values
        if fast[-1] > slow[-1]:
            return "bullish"
        if fast[-1] < slow[-1]:
            return "bearish"
        return None

    @staticmethod
    def _et_minutes(ts) -> Optional[int]:
        """Wall-clock minutes-since-midnight of ``ts`` in ET (or None)."""
        try:
            t = pd.Timestamp(ts)
            if t.tz is None:
                t = t.tz_localize("UTC")
            et = t.tz_convert("US/Eastern")
            return int(et.hour) * 60 + int(et.minute)
        except Exception:
            return None

    def _first_fvg_after_open(
        self, pdf: pd.DataFrame, inst: str, bias: str
    ) -> Optional[object]:
        """The FIRST FVG (earliest formation) that formed at/after 10:00 ET on
        the current ET date AND matches the 1H bias (bullish FVG for an up bias,
        bearish for a down bias). Returns the :class:`FairValueGap` or ``None``.

        "Formed after 10:00" is judged by the FVG's own timestamp (the third /
        completing candle), localized to ET, restricted to the latest bar's ET
        date so a pool left on a prior day cannot qualify.
        """
        try:
            fvgs = detect_fvgs(pdf.tail(_FVG_SCAN_BARS), instrument=inst, min_size_ticks=1)
        except Exception as exc:
            logger.warning(f"[ict:silver_bullet] fvg detection failed ({inst}): {exc!r}")
            return None
        if not fvgs:
            return None

        want = "bullish" if bias == "bullish" else "bearish"
        cur_date = self._et_date(pdf.index[-1])

        eligible = []
        for f in fvgs:
            if f.direction != want:
                continue
            fm = self._et_minutes(f.timestamp)
            if fm is None or fm < _WINDOW_OPEN_MIN:
                continue
            # Same ET date as the current bar (no stale prior-day gaps), and the
            # FVG must already have completed (not in the future).
            if self._et_date(f.timestamp) != cur_date:
                continue
            eligible.append(f)

        if not eligible:
            return None
        # FIRST after 10:00 == earliest by bar_index (formation order).
        eligible.sort(key=lambda f: getattr(f, "bar_index", 0))
        return eligible[0]

    def _nearest_opposing_liquidity(
        self, ctx: ICTContext, pdf: pd.DataFrame, inst: str,
        direction: str, entry: float,
    ) -> Optional[float]:
        """Nearest opposing liquidity pool the displacement runs toward.

        For a LONG: the nearest pool ABOVE ``entry`` (old swing high or session
        high). For a SHORT: nearest pool BELOW ``entry`` (old swing low or
        session low). Returns the price or ``None`` if nothing sits on the
        opposing side.
        """
        levels: list[float] = []

        # Session high/low of the current Silver Bullet window's surrounding
        # NY session (use the SILVER_BULLET window itself for the in-window
        # extreme; fall back to the morning if empty).
        for win in (("10:00", "11:00"), ("09:30", "12:00")):
            hi, lo = session_range(pdf, win[0], win[1])
            if direction == "long" and hi is not None:
                levels.append(float(hi))
            if direction == "short" and lo is not None:
                levels.append(float(lo))

        # Structural old highs/lows (liquidity resting at swing points).
        try:
            if direction == "long":
                for s in find_swing_highs(pdf, _SWING_LOOKBACK):
                    levels.append(float(s.price))
            else:
                for s in find_swing_lows(pdf, _SWING_LOOKBACK):
                    levels.append(float(s.price))
        except Exception:
            pass

        if direction == "long":
            above = [p for p in levels if p > entry]
            return min(above) if above else None
        else:
            below = [p for p in levels if p < entry]
            return max(below) if below else None

    # --- per-day trade cap (SS3.2.9; entry_guard is the hard gate) ---------
    @staticmethod
    def _et_date(ts) -> str:
        try:
            t = pd.Timestamp(ts)
            if t.tz is None:
                t = t.tz_localize("UTC")
            return str(t.tz_convert("US/Eastern").date())
        except Exception:
            return "?"

    def _under_daily_cap(self, ctx: ICTContext, ts) -> bool:
        fired = ctx.extra.get("_silver_bullet_fires", {}) if isinstance(ctx.extra, dict) else {}
        return fired.get(self._et_date(ts), 0) < _MAX_TRADES_PER_DAY

    def _record_fire(self, ctx: ICTContext, ts) -> None:
        if not isinstance(ctx.extra, dict):
            return
        fired = ctx.extra.setdefault("_silver_bullet_fires", {})
        d = self._et_date(ts)
        fired[d] = fired.get(d, 0) + 1
