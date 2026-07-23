"""TradingView-style annotated trade-chart renderer.

Produces a PNG (bytes) showing candlesticks for the recent bars with the
entry / stop / target lines, the red risk zone + green reward zone (exactly
like TradingView's long/short position tool), labelled price levels, an R:R
badge, and any caller-supplied key levels (VWAP, prev high, swing low, FVG).

Headless by design — `matplotlib.use("Agg")` is set at import so this works
inside the backend container with no display. matplotlib is the only hard
dependency (candlesticks are drawn manually; mplfinance is optional and not
required).

The single most important rule: NEVER render a misleading chart. Trade
geometry is validated FIRST and if it's invalid (long without
target>entry>stop, or short without target<entry<stop) we log a warning and
return None so the caller can send the email without a chart instead of
shipping a picture that contradicts the numbers.
"""
from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

try:  # zoneinfo ships with 3.9+; guarded so an exotic env can't kill the import
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET_TZ = None

import matplotlib
matplotlib.use("Agg")  # headless — must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from loguru import logger


# ── Thread-safety guard (SEGFAULT-FIX-V1) ───────────────────────────────
# matplotlib's pyplot API (plt.subplots / fig.savefig / plt.close below) is
# STATEFUL and NOT thread-safe: every call mutates matplotlib's process-global
# figure-manager registry (Gcf) and drives the non-reentrant C/Agg backend.
# This renderer is invoked from send_signal_email via asyncio.to_thread, so at
# the market open a fan-out of simultaneous account-signal watcher tasks lands
# multiple worker threads inside plt.* at once. Concurrent mutation of that
# global C-level state corrupts the Agg backend and takes the whole process
# down with a SIGSEGV (exit 139) and no Python traceback — exactly the crash
# we saw at the open. Serialising the ENTIRE render (figure create -> savefig
# -> close) behind one module-level lock makes only one thread render at a
# time; the fan-out just queues briefly (renders are ~tens of ms). Follow-up:
# migrating to the OO API (Figure()+FigureCanvasAgg, no pyplot/global state)
# would remove the shared state entirely and let renders run in parallel — the
# lock is the minimal, low-risk fix for now.
_RENDER_LOCK = threading.Lock()


# Candle colours — green up / red down, TradingView-ish palette.
_UP = "#26a69a"
_DOWN = "#ef5350"
_ENTRY_C = "#2563eb"   # blue solid
_STOP_C = "#dc2626"    # red dashed
_TARGET_C = "#16a34a"  # green dashed
_RISK_FILL = "#dc2626"
_REWARD_FILL = "#16a34a"
_LEVEL_C = "#7c3aed"   # violet for key levels


# ── Pure window / label helpers (CHART-TRUTH-V1) ────────────────────────
# Extracted as pure functions so tests can pin the window math and the ET
# label format with no rendering (backend/tests/test_trade_chart_window.py).
# Why: the 2026-07-13 NQ short email chart plotted delayed proxy bars whose
# x axis (raw UTC, no fire marker) appeared to END before the signal fired.

def _as_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC; aware ones are converted to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_signal_window(fire_time: datetime, now: Optional[datetime] = None,
                          before_min: int = 45, after_min: int = 45):
    """Bar window for a signal chart: [fire-45m, fire+45m], clipped to `now`
    (at send time no post-entry bars exist yet). Always returns a tz-aware
    UTC (start, end) pair, and end is never before fire_time so a skewed
    clock cannot produce a chart that stops before the entry."""
    fire_utc = _as_utc(fire_time)
    now_utc = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    start = fire_utc - timedelta(minutes=before_min)
    end = min(fire_utc + timedelta(minutes=after_min), now_utc)
    if end < fire_utc:
        end = fire_utc
    return start, end


def format_et_label(ts: datetime) -> str:
    """X-axis tick label: Eastern wall-clock with an explicit 'ET' suffix,
    e.g. 2026-07-13 14:45 UTC (an EDT day) -> '10:45 ET'. Naive input is
    treated as UTC."""
    ts_et = _as_utc(ts)
    if _ET_TZ is not None:
        ts_et = ts_et.astimezone(_ET_TZ)
    return ts_et.strftime("%H:%M") + " ET"


def _validate_geometry(direction: str, entry: float, stop: float, target: float) -> bool:
    """Long: target > entry > stop. Short: target < entry < stop.
    Returns True only when strictly ordered (no equal legs — a zero-width
    risk or reward leg is degenerate and would divide-by-zero on R:R)."""
    d = (direction or "").lower()
    try:
        e, s, t = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return False
    if d in ("long", "buy"):
        return t > e > s
    if d in ("short", "sell"):
        return t < e < s
    return False


def _coerce_ohlc(bars_df):
    """Return a DataFrame with lowercase open/high/low/close columns + a
    DatetimeIndex, tolerating the common capitalisations (Open/High/...) and
    a 'timestamp' column instead of an index. Returns None if unusable."""
    if bars_df is None:
        return None
    try:
        df = bars_df.copy()
    except Exception:
        return None
    if getattr(df, "empty", True):
        return None
    # Normalise column names to lowercase.
    rename = {}
    for c in list(df.columns):
        lc = str(c).lower()
        if lc in ("open", "high", "low", "close", "volume", "timestamp"):
            rename[c] = lc
    if rename:
        df = df.rename(columns=rename)
    # Move a timestamp column into the index if present.
    if "timestamp" in df.columns:
        try:
            import pandas as pd
            df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True)))
        except Exception:
            pass
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            return None
    return df


def generate_trade_chart(
    *,
    symbol: str,
    timeframe: str,
    bars_df,
    entry: float,
    stop: float,
    target: float,
    direction: str,
    key_levels: Optional[dict] = None,
    stop_reason: Optional[str] = None,
    target_reason: Optional[str] = None,
    fire_time: Optional[datetime] = None,
) -> Optional[bytes]:
    """Render a TradingView-style annotated trade chart to PNG bytes.

    Returns None (and logs) if trade math is invalid."""
    # ── 1. Validate geometry FIRST — never render a misleading chart. ──
    if not _validate_geometry(direction, entry, stop, target):
        logger.warning(
            f"[trade-chart] invalid geometry sym={symbol} dir={direction} "
            f"e={entry} s={stop} t={target}"
        )
        return None

    entry = float(entry)
    stop = float(stop)
    target = float(target)
    d = (direction or "").lower()
    side_word = "LONG" if d in ("long", "buy") else "SHORT"
    rr = round(abs(target - entry) / abs(entry - stop), 1) if entry != stop else 0.0

    df = _coerce_ohlc(bars_df)

    # Serialise the entire pyplot render behind one lock — see _RENDER_LOCK.
    with _RENDER_LOCK:
        fig, ax = plt.subplots(figsize=(8.6, 5.0))
        try:
            # ── 2. Candlesticks (drawn manually). ──
            x_is_time = False
            if df is not None and len(df) > 0:
                # Decide an x sequence: prefer real timestamps, fall back to ints.
                try:
                    xs = mdates.date2num(df.index.to_pydatetime())
                    x_is_time = True
                except Exception:
                    xs = list(range(len(df)))
                # Width of each candle body in x-units (80% of spacing).
                if len(xs) >= 2:
                    step = min(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
                    if step <= 0:
                        step = 1.0
                else:
                    step = 1.0
                body_w = step * 0.7

                o = df["open"].astype(float).tolist()
                h = df["high"].astype(float).tolist()
                low = df["low"].astype(float).tolist()
                c = df["close"].astype(float).tolist()

                for i in range(len(xs)):
                    up = c[i] >= o[i]
                    color = _UP if up else _DOWN
                    # Wick: high → low.
                    ax.vlines(xs[i], low[i], h[i], color=color, linewidth=0.9, zorder=3)
                    # Body: open → close rectangle.
                    lo_body = min(o[i], c[i])
                    height = abs(c[i] - o[i]) or (max(h[i] - low[i], 1e-9) * 0.02)
                    ax.add_patch(Rectangle(
                        (xs[i] - body_w / 2, lo_body), body_w, height,
                        facecolor=color, edgecolor=color, linewidth=0.6, zorder=4,
                    ))
                x_min = xs[0] - step
                x_max = xs[-1] + step
            else:
                # No bars — still draw the position tool over a neutral x range so
                # the email shows the levels (better than nothing). 0..1 x range.
                x_min, x_max = 0.0, 1.0

            # ── 2b. Fire-time marker (CHART-TRUTH-V1). ──
            # Vertical line AT the moment the signal fired so "entry happened
            # HERE" is unambiguous. Drawn only when the caller passes fire_time
            # and the x axis is real timestamps (the futures signal email path);
            # the equity/Saro pick path passes nothing and renders as before.
            if fire_time is not None and x_is_time:
                try:
                    _fire_utc = _as_utc(fire_time)
                    fire_x = mdates.date2num(_fire_utc)
                    if fire_x > x_max - step:
                        x_max = fire_x + step  # keep the marker inside the frame
                    ax.axvline(fire_x, color="#f59e0b", linewidth=1.6,
                               linestyle="-.", zorder=6)
                    import matplotlib.transforms as _mtransforms
                    _btrans = _mtransforms.blended_transform_factory(
                        ax.transData, ax.transAxes)
                    ax.text(fire_x, 0.02, f" entry {format_et_label(_fire_utc)}",
                            transform=_btrans, va="bottom", ha="left", fontsize=8,
                            color="#b45309", fontweight="bold", zorder=7)
                except Exception:
                    pass

            # ── 3. Shaded risk/reward zones (TradingView position tool). ──
            # Risk: entry ↔ stop, red @ 0.12. Reward: entry ↔ target, green @ 0.12.
            ax.axhspan(min(entry, stop), max(entry, stop),
                       facecolor=_RISK_FILL, alpha=0.12, zorder=1)
            ax.axhspan(min(entry, target), max(entry, target),
                       facecolor=_REWARD_FILL, alpha=0.12, zorder=1)

            # ── 4. Entry / stop / target horizontal lines. ──
            ax.axhline(entry, color=_ENTRY_C, linewidth=1.6, linestyle="-", zorder=5)
            ax.axhline(stop, color=_STOP_C, linewidth=1.4, linestyle="--", zorder=5)
            ax.axhline(target, color=_TARGET_C, linewidth=1.4, linestyle="--", zorder=5)

            # ── 5. Key levels (optional). ──
            if key_levels:
                for name, val in key_levels.items():
                    try:
                        if isinstance(val, (tuple, list)) and len(val) == 2:
                            lo_v, hi_v = float(val[0]), float(val[1])
                            ax.axhspan(min(lo_v, hi_v), max(lo_v, hi_v),
                                       facecolor=_LEVEL_C, alpha=0.10, zorder=2)
                            ax.text(x_max, (lo_v + hi_v) / 2.0, f" {name}",
                                    va="center", ha="left", fontsize=8,
                                    color=_LEVEL_C, fontweight="bold", zorder=6)
                        else:
                            fv = float(val)
                            ax.axhline(fv, color=_LEVEL_C, linewidth=1.0,
                                       linestyle=":", alpha=0.8, zorder=4)
                            ax.text(x_max, fv, f" {name} {fv:g}", va="center",
                                    ha="left", fontsize=8, color=_LEVEL_C,
                                    fontweight="bold", zorder=6)
                    except (TypeError, ValueError):
                        continue

            # ── 6. Right-edge labels for entry/stop/target. ──
            # Prefer the explicit caller-supplied level reasons (e.g.
            # "swing low", "London high") so the label reads
            # "STOP 29895 (swing low)". Fall back to the legacy key_levels
            # heuristic when no reason was passed.
            stop_note = ""
            target_note = ""
            if stop_reason:
                stop_note = f" ({stop_reason})"
            elif key_levels:
                if "swing_low" in key_levels and side_word == "LONG":
                    stop_note = " (swing low)"
                elif "swing_high" in key_levels and side_word == "SHORT":
                    stop_note = " (swing high)"
            if target_reason:
                target_note = f" ({target_reason})"
            elif key_levels:
                if "prev_high" in key_levels and side_word == "LONG":
                    target_note = " (prev high)"
                elif "prev_low" in key_levels and side_word == "SHORT":
                    target_note = " (prev low)"

            def _label(y, text, color):
                ax.annotate(
                    text, xy=(x_max, y), xytext=(4, 0), textcoords="offset points",
                    va="center", ha="left", fontsize=8.5, fontweight="bold",
                    color="white",
                    bbox=dict(boxstyle="round,pad=0.28", fc=color, ec="none"),
                    zorder=7,
                )

            _label(entry, f"ENTRY {entry:.2f}", _ENTRY_C)
            _label(stop, f"STOP {stop:.2f}{stop_note}", _STOP_C)
            _label(target, f"TARGET {target:.2f}{target_note}", _TARGET_C)

            # ── 7. R:R + direction badge (top-left, inside axes). ──
            badge_color = _TARGET_C if side_word == "LONG" else _STOP_C
            ax.text(
                0.012, 0.97, f"{side_word}   R:R 1:{rr:g}",
                transform=ax.transAxes, va="top", ha="left",
                fontsize=11, fontweight="bold", color="white",
                bbox=dict(boxstyle="round,pad=0.4", fc=badge_color, ec="none"),
                zorder=8,
            )

            # ── 8. Axes cosmetics. ──
            ax.set_xlim(x_min, x_max)
            # Pad y so labels at extreme levels aren't clipped.
            all_y = [entry, stop, target]
            if df is not None and len(df) > 0:
                all_y += [df["low"].astype(float).min(), df["high"].astype(float).max()]
            if key_levels:
                for v in key_levels.values():
                    if isinstance(v, (tuple, list)) and len(v) == 2:
                        all_y += [float(v[0]), float(v[1])]
                    else:
                        try:
                            all_y.append(float(v))
                        except (TypeError, ValueError):
                            pass
            y_lo, y_hi = min(all_y), max(all_y)
            pad = (y_hi - y_lo) * 0.08 or 1.0
            ax.set_ylim(y_lo - pad, y_hi + pad)

            ax.set_title(f"{symbol} · {timeframe} · {side_word}", fontsize=12,
                         fontweight="bold", color="#0f172a", loc="left")
            ax.grid(True, color="#e2e8f0", linewidth=0.6, zorder=0)
            ax.set_axisbelow(True)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            ax.tick_params(labelsize=8, colors="#475569")
            if x_is_time:
                try:
                    if fire_time is not None:
                        # Futures signal chart: ticks in Eastern wall-clock with
                        # an explicit 'ET' suffix (the old bare %H:%M printed the
                        # raw UTC index and read ~4-5h in the past).
                        from matplotlib.ticker import FuncFormatter
                        ax.xaxis.set_major_formatter(FuncFormatter(
                            lambda x, _pos: format_et_label(mdates.num2date(x))))
                        fig.autofmt_xdate(rotation=30, ha="right")
                    else:
                        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                        fig.autofmt_xdate(rotation=0, ha="center")
                except Exception:
                    pass
            else:
                ax.set_xticks([])

            # ── 9. Serialise to PNG bytes. ──
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
            buf.seek(0)
            png = buf.getvalue()
            logger.info(
                f"[trade-chart] rendered sym={symbol} tf={timeframe} dir={side_word} "
                f"rr=1:{rr:g} bytes={len(png)} "
                f"fire={format_et_label(fire_time) if fire_time is not None else '-'} "
                f"stop_reason={stop_reason or '-'} "
                f"target_reason={target_reason or '-'}"
            )
            return png
        except Exception as e:
            logger.warning(f"[trade-chart] render failed sym={symbol}: {type(e).__name__}: {e}")
            return None
        finally:
            plt.close(fig)
