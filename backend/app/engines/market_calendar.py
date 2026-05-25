"""US equity market calendar — holiday detection + open/close status.

Hardcoded NYSE/Nasdaq holiday list covering 2025-2030. No external dep.
Adapted from official NYSE holiday calendar.
"""
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# Full-day closures (US equity markets closed)
HOLIDAYS_FULL = {
    # 2025
    '2025-01-01': 'New Year\u2019s Day',
    '2025-01-09': 'National Day of Mourning (Carter)',
    '2025-01-20': 'Martin Luther King Jr. Day',
    '2025-02-17': 'Presidents\u2019 Day',
    '2025-04-18': 'Good Friday',
    '2025-05-26': 'Memorial Day',
    '2025-06-19': 'Juneteenth',
    '2025-07-04': 'Independence Day',
    '2025-09-01': 'Labor Day',
    '2025-11-27': 'Thanksgiving',
    '2025-12-25': 'Christmas Day',
    # 2026
    '2026-01-01': 'New Year\u2019s Day',
    '2026-01-19': 'Martin Luther King Jr. Day',
    '2026-02-16': 'Presidents\u2019 Day',
    '2026-04-03': 'Good Friday',
    '2026-05-25': 'Memorial Day',
    '2026-06-19': 'Juneteenth',
    '2026-07-03': 'Independence Day (observed)',
    '2026-09-07': 'Labor Day',
    '2026-11-26': 'Thanksgiving',
    '2026-12-25': 'Christmas Day',
    # 2027
    '2027-01-01': 'New Year\u2019s Day',
    '2027-01-18': 'Martin Luther King Jr. Day',
    '2027-02-15': 'Presidents\u2019 Day',
    '2027-03-26': 'Good Friday',
    '2027-05-31': 'Memorial Day',
    '2027-06-18': 'Juneteenth (observed)',
    '2027-07-05': 'Independence Day (observed)',
    '2027-09-06': 'Labor Day',
    '2027-11-25': 'Thanksgiving',
    '2027-12-24': 'Christmas Day (observed)',
}

# Half-day closures (early close at 1pm ET)
HOLIDAYS_HALF = {
    '2025-07-03': 'Day before Independence Day',
    '2025-11-28': 'Day after Thanksgiving',
    '2025-12-24': 'Christmas Eve',
    '2026-11-27': 'Day after Thanksgiving',
    '2026-12-24': 'Christmas Eve',
    '2027-11-26': 'Day after Thanksgiving',
}


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def is_trading_day(d: date) -> bool:
    """True if d is a weekday and not a full-day holiday."""
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in HOLIDAYS_FULL


def holiday_name(d: date) -> str | None:
    iso = d.isoformat()
    if iso in HOLIDAYS_FULL: return HOLIDAYS_FULL[iso]
    if iso in HOLIDAYS_HALF: return HOLIDAYS_HALF[iso]
    return None


def next_trading_day(d: date) -> date:
    """Next trading day strictly after d."""
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def market_status(now: datetime | None = None) -> dict:
    """Snapshot of market state. Times in ET.

    Returns dict with:
      is_open (bool) — true if regular cash session right now (9:30-16:00 ET on a trading day)
      is_trading_day (bool) — true if today is a trading day at all
      is_holiday (bool) — true if today is a full-day market holiday
      is_half_day (bool) — true if today closes early at 1pm ET
      holiday_name (str|None) — name of today's holiday, or None
      session (str) — 'closed' | 'premarket' | 'regular' | 'afterhours'
      next_open_et (str) — ISO timestamp of next 9:30 ET regular session
      now_et (str)
    """
    n = (now or now_et()).astimezone(ET)
    today = n.date()
    full = today.isoformat() in HOLIDAYS_FULL
    half = today.isoformat() in HOLIDAYS_HALF
    trading = is_trading_day(today)
    rth_open = n.replace(hour=9, minute=30, second=0, microsecond=0)
    rth_close = n.replace(hour=(13 if half else 16), minute=(0 if half else 0), second=0, microsecond=0)
    pre_open = n.replace(hour=4, minute=0, second=0, microsecond=0)
    after_close = n.replace(hour=20, minute=0, second=0, microsecond=0)
    session = 'closed'
    is_open = False
    if trading:
        if rth_open <= n < rth_close:
            session = 'regular'; is_open = True
        elif pre_open <= n < rth_open:
            session = 'premarket'
        elif rth_close <= n < after_close:
            session = 'afterhours'
    # Compute next regular open
    if trading and n < rth_open:
        nxt_open = rth_open
    else:
        nxt_day = next_trading_day(today)
        nxt_open = datetime.combine(nxt_day, time(9, 30), tzinfo=ET)
    return {
        'is_open': is_open,
        'is_trading_day': trading,
        'is_holiday': full,
        'is_half_day': half,
        'holiday_name': holiday_name(today),
        'session': session,
        'next_open_et': nxt_open.isoformat(),
        'now_et': n.isoformat(),
    }
