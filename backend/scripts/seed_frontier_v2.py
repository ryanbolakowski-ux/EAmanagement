"""Seed '<Strategy> V2' DRAFT rows on the owner's account from the frontier's
MAX_HONEST_WR configs. DRAFTS only — nothing trades/emits until Ryan activates
each one. Dry-run by default; --execute to write. Run inside the backend image
with the app's env (DATABASE_URL)."""
import asyncio, csv, json, sys

FRONTIER_CSV = "docs/v2/strategy-v2-winrate-frontier.csv"
OWNER_EMAIL = "ryan.bolakowski@icloud.com"

SESSION_MAP = {"NY_AM": ["NY_AM"], "LONDON": ["LONDON"], "NY_PM": ["NY_PM"]}


def parse_params(p: str) -> dict:
    # e.g. "tp=range gate=off be=0.5R fvgmin=6 sess=NY_AM filt=vwap"
    out = {}
    for tok in p.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


async def main(execute: bool):
    rows = []
    with open(FRONTIER_CSV, newline="") as fh:
        for r in csv.DictReader(fh):
            flags = (r.get("flags") or r.get("Flags") or "")
            if "MAX_HONEST_WR" in flags:
                rows.append(r)
    print(f"frontier MAX_HONEST_WR rows: {len(rows)}")

    from sqlalchemy import select, cast, String, text
    import app.models  # noqa
    from app.database import async_session_factory
    from app.models.strategy import Strategy
    from app.models.user import User

    async with async_session_factory() as db:
        owner = (await db.execute(select(User).where(User.email == OWNER_EMAIL))).scalar_one_or_none()
        assert owner, "owner not found"
        created, skipped = [], []
        for r in rows:
            name = r.get("strategy") or r.get("Strategy")
            params = parse_params(r.get("params") or r.get("Params") or "")
            role = r.get("role") or r.get("Role") or ""
            v2_name = f"{name} V2"
            # skip if a V2 row already exists for the owner
            dup = (await db.execute(select(Strategy).where(
                Strategy.user_id == owner.id, Strategy.name == v2_name))).scalars().first()
            if dup:
                skipped.append(v2_name); continue
            # source: the owner's copy of the strategy, else any ACTIVE copy
            src = (await db.execute(select(Strategy).where(
                Strategy.user_id == owner.id, Strategy.name == name,
                cast(Strategy.status, String) == "ACTIVE"))).scalars().first()
            if src is None:
                src = (await db.execute(select(Strategy).where(
                    Strategy.name == name,
                    cast(Strategy.status, String) == "ACTIVE"))).scalars().first()
            if src is None:
                skipped.append(f"{v2_name} (no source row)"); continue

            rt = dict(src.rule_tree or {})
            if params.get("tp"):
                rt["take_profit_mode"] = "range" if params["tp"] == "range" else "auto"
            if params.get("gate") == "off":
                rt["disable_activity_gate"] = True
            if params.get("filt") and params["filt"] != "none":
                rt["use_vwap_filter"] = "vwap" in params["filt"]
                rt["use_rsi_filter"] = "rsi" in params["filt"]
            rt["v2_frontier"] = {
                "seeded": "2026-07-03", "source_role": role or "FRONTIER",
                "params": params, "provenance": "strategy-v2-winrate-frontier (OOS, PF>=2)",
            }

            kw = dict(
                user_id=owner.id, name=v2_name,
                description=(src.description or "") + " — V2 frontier config (structure targets + selectivity); forward-test draft.",
                status="DRAFT",
                instruments=src.instruments, primary_timeframe=src.primary_timeframe,
                execution_timeframe=src.execution_timeframe, higher_timeframes=src.higher_timeframes,
                risk_reward_ratio=src.risk_reward_ratio, stop_loss_type=src.stop_loss_type,
                stop_loss_ticks=src.stop_loss_ticks,
                breakeven_at_r=0.5 if params.get("be", "").startswith("0.5") else src.breakeven_at_r,
                breakeven_mode="r" if params.get("be", "").startswith("0.5") else src.breakeven_mode,
                max_contracts=src.max_contracts,
                session_filters=(SESSION_MAP.get(params.get("sess", ""), src.session_filters)
                                 if params.get("sess") else src.session_filters),
                fvg_min_size_ticks=int(params.get("fvgmin", src.fvg_min_size_ticks or 4)),
                fvg_max_size_ticks=src.fvg_max_size_ticks,
                rule_tree=rt, max_daily_loss=src.max_daily_loss,
                max_trades_per_day=src.max_trades_per_day,
                kill_switch_enabled=src.kill_switch_enabled, cooldown_min=src.cooldown_min,
                max_open_positions=src.max_open_positions,
            )
            created.append((v2_name, params))
            if execute:
                db.add(Strategy(**kw))
        if execute:
            await db.commit()
        print(f"\n{'CREATED' if execute else 'WOULD CREATE'} ({len(created)}):")
        for n, p in created:
            print(f"  {n}: {p}")
        if skipped:
            print(f"SKIPPED ({len(skipped)}): {skipped}")

asyncio.run(main("--execute" in sys.argv))
