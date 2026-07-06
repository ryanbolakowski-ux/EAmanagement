"""DUP-SEND FIX: the futures-email session cap key must use a SESSION-ANCHORED
ET trading day, not the server's UTC calendar date.

Observed 2026-07-05: the same user received the same NQ short setup at
18:33 ET (22:33 UTC Jul 5) and again at 20:40 ET (00:40 UTC Jul 6). Both are
the SAME Asia session, but the old cap key used the UTC date — which rolled
over at 00:00 UTC (20:00 ET) and minted a fresh one-email-per-session key
mid-session.

Pure/offline. Run: pytest backend/tests/test_email_session_cap_day.py -v
"""
from datetime import datetime, timezone

from app.engines.account_signals.signal_guard import email_session_and_day


def test_asia_session_survives_utc_midnight_rollover():
    """The regression: 18:33 ET and 20:40 ET on the same evening are one ASIA
    session and MUST produce the same (session, day) cap key."""
    first = email_session_and_day(datetime(2026, 7, 5, 22, 33, tzinfo=timezone.utc))
    second = email_session_and_day(datetime(2026, 7, 6, 0, 40, tzinfo=timezone.utc))
    assert first == ("ASIA", "2026-07-05")
    assert second == ("ASIA", "2026-07-05")
    assert first == second  # same cap key -> second email suppressed


def test_asia_after_et_midnight_anchors_to_session_start_date():
    """01:00 ET is still the Asia session that STARTED the previous ET evening."""
    sess, day = email_session_and_day(datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc))
    assert sess == "ASIA"
    assert day == "2026-07-05"


def test_asia_evening_uses_et_date_not_utc():
    # 22:00 UTC Jul 5 = 18:00 ET Jul 5 (EDT): session start boundary.
    sess, day = email_session_and_day(datetime(2026, 7, 5, 22, 0, tzinfo=timezone.utc))
    assert sess == "ASIA"
    assert day == "2026-07-05"


def test_london_ny_and_dead_windows():
    # 03:00 ET -> LONDON, same ET date.
    assert email_session_and_day(
        datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)) == ("LONDON", "2026-07-06")
    # 10:00 ET -> NY_AM.
    assert email_session_and_day(
        datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)) == ("NY_AM", "2026-07-06")
    # 14:00 ET -> NY_PM.
    assert email_session_and_day(
        datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)) == ("NY_PM", "2026-07-06")
    # 12:30 ET -> DEAD zone.
    sess, _ = email_session_and_day(datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc))
    assert sess == "DEAD"
    # 09:15 ET -> between LONDON and NY_AM -> DEAD.
    sess, _ = email_session_and_day(datetime(2026, 7, 6, 13, 15, tzinfo=timezone.utc))
    assert sess == "DEAD"


def test_distinct_sessions_get_distinct_keys_same_day():
    """One email per SESSION per day — different sessions on the same ET day
    must still produce different keys."""
    london = email_session_and_day(datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc))
    ny_am = email_session_and_day(datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc))
    asia = email_session_and_day(datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc))
    assert len({london, ny_am, asia}) == 3
    # And the NEXT day's Asia session is a fresh key.
    asia_next = email_session_and_day(datetime(2026, 7, 7, 23, 0, tzinfo=timezone.utc))
    assert asia_next != asia
