"""
Seed "<name> V2" draft clones of every ACTIVE futures strategy row.

Part of the v2-redesign track: each clone carries the full V1 definition plus
a `rule_tree.v2_variant` annotation pointing back at its source row, lands as
STATUS=DRAFT, and NEVER modifies the V1 rows themselves. Cloning is per ROW
(the book has per-user duplicates of the same definition), keyed by
(user_id, name) for idempotency — re-running after a clean run is a no-op.

SAFETY MODEL (three layers, all required before anything is written):
  1. DRY-RUN by default — prints the plan table and exits.
  2. --execute flag required to write.
  3. Hard guard: refuses --execute unless env STRATEGY_V2_SEED_CONFIRM=YES.

Run in the backend container:
    python -m scripts.seed_strategy_v2                      # plan only (dry-run)
    STRATEGY_V2_SEED_CONFIRM=YES python -m scripts.seed_strategy_v2 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from datetime import datetime
from typing import Optional

from loguru import logger

# Futures symbols the backtest engine can price — single source of truth is
# the runner's tick table; anything outside it is options/stock territory.
from app.engines.backtest_engine.backtest_runner import TICK_SIZES as FUTURES_TICK_SIZES

V2_SUFFIX = " V2"
CONFIRM_ENV = "STRATEGY_V2_SEED_CONFIRM"


# ─────────────────────────────────────────────────────────────────────────────
# Pure planning helpers (no DB, no ORM instantiation — unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def is_futures_strategy(strategy) -> bool:
    """True when at least one configured instrument is a symbol the futures
    backtest engine prices (ES/NQ/... incl. micros)."""
    return any(str(i or "").upper() in FUTURES_TICK_SIZES
               for i in (strategy.instruments or []))


def _is_active(strategy) -> bool:
    """Status may arrive as the StrategyStatus enum (ORM) or a raw string
    (fixtures / raw rows) — compare on the lowercase value either way."""
    st = strategy.status
    return str(getattr(st, "value", st)).lower() == "active"


def build_v2_rule_tree(src) -> dict:
    """Deep-copied rule_tree with the v2_variant annotation. Deep copy so the
    clone can never share (and later mutate) the V1 row's JSON."""
    rt = copy.deepcopy(src.rule_tree or {})
    rt["v2_variant"] = {
        "source_strategy_id": str(src.id),
        "source_name": src.name,
        "seeded_at": datetime.utcnow().isoformat() + "Z",
        "seeded_by": "scripts.seed_strategy_v2",
    }
    return rt


def build_seed_plan(strategies: list) -> list[dict]:
    """Pure planner over already-loaded rows. One plan entry per row:
    action='create' (clone it) or 'skip' (with the reason). Never mutates
    the inputs. Idempotent: rows whose '<name> V2' clone already exists for
    the same user are skipped."""
    existing = {(str(s.user_id), s.name) for s in strategies}
    plan: list[dict] = []
    for s in strategies:
        new_name = s.name + V2_SUFFIX
        if s.name.endswith(V2_SUFFIX):
            action, reason = "skip", "already a V2 clone"
        elif not _is_active(s):
            status = getattr(s.status, "value", s.status)
            action, reason = "skip", f"status={status} (only active rows are cloned)"
        elif not is_futures_strategy(s):
            action, reason = "skip", "no futures instrument (options/stock path)"
        elif (str(s.user_id), new_name) in existing:
            action, reason = "skip", "V2 clone already exists"
        else:
            action, reason = "create", ""
        plan.append({
            "action": action,
            "reason": reason,
            "source_id": str(s.id),
            "source_name": s.name,
            "user_id": str(s.user_id),
            "new_name": new_name if action == "create" else "",
            "instruments": list(s.instruments or []),
        })
    return plan


def clone_strategy_row(src):
    """Build the DRAFT '<name> V2' ORM row from a source row. Copies the full
    trading definition, annotates rule_tree with v2_variant, and touches
    NOTHING on `src`. Import is local so the pure planner path (and the
    --synthetic harness) never needs the model registry."""
    from app.models.strategy import Strategy, StrategyStatus

    desc = (src.description or "").strip()
    v2_note = f"V2 execution variant of '{src.name}' (seeded from {src.id})."
    return Strategy(
        user_id=src.user_id,
        name=src.name + V2_SUFFIX,
        description=f"{v2_note} {desc}".strip(),
        status=StrategyStatus.DRAFT,
        instruments=list(src.instruments or []),
        primary_timeframe=src.primary_timeframe,
        execution_timeframe=src.execution_timeframe,
        higher_timeframes=list(src.higher_timeframes or []),
        risk_reward_ratio=src.risk_reward_ratio,
        stop_loss_type=src.stop_loss_type,
        stop_loss_ticks=src.stop_loss_ticks,
        breakeven_at_r=getattr(src, "breakeven_at_r", None),
        breakeven_mode=getattr(src, "breakeven_mode", None),
        max_contracts=src.max_contracts,
        session_filters=list(src.session_filters or []),
        fvg_min_size_ticks=src.fvg_min_size_ticks,
        fvg_max_size_ticks=src.fvg_max_size_ticks,
        rule_tree=build_v2_rule_tree(src),
        starred=False,
        max_daily_loss=src.max_daily_loss,
        max_trades_per_day=src.max_trades_per_day,
        kill_switch_enabled=bool(getattr(src, "kill_switch_enabled", True)),
        cooldown_min=getattr(src, "cooldown_min", None),
        max_open_positions=getattr(src, "max_open_positions", None),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Seed run (session-injected so tests can pass a mock — no real DB needed)
# ─────────────────────────────────────────────────────────────────────────────

async def seed(session, execute: bool = False) -> list[dict]:
    """Load all strategy rows, build the plan, and (only when `execute` AND
    the confirm env is set) write the clones. Returns the plan either way.
    The env re-check here is deliberate defense-in-depth on top of main()'s
    check — a programmatic caller can't bypass it."""
    if execute and os.environ.get(CONFIRM_ENV) != "YES":
        raise RuntimeError(
            f"refusing to write: --execute requires env {CONFIRM_ENV}=YES "
            f"(dry-run needs neither)")

    from sqlalchemy import select
    import app.models  # noqa: F401 — full mapper registry so relationships resolve
    from app.models.strategy import Strategy

    result = await session.execute(select(Strategy))
    strategies = result.scalars().all()
    plan = build_seed_plan(strategies)
    creates = [p for p in plan if p["action"] == "create"]

    if not execute:
        logger.info(f"[seed-v2] DRY-RUN: {len(creates)} clone(s) would be created "
                    f"({len(plan) - len(creates)} skipped). Nothing written.")
        return plan

    by_id = {str(s.id): s for s in strategies}
    for p in creates:
        session.add(clone_strategy_row(by_id[p["source_id"]]))
    await session.commit()
    logger.info(f"[seed-v2] EXECUTED: created {len(creates)} draft V2 clone(s).")
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_plan(plan: list[dict], executed: bool) -> None:
    title = "SEED PLAN — EXECUTED" if executed else "SEED PLAN — DRY-RUN (nothing written)"
    print(f"\n=== {title} ({len(plan)} rows) ===")
    if not plan:
        print("(no strategy rows found)")
        return
    keys = ["action", "source_name", "new_name", "user_id", "reason"]
    widths = {k: max(len(k), max((len(str(r.get(k, ""))) for r in plan), default=0)) for k in keys}
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in plan:
        print(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))
    creates = sum(1 for p in plan if p["action"] == "create")
    print(f"\n{creates} create / {len(plan) - creates} skip")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Clone active futures strategies to '<name> V2' drafts "
                    "(dry-run by default — see module docstring).")
    ap.add_argument("--execute", action="store_true",
                    help=f"actually write the clones (requires {CONFIRM_ENV}=YES)")
    args = ap.parse_args(argv)

    if args.execute and os.environ.get(CONFIRM_ENV) != "YES":
        print(f"REFUSED: --execute requires {CONFIRM_ENV}=YES in the environment. "
              f"Run without --execute to see the dry-run plan.", file=sys.stderr)
        return 2

    async def _run() -> list[dict]:
        from app.database import async_session_factory
        async with async_session_factory() as session:
            return await seed(session, execute=args.execute)

    plan = asyncio.run(_run())
    _print_plan(plan, executed=args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
