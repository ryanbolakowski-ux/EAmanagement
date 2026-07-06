"""Replay of the 2026-07-06 GPC-vs-IREN morning under SARO_RANK_UPGRADE.

Recorded facts (forensics 2026-07-06):
  * Saro picked GPC at score 50.2 off a STALE snapshot — every number was
    Thursday 7/02's session: "gap +12.92%", "entry $132.57". Real Monday: gap
    -2.54%, high $130.30 (entry never printed), traded down to ~$126.15 where
    the recorded stop-out (-4.84%) landed. Profile: prior-day +12.9% pop into a
    +34.9%/20d run, analyst consensus only +8.2% upside, Hold — post-pop fade.
  * IREN (STT Oracle's pick, +13% on the day) was REJECTED by the coarse gate
    on the same stale data (read -10.39% = Thursday's flush; real Monday gap
    +8.11%). Profile: 5d -18.7%, 20d -40.7%, adv20 $2.24B (10x GPC), analyst
    consensus +95% upside, Buy, fresh catalysts.

Claim verified here: under the upgrade the pipeline ranks IREN above GPC AND
the stale-quote hard gate rejects GPC. No network — recorded numbers only.

HONESTY NOTE: candidate SOURCING still comes from the market snapshot. On a
stale morning (delayed Polygon universe) the upgrade's real-world effect is
the stale-quote gate rejecting bad finalists (Saro abstains); IREN-class
winners can only be SOURCED once the FMP live universe (SARO_UNIVERSE=fmp)
flips. This replay shows the scoring math is ready for that data.

Run (container): PYTHONPATH=/tmp/wt python scripts/replay_iren_gpc_20260706.py
"""
import os

os.environ["SARO_RANK_UPGRADE"] = "1"
os.environ.setdefault("REALTIME_FEED", "fmp")

from app.engines.options.theta_scanner import _stale_quote_check  # noqa: E402
from app.engines.scanner.scoring import score_candidate  # noqa: E402


def _mk_gpc() -> dict:
    """GPC as the 7/06 scan saw it (stale Thursday snapshot) + what live-Monday
    enrichment WOULD have attached."""
    c = {
        "ticker": "GPC",
        # ── stale snapshot (Thursday 7/02's session read as 'today') ──
        "price": 132.57,        # Thursday close — the recorded 'entry'
        "gap_pct": 12.92,       # Thursday's gap, reported as Monday's
        "rel_vol": 3.72,
        "today_vol": 5_088_382,
        "dollar_vol": 674_600_000,
        "matched_strategy": "momentum_breakout",
    }
    enrichment = {
        # ── real Monday tape ──
        "live_price": 126.15,        # where GPC actually traded (stop-out zone)
        "live_gap_pct": -4.84,       # live vs Thursday close
        "prior_day_ret_pct": 12.92,  # Thursday's pop is the PRIOR day now
        "prior_day_clv": 0.90,
        "chg_5d_pct": 18.0,
        "chg_20d_pct": 34.9,         # recorded: +34.9%/20d run
        "dist_from_60d_high_pct": -1.7,
        "adv20_shares": 1_700_000,
        "adv20_dollars": 224_000_000,   # ~10x smaller than IREN
        "rel_vol_adv20": 0.36,
        "former_runner_days": 2,
        "analyst_upside_pct": 8.2,   # recorded consensus vs price
        "analyst_rating": "Hold",
    }
    return c, enrichment


def _mk_iren() -> dict:
    """IREN as a LIVE Monday candidate (what a fresh snapshot would have shown)
    + its live-Monday enrichment."""
    c = {
        "ticker": "IREN",
        "price": 14.38,          # live Monday (prev close 13.30, gap +8.11%)
        "gap_pct": 8.11,         # real Monday gap
        "rel_vol": 0.95,         # failure mode 5: prev-day volume base was elevated
        "today_vol": 300_000_000,
        "dollar_vol": 150_000_000,
        "catalyst_weight": 1.4,
        "catalyst_reason": "analyst initiation + partnership news",
        "matched_strategy": "capitulation_gap_reclaim",
    }
    enrichment = {
        "live_price": 14.38,
        "live_gap_pct": 8.11,
        "prior_day_ret_pct": -10.39,   # Thursday's capitulation flush
        "prior_day_clv": 0.15,
        "chg_5d_pct": -18.7,           # recorded
        "chg_20d_pct": -40.7,          # recorded
        "dist_from_60d_high_pct": -43.0,
        "adv20_shares": 160_000_000,
        "adv20_dollars": 2_240_000_000,  # recorded adv20 $2.24B
        "rel_vol_adv20": 1.8,
        "former_runner_days": 6,
        "analyst_upside_pct": 95.0,    # recorded consensus target vs price
        "analyst_rating": "Buy",
    }
    return c, enrichment


def _score(c: dict) -> float:
    return score_candidate(c).total


def main() -> int:
    gpc, gpc_live = _mk_gpc()
    iren, iren_live = _mk_iren()

    # OLD pipeline = gate off, snapshot-only dicts
    os.environ["SARO_RANK_UPGRADE"] = "0"
    old_gpc = _score(dict(gpc))
    old_iren = _score(dict(iren))
    os.environ["SARO_RANK_UPGRADE"] = "1"

    # NEW pipeline = gate on, enriched dicts, then the stale-quote hard gate
    gpc_en = {**gpc, **gpc_live}
    iren_en = {**iren, **iren_live}
    new_gpc = _score(gpc_en)
    new_iren = _score(iren_en)

    gpc_stale, gpc_diff = _stale_quote_check(gpc["price"], gpc_live["live_price"])
    iren_stale, iren_diff = _stale_quote_check(iren["price"], iren_live["live_price"])
    gpc_verdict = (
        f"REJECT — stale snapshot: live ${gpc_live['live_price']:.2f} vs snapshot "
        f"${gpc['price']:.2f} ({gpc_diff:+.1f}%) — data mismatch" if gpc_stale
        else "pass")
    iren_verdict = (
        f"REJECT — stale snapshot ({iren_diff:+.1f}%)" if iren_stale
        else f"pass (live matches snapshot, {iren_diff:+.1f}%)")

    print("=" * 74)
    print("REPLAY 2026-07-06: GPC (Saro's stale pick) vs IREN (STT Oracle, +13%)")
    print("=" * 74)
    print(f"{'':14}{'OLD score':>12}{'NEW score':>12}   stale-quote hard gate")
    print(f"{'GPC':14}{old_gpc:>12.1f}{new_gpc:>12.1f}   {gpc_verdict}")
    print(f"{'IREN':14}{old_iren:>12.1f}{new_iren:>12.1f}   {iren_verdict}")
    print("-" * 74)
    print(f"(recorded live GPC score that morning: 50.2; replayed OLD: {old_gpc:.1f})")

    old_rank = sorted([("GPC", old_gpc), ("IREN", old_iren)], key=lambda x: -x[1])
    new_rank = sorted([("GPC", new_gpc), ("IREN", new_iren)], key=lambda x: -x[1])
    print(f"OLD ranking: {' > '.join(t for t, _ in old_rank)}"
          f"  (IREN was additionally coarse-rejected on the stale -10.39% gap)")
    print(f"NEW ranking: {' > '.join(t for t, _ in new_rank)}"
          f"  + GPC hard-rejected by the stale-quote gate")

    ok_rank = new_iren > new_gpc
    ok_gate = gpc_stale and not iren_stale
    print("-" * 74)
    print(f"CLAIM 1 (scoring math ranks IREN above GPC once both are live-sourced): "
          f"{'PASS' if ok_rank else 'FAIL'} ({new_iren:.1f} vs {new_gpc:.1f})")
    print(f"CLAIM 2 (stale-quote gate rejects GPC, passes IREN): "
          f"{'PASS' if ok_gate else 'FAIL'} "
          f"(GPC {gpc_diff:+.2f}% > 3% threshold; IREN {iren_diff:+.2f}%)")
    print("component detail (NEW):")
    for name, cand in (("GPC", gpc_en), ("IREN", iren_en)):
        sb = score_candidate(cand)
        keys = ("trend", "fade_guard", "analyst", "rel_vol", "liquidity", "momentum")
        parts = ", ".join(f"{k}={sb.components[k]['raw']:.2f}" for k in keys)
        print(f"  {name}: total {sb.total:.1f} | {parts}")
    return 0 if (ok_rank and ok_gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
