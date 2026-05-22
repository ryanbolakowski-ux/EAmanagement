"""Seed strategies — populates every user's account with the canonical
set of 16 strategies (11 futures + 5 options swing). Replaces the old
in-frontend Templates picker. Strategies are upserted by name per user,
so re-running this is idempotent and won't duplicate.

Called automatically:
  - On user registration (auth.register) so new users start with the set
  - From a one-shot CLI run during deploy to backfill existing users
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.strategy import Strategy, StrategyStatus


# Each entry is one canonical strategy. ICT futures setups are configured
# per the user's verified rules: bias 1H/4H, setup 5m/15m, entry 1m.
# Options swing modes carry the user-provided framework: 1-2% per trade,
# 30+ DTE, 30-50 delta, 2x volume on breakouts, 7-day earnings filter.
SEED: list[dict] = [
    # ── Futures (live engine) ──
    {"name": "ICT Silver Bullet",          "description": "Enter during 10-11 AM EST on FVG after displacement. M1 execution with H1 bias.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "5m",  "execution_timeframe": "1m",  "higher_timeframes": ["1H"], "risk_reward_ratio": 3, "session_filters": ["NY_AM"]},
    {"name": "Liquidity Sweep + FVG",      "description": "Wait for sweep of key liquidity, enter on FVG from displacement leg. Classic ICT model.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["1H"], "risk_reward_ratio": 2.5},
    {"name": "SMT Divergence Reversal",    "description": "ES/NQ divergence at key levels, enter on structure shift with FVG confirmation.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["1H"], "risk_reward_ratio": 2},
    {"name": "London Sweep into NY",       "description": "London sweeps Asian range, NY provides continuation/reversal entry on M1 FVG.",
     "instruments": ["ES", "NQ", "YM"], "primary_timeframe": "15m", "execution_timeframe": "1m", "higher_timeframes": ["4H"], "risk_reward_ratio": 3, "session_filters": ["LONDON", "NY_AM"]},
    {"name": "IOFED Precision Entry",      "description": "HTF PD Array tap with H1 bias. 5m structure shift, 1m FVG entry. Highest precision ICT model.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "5m",  "execution_timeframe": "1m",  "higher_timeframes": ["1H"], "risk_reward_ratio": 4},
    {"name": "NY PM Reversal",             "description": "Afternoon 2-3PM EST reversal after morning exhaustion. FVG + OB confluence.",
     "instruments": ["ES", "NQ", "YM"], "primary_timeframe": "5m", "execution_timeframe": "1m", "higher_timeframes": ["1H"], "risk_reward_ratio": 2, "session_filters": ["NY_PM"]},
    {"name": "Reversal Swing",             "description": "1H/4H bias from HTF FVG respect. Price taps untapped 15m FVG, rejects, inverts a 2-3m FVG toward an untapped 4H FVG.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["4H"], "risk_reward_ratio": 3},
    {"name": "AMD Strategy",               "description": "Accumulation → Manipulation → Distribution. Liquidity sweep, displacement, FVG, retrace, order-flow confirmation.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["4H"], "risk_reward_ratio": 3, "session_filters": ["LONDON", "NY_AM"]},
    {"name": "Power of 3 (PO3)",           "description": "Accumulation → Manipulation → Distribution at session level. Sweep one extreme, enter on MSS, target the other.",
     "instruments": ["ES", "NQ", "YM"], "primary_timeframe": "15m", "execution_timeframe": "1m", "higher_timeframes": ["4H"], "risk_reward_ratio": 3},
    {"name": "Judas Swing",                "description": "False move at session open that traps traders before the real direction begins. Enter on MSS + FVG after the sweep.",
     "instruments": ["ES", "NQ", "YM"], "primary_timeframe": "5m", "execution_timeframe": "1m", "higher_timeframes": ["1H"], "risk_reward_ratio": 3, "session_filters": ["LONDON", "NY_AM"]},
    {"name": "ICT 2022 Model (AMD)",       "description": "Asian range accumulation → London manipulation sweep → NY distribution. Enter NY MSS + FVG.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["4H"], "risk_reward_ratio": 3, "session_filters": ["NY_AM"]},
    {"name": "FVG Inversion Tap",
     "description": "Counter-bias FVG tap then inversion entry. Step 1 — Bias on the HTF (1H/4H), same as the other ICT setups. Step 2 — Mark a 15m or 5m FVG on the OPPOSITE side of bias (the draw on liquidity). Step 3 — Wait for price to tap into that 15m/5m FVG, leaving a 1m–3m FVG on the way there. Step 4 — Watch the 1m–3m FVG invert; entry triggers on the CANDLE CLOSURE that inverts it. Stop loss = the reversal-point low (longs) or high (shorts). Take profit = the nearest untapped 1H or 4H FVG in bias direction; if none exists, use previous session highs (longs) or lows (shorts) — London preferred, Asia or previous-day if London/Asia were consolidating.",
     "instruments": ["ES", "NQ"],   "primary_timeframe": "15m", "execution_timeframe": "1m",  "higher_timeframes": ["1H", "4H"], "risk_reward_ratio": 3, "session_filters": ["NY_AM", "LONDON"]},

    # ── Options Swing (engine ships when Tradier connects) ──
    {"name": "Trend Pullback (Options)",       "description": "Buy options on pullbacks inside a strong existing trend. 50/200 EMA filter, RSI confirmation, 30-50 delta calls/puts at 30-60 DTE.",
     "instruments": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"], "primary_timeframe": "4H", "execution_timeframe": "1H", "higher_timeframes": ["1D"], "risk_reward_ratio": 2.5,
     "options_mode": "trend_pullback"},
    {"name": "Breakout (Options)",             "description": "Enter on confirmed 20-day high/low breaks with 2x average volume. Hard stop 1% below the breakout level.",
     "instruments": ["SPY", "QQQ", "NVDA", "TSLA", "AMD"], "primary_timeframe": "4H", "execution_timeframe": "1H", "higher_timeframes": ["1D"], "risk_reward_ratio": 2.5,
     "options_mode": "breakout"},
    {"name": "Vertical Spread (Options)",      "description": "Defined-risk bull call / bear put spreads. Cuts cost and theta drag when IV is elevated.",
     "instruments": ["SPY", "QQQ", "NVDA", "AAPL"], "primary_timeframe": "4H", "execution_timeframe": "1H", "higher_timeframes": ["1D"], "risk_reward_ratio": 1.5,
     "options_mode": "vertical_spread"},
    {"name": "Earnings/Catalyst (Options)",    "description": "Buy ATM straddles 1-3 days before earnings or major catalysts. Tiny size — IV crush is real.",
     "instruments": ["NVDA", "TSLA", "AAPL", "META", "AMZN", "GOOGL"], "primary_timeframe": "4H", "execution_timeframe": "1H", "higher_timeframes": ["1D"], "risk_reward_ratio": 2.0,
     "options_mode": "earnings_catalyst"},
    {"name": "The Wheel (Options)",            "description": "Sell cash-secured puts on stocks you want to own. If assigned, sell covered calls until shares get called away.",
     "instruments": ["SPY", "AAPL", "MSFT", "JPM", "KO"], "primary_timeframe": "1D", "execution_timeframe": "1D", "higher_timeframes": ["1D"], "risk_reward_ratio": 1.0,
     "options_mode": "wheel"},
]


async def seed_user_strategies(db: AsyncSession, user_id) -> int:
    """Insert any missing canonical strategies for the given user. Idempotent —
    skips entries whose name already exists for that user. Returns the number
    of strategies created this run."""
    existing_rows = await db.execute(
        select(Strategy.name).where(Strategy.user_id == user_id)
    )
    existing_names = {r for (r,) in existing_rows.all()}

    created = 0
    for tpl in SEED:
        if tpl["name"] in existing_names:
            continue
        s = Strategy(
            user_id=user_id,
            name=tpl["name"],
            description=tpl.get("description"),
            status=StrategyStatus.ACTIVE,
            instruments=tpl["instruments"],
            primary_timeframe=tpl["primary_timeframe"],
            execution_timeframe=tpl["execution_timeframe"],
            higher_timeframes=tpl["higher_timeframes"],
            risk_reward_ratio=tpl.get("risk_reward_ratio", 2.0),
            stop_loss_type="structure",
            max_contracts=10,
            session_filters=tpl.get("session_filters", []),
            fvg_min_size_ticks=4,
        )
        # Options strategies carry the optional options_mode marker
        if "options_mode" in tpl:
            try:
                s.options_mode = tpl["options_mode"]
            except Exception:
                pass
        db.add(s)
        created += 1

    if created:
        await db.commit()
    return created


async def seed_all_existing_users(db: AsyncSession) -> dict:
    """One-shot: seed every user that's missing strategies. Returns
    {user_id: created_count}."""
    from app.models.user import User
    rows = await db.execute(select(User.id))
    user_ids = [uid for (uid,) in rows.all()]
    out = {}
    for uid in user_ids:
        n = await seed_user_strategies(db, uid)
        if n > 0:
            out[str(uid)] = n
    return out
