"""MOO LATE CUTOFF + blackout-deferral visibility (2026-07-14 AMDL incident).

A queued stock entry that has not fired by 10:30 ET must be CANCELLED
(redis key cleared, EXPIRED log, one 'Saro: entry skipped' email per
user/day) — never fired late. The blackout-deferral log line is debounced
to once per 5 minutes.

Run: pytest backend/tests/test_entry_expiry.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _pending(**overrides):
    p = {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "user_email": "test@example.com",
        "broker_account_id": "00000000-0000-0000-0000-0000000000aa",
        "ticker": "AMDL",
        "direction": "long",
        "qty": 10,
        "pick_price": 5.00,
        "target": 5.50,
        "pick_date": "2026-07-14",
        "queued_at_et": "09:34",
    }
    p.update(overrides)
    return p


# ── 1. Pure cutoff helper ───────────────────────────────────────────────────

def test_cutoff_queued_934_now_1031_expires():
    from app.engines.options import premarket_scheduler as ps
    assert ps._pending_entry_expired(10 * 60 + 31, queued_et_min=9 * 60 + 34) is True


def test_cutoff_now_1015_does_not_expire():
    from app.engines.options import premarket_scheduler as ps
    assert ps._pending_entry_expired(10 * 60 + 15, queued_et_min=9 * 60 + 34) is False


def test_cutoff_boundary_1030_exact_does_not_expire():
    # 10:30 sharp is still inside the window; expiry is strictly AFTER 10:30.
    from app.engines.options import premarket_scheduler as ps
    assert ps._pending_entry_expired(10 * 60 + 30) is False


def test_cutoff_way_late_expires():
    from app.engines.options import premarket_scheduler as ps
    assert ps._pending_entry_expired(11 * 60 + 20) is True  # today's 11:20 manual delete time


# ── 2. Expiry flow: clears redis key, logs, emails ─────────────────────────

def test_expiry_clears_key_and_emails(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    import app.services.email as email_mod

    cleared, sent = [], []

    async def fake_clear(pick_date, user_id):
        cleared.append((pick_date, user_id))

    async def fake_latch(pick_date, user_id):
        return True

    monkeypatch.setattr(ps, "_clear_pending_entry", fake_clear)
    monkeypatch.setattr(ps, "_acquire_entry_skip_email_latch", fake_latch)
    monkeypatch.setattr(email_mod, "_send_tracked",
                        lambda to, subject, html: sent.append((to, subject, html)))

    now_et = datetime(2026, 7, 14, 10, 31, tzinfo=ET)
    expired = _run(ps._maybe_expire_pending_entry(_pending(), now_et))

    assert expired is True
    assert cleared == [("2026-07-14", "00000000-0000-0000-0000-000000000001")]
    assert len(sent) == 1
    to, subject, html = sent[0]
    assert to == "test@example.com"
    assert "Saro" in subject          # EMAIL_KILL_SWITCH whitelist requirement
    assert "entry skipped" in subject
    assert "AMDL" in html
    assert "09:34" in html and "10:31" in html  # honest queued/now times


def test_no_expiry_at_1015_no_side_effects(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    import app.services.email as email_mod

    cleared, sent = [], []

    async def fake_clear(pick_date, user_id):
        cleared.append((pick_date, user_id))

    monkeypatch.setattr(ps, "_clear_pending_entry", fake_clear)
    monkeypatch.setattr(email_mod, "_send_tracked",
                        lambda to, subject, html: sent.append(to))

    now_et = datetime(2026, 7, 14, 10, 15, tzinfo=ET)
    expired = _run(ps._maybe_expire_pending_entry(_pending(), now_et))

    assert expired is False
    assert cleared == [] and sent == []


def test_expiry_email_latched_once_per_user_day(monkeypatch):
    """Second expiry pass for the same (user, day) must NOT email again."""
    from app.engines.options import premarket_scheduler as ps
    import app.services.email as email_mod

    latch_state = {"granted": 0}

    async def fake_clear(pick_date, user_id):
        pass

    async def fake_latch(pick_date, user_id):
        # SETNX semantics: first acquire wins, later ones don't.
        latch_state["granted"] += 1
        return latch_state["granted"] == 1

    sent = []
    monkeypatch.setattr(ps, "_clear_pending_entry", fake_clear)
    monkeypatch.setattr(ps, "_acquire_entry_skip_email_latch", fake_latch)
    monkeypatch.setattr(email_mod, "_send_tracked",
                        lambda to, subject, html: sent.append(to))

    now_et = datetime(2026, 7, 14, 10, 40, tzinfo=ET)
    assert _run(ps._maybe_expire_pending_entry(_pending(), now_et)) is True
    assert _run(ps._maybe_expire_pending_entry(_pending(), now_et)) is True
    assert len(sent) == 1


def test_expiry_email_skipped_for_bad_address(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    import app.services.email as email_mod

    async def fake_clear(pick_date, user_id):
        pass

    async def fake_latch(pick_date, user_id):
        raise AssertionError("latch must not be touched for a bad address")

    sent = []
    monkeypatch.setattr(ps, "_clear_pending_entry", fake_clear)
    monkeypatch.setattr(ps, "_acquire_entry_skip_email_latch", fake_latch)
    monkeypatch.setattr(email_mod, "_send_tracked",
                        lambda to, subject, html: sent.append(to))

    now_et = datetime(2026, 7, 14, 10, 40, tzinfo=ET)
    assert _run(ps._maybe_expire_pending_entry(_pending(user_email="(unknown)"), now_et)) is True
    assert sent == []


# ── 3. Blackout-deferral log debounce ───────────────────────────────────────

def test_blackout_defer_log_debounce_5min():
    from app.engines.options import premarket_scheduler as ps
    ps._blackout_defer_last_log = float("-inf")
    assert ps._should_log_blackout_deferral(now_monotonic=1000.0) is True
    assert ps._should_log_blackout_deferral(now_monotonic=1001.0) is False
    assert ps._should_log_blackout_deferral(now_monotonic=1299.9) is False
    assert ps._should_log_blackout_deferral(now_monotonic=1300.0) is True   # 300s later
    assert ps._should_log_blackout_deferral(now_monotonic=1301.0) is False


def test_blackout_defer_log_first_call_always_allowed():
    from app.engines.options import premarket_scheduler as ps
    ps._blackout_defer_last_log = float("-inf")
    assert ps._should_log_blackout_deferral(now_monotonic=0.0) is True
