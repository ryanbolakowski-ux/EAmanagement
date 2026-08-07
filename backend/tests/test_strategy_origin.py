"""STRATEGY-VISIBILITY-V1 tests — strategies.origin provenance.

Covers: (1) the pure backfill heuristic (canonical name AND table-wide
same-second batch >= 3) on the exact prod patterns that disproved the
simpler heuristics — organic canonical-named solitaires (bcf150's ACTIVE
"Judas Swing", jaceford's DRAFT "Reversal Swing"), cross-user
identical-timestamp scanner backfills, and the non-canonical "{name} V2"
same-second batch; (2) the signup seed gate is default-OFF at source
level; (3) the end-to-end backfill script against a throwaway Postgres
including the lazy ALTER (origin column dropped first to mirror today's
prod schema), dry-run writes nothing, updated_at is untouched, and a
rerun is a no-op; (4) GET /strategies/ hides seeded rows — non-admins
always, admins unless ?include_seeded=1; (5) by-id access (GET
/strategies/{id} and the watcher-style select) still resolves hidden
rows; (6) POST /strategies/ stamps origin='user'; (7) GET
/backtests/ranking hides seeded rows for non-admins but not admins.

The DB-backed suite is DESTRUCTIVE (drop_all/create_all/DROP COLUMN) and
only runs when ORIGIN_TEST_ALLOW_DDL=1 — the runner points DATABASE_URL
at a THROWAWAY Postgres container. Never set that env against prod.

Runs under pytest OR directly:  python tests/test_strategy_origin.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

DDL_OK = os.environ.get("ORIGIN_TEST_ALLOW_DDL") == "1"


# ── (1) pure heuristic ─────────────────────────────────────────────────────
def test_heuristic_classification():
    from scripts.backfill_strategy_origin import (
        CANONICAL_SEEDED_NAMES, MIN_BATCH, SCANNER_SEEDED_NAMES, classify_seeded)
    from app.scripts.seed_strategies import SEED

    assert MIN_BATCH == 3
    assert len(CANONICAL_SEEDED_NAMES) == len(SEED) + len(SCANNER_SEEDED_NAMES), \
        "canonical set must be the seed library + the 7 scanner names, no dupes"

    rows: list[tuple[str, datetime]] = []
    # signup seed batch: 17 canonical rows within ~200us of one second
    t_signup = datetime(2026, 5, 8, 3, 27, 5)
    rows += [(t["name"], t_signup + timedelta(microseconds=12 * i))
             for i, t in enumerate(SEED)]
    n_batch = len(rows)
    # cross-user scanner backfill: IDENTICAL microsecond timestamp on 3 users
    t_scan = datetime(2026, 5, 13, 1, 38, 23, 519186)
    rows += [("Momentum Gappers", t_scan)] * 3
    n_batch += 3
    # organic canonical-named solitaires (the rows that disprove name-only)
    rows += [("Judas Swing", datetime(2026, 8, 4, 21, 31, 7)),      # bcf150 ACTIVE
             ("Reversal Swing", datetime(2026, 5, 16, 14, 2, 9))]   # jaceford DRAFT
    # owner "{name} V2" track: 13-row same-second batch, non-canonical names
    t_v2 = datetime(2026, 7, 4, 3, 21, 14)
    rows += [(f"Strategy {i} V2", t_v2 + timedelta(microseconds=i)) for i in range(13)]

    got = classify_seeded(rows)
    assert all(got[:n_batch]), "signup + cross-user scanner batches must classify seeded"
    assert not any(got[n_batch:]), \
        "organic solitaires and the non-canonical V2 batch must stay user-visible"

    # a 2-row same-second canonical batch stays visible (< MIN_BATCH):
    # a solitary organic create can never be hidden, and even a freak
    # two-user same-second collision errs toward visible.
    t2 = datetime(2026, 6, 1, 10, 0, 0)
    assert classify_seeded([("Judas Swing", t2),
                            ("Judas Swing", t2 + timedelta(microseconds=5))]) == [False, False]


# ── (2) signup seeding gate is default-OFF ─────────────────────────────────
def test_signup_seed_gate_default_off():
    path = os.path.join(_BACKEND, "app", "api", "routes", "auth.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "SEED_STRATEGIES_ON_SIGNUP" in src, "env gate missing from register()"
    assert '"SEED_STRATEGIES_ON_SIGNUP", "0"' in src, "gate must default OFF"
    assert src.index("SEED_STRATEGIES_ON_SIGNUP") < src.index("seed_user_strategies"), \
        "the seed call must sit inside (after) the env gate"
    # the seed library itself must stamp provenance on insert
    with open(os.path.join(_BACKEND, "app", "scripts", "seed_strategies.py"),
              encoding="utf-8") as f:
        seed_src = f.read()
    assert 'origin="seeded"' in seed_src, "seed_user_strategies must set origin='seeded'"


# ── (3)-(7) DB-backed suite (throwaway Postgres only) ──────────────────────
async def _integration():
    from sqlalchemy import select, text
    # Route imports also pull the model modules so mappers configure fully.
    from app.api.routes.strategies import (
        StrategyCreate, create_strategy, ensure_origin_column, get_strategy,
        list_strategies)
    from app.api.routes.backtests import get_strategy_ranking
    from app.database import Base, async_session_factory, engine
    from app.models import (BacktestMetrics, BacktestRun, Strategy, User)  # noqa: F401
    from app.models.backtest import BacktestStatus
    from app.models.strategy import StrategyStatus
    from app.scripts.seed_strategies import SEED
    from scripts.backfill_strategy_origin import run as backfill_run

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    ensure_origin_column._done = False  # fresh process semantics

    def _mk_user(email, username, is_admin=False):
        return User(email=email, username=username, hashed_password="!test!",
                    is_admin=is_admin, subscription_tier="tier_5")

    def _mk_strat(user_id, name, created_at, rule_tree=None):
        return Strategy(user_id=user_id, name=name, status=StrategyStatus.ACTIVE,
                        instruments=["ES"], rule_tree=rule_tree if rule_tree is not None else {},
                        created_at=created_at)

    t_seed = datetime(2026, 5, 8, 3, 27, 5)
    t_scan = datetime(2026, 5, 13, 1, 38, 23, 519186)

    async with async_session_factory() as db:
        alice = _mk_user("alice@origin.test", "alice_origin")
        bob = _mk_user("bob@origin.test", "bob_origin")
        admin = _mk_user("admin@origin.test", "admin_origin", is_admin=True)
        db.add_all([alice, bob, admin])
        await db.flush()

        # alice: full 17-row signup batch + 2 organic solitaires (one with a
        # canonical name and empty rule_tree — the exact bcf150 pattern)
        for i, tpl in enumerate(SEED):
            db.add(_mk_strat(alice.id, tpl["name"], t_seed + timedelta(microseconds=12 * i)))
        db.add(_mk_strat(alice.id, "Judas Swing", datetime(2026, 8, 4, 21, 31, 7),
                         rule_tree={"nodes": ["organic"]}))
        db.add(_mk_strat(alice.id, "My Custom Momo", datetime(2026, 8, 5, 9, 12, 0)))
        # cross-user scanner backfill: identical timestamp on 3 users
        for u in (alice, bob, admin):
            db.add(_mk_strat(u.id, "Momentum Gappers", t_scan))
        # admin organic row
        db.add(_mk_strat(admin.id, "Admin Scratch", datetime(2026, 8, 1, 12, 0, 0)))
        await db.commit()
        alice_id, bob_id, admin_id = alice.id, bob.id, admin.id

    # NOW mirror TODAY'S prod schema: drop the origin column so the existing
    # rows predate it, exactly like the 242 prod rows do. (The rows had to be
    # inserted first — the ORM mapping includes origin in INSERT statements,
    # which is also why every origin-writing route calls ensure_origin_column
    # before inserting.)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE strategies DROP COLUMN IF EXISTS origin"))

    # snapshot updated_at BEFORE the backfill — it must not move
    async with async_session_factory() as db:
        pre = {str(r.id): r.updated_at for r in (await db.execute(
            text("SELECT id, updated_at FROM strategies"))).fetchall()}

    # dry-run: SELECT-only — must not even create the column
    rc = await backfill_run(dry_run=True)
    assert rc == 0
    async with async_session_factory() as db:
        col = (await db.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='strategies' AND column_name='origin'"))).first()
        assert col is None, "--dry-run must not ALTER"
    print("PASS  backfill dry-run is SELECT-only (no ALTER, no UPDATE)")

    # real run: lazy ALTER + heuristic UPDATE
    rc = await backfill_run(dry_run=False)
    assert rc == 0

    async def _counts(db, uid):
        r = (await db.execute(text(
            "SELECT COUNT(*) FILTER (WHERE origin = 'seeded') AS hidden, "
            "       COUNT(*) FILTER (WHERE origin IS DISTINCT FROM 'seeded') AS visible "
            "  FROM strategies WHERE user_id = :u"), {"u": str(uid)})).first()
        return int(r.hidden), int(r.visible)

    async with async_session_factory() as db:
        assert await _counts(db, alice_id) == (18, 2), \
            "alice: 17 signup + 1 scanner hidden; organic Judas Swing + My Custom Momo visible"
        assert await _counts(db, bob_id) == (1, 0)
        assert await _counts(db, admin_id) == (1, 1)
        organic = {r.name for r in (await db.execute(text(
            "SELECT name FROM strategies WHERE user_id=:u AND origin IS NULL"),
            {"u": str(alice_id)})).fetchall()}
        assert organic == {"Judas Swing", "My Custom Momo"}
        post = {str(r.id): r.updated_at for r in (await db.execute(
            text("SELECT id, updated_at FROM strategies"))).fetchall()}
        assert post == pre, "backfill must not churn updated_at (raw SQL, origin only)"
    print("PASS  backfill heuristic on throwaway DB (18/2, 1/0, 1/1; updated_at untouched)")

    # rerun: idempotent no-op
    rc = await backfill_run(dry_run=False)
    assert rc == 0
    async with async_session_factory() as db:
        assert await _counts(db, alice_id) == (18, 2)
        post2 = {str(r.id): r.updated_at for r in (await db.execute(
            text("SELECT id, updated_at FROM strategies"))).fetchall()}
        assert post2 == pre
    print("PASS  backfill rerun is a no-op")

    # (4) list filter — direct route invocation (Depends bypassed)
    async with async_session_factory() as db:
        alice_db = (await db.execute(select(User).where(User.id == alice_id))).scalar_one()
        admin_db = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()

        names = {s.name for s in await list_strategies(
            include_seeded=False, current_user=alice_db, db=db)}
        assert names == {"Judas Swing", "My Custom Momo"}, f"non-admin sees only self-created: {names}"

        names2 = {s.name for s in await list_strategies(
            include_seeded=True, current_user=alice_db, db=db)}
        assert names2 == names, "include_seeded must be ignored for non-admins"

        names3 = {s.name for s in await list_strategies(
            include_seeded=False, current_user=admin_db, db=db)}
        assert names3 == {"Admin Scratch"}, "admin default view follows the same rule"

        names4 = {s.name for s in await list_strategies(
            include_seeded=True, current_user=admin_db, db=db)}
        assert names4 == {"Admin Scratch", "Momentum Gappers"}, \
            "admin + include_seeded=1 is the support escape hatch"
        print("PASS  GET /strategies/ hides seeded rows (admin escape hatch works)")

        # (5) by-id unaffected — route + watcher-style select on a hidden row
        seeded_id = (await db.execute(text(
            "SELECT id FROM strategies WHERE user_id=:u AND origin='seeded' "
            "AND name='ICT Silver Bullet'"), {"u": str(alice_id)})).scalar_one()
        got = await get_strategy(strategy_id=str(seeded_id), current_user=alice_db, db=db)
        assert got.id == str(seeded_id) and got.name == "ICT Silver Bullet"
        watcher_style = (await db.execute(
            select(Strategy).where(Strategy.id == seeded_id))).scalar_one()
        assert watcher_style.name == "ICT Silver Bullet"
        print("PASS  by-id access still resolves hidden rows (route + watcher select)")

        # (6) create stamps origin='user' and the row is immediately listed
        created = await create_strategy(
            data=StrategyCreate(name="Fresh Momentum Idea"),
            current_user=alice_db, db=db)
        origin_val = (await db.execute(text(
            "SELECT origin FROM strategies WHERE id = CAST(:i AS uuid)"),
            {"i": created.id})).scalar_one()
        assert origin_val == "user"
        names5 = {s.name for s in await list_strategies(
            include_seeded=False, current_user=alice_db, db=db)}
        assert "Fresh Momentum Idea" in names5
        await db.commit()
        print("PASS  POST /strategies/ stamps origin='user' and lists immediately")

        # (7) ranking hides seeded rows for non-admins, not for admins
        # NB: alice deliberately has TWO "Judas Swing" rows (seeded + organic,
        # the bcf150 situation) — key on origin, never on name alone.
        organic_sid = (await db.execute(text(
            "SELECT id FROM strategies WHERE user_id=:u AND name='Judas Swing' "
            "AND origin IS NULL"), {"u": str(alice_id)})).scalar_one()
        admin_seeded_sid = (await db.execute(text(
            "SELECT id FROM strategies WHERE user_id=:u AND name='Momentum Gappers'"),
            {"u": str(admin_id)})).scalar_one()
        t0 = datetime(2026, 7, 1, 9, 30, 0)
        for uid, sid in ((alice_id, seeded_id), (alice_id, organic_sid),
                         (admin_id, admin_seeded_sid)):
            br = BacktestRun(strategy_id=sid, user_id=uid, instrument="ES",
                             start_date=t0, end_date=t0 + timedelta(days=30),
                             timeframe="5m", status=BacktestStatus.COMPLETED,
                             completed_at=t0 + timedelta(days=31))
            db.add(br)
            await db.flush()
            db.add(BacktestMetrics(backtest_run_id=br.id, total_trades=40,
                                   win_rate=0.6, profit_factor=1.8,
                                   max_drawdown_pct=5.0, net_profit=1000.0))
        await db.commit()

        rank = await get_strategy_ranking(sort_by="profit_factor",
                                          current_user=alice_db, db=db)
        ids = {r["strategy_id"] for r in rank}
        assert str(organic_sid) in ids, "organic strategy must stay ranked"
        assert str(seeded_id) not in ids, "seeded strategy must vanish from non-admin ranking"

        rank_admin = await get_strategy_ranking(sort_by="profit_factor",
                                                current_user=admin_db, db=db)
        admin_ids = {r["strategy_id"] for r in rank_admin}
        assert str(admin_seeded_sid) in admin_ids, "admin ranking is unfiltered (support view)"
        print("PASS  GET /backtests/ranking filters seeded rows for non-admins only")

    await engine.dispose()


def test_integration_suite():
    if not DDL_OK:
        try:
            import pytest
            pytest.skip("destructive DB suite: set ORIGIN_TEST_ALLOW_DDL=1 "
                        "with DATABASE_URL pointing at a THROWAWAY Postgres")
        except ImportError:
            print("SKIP  integration suite (ORIGIN_TEST_ALLOW_DDL != 1)")
            return
    asyncio.run(_integration())


def main() -> int:
    tests = [
        ("heuristic_classification", test_heuristic_classification),
        ("signup_seed_gate_default_off", test_signup_seed_gate_default_off),
        ("integration_suite", test_integration_suite),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
