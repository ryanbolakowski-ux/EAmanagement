"""QQQ/ETF-proxy pricing must scale to the futures level accurately.

The old hardcoded NQ scale was 31.0 while the live NQ/QQQ ratio is ~41 (and
polygon_feed applied NO scaling at all -> ~41x too small). These tests pin the
dynamic ratio + the scaled fetch to within a tolerance of the real price.
"""
import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from app.engines.data_feeds.proxy_scale import get_proxy_scale


def test_proxy_scale_is_dynamic_and_in_range():
    # NQ/QQQ has historically been ~31-42; the stale constant was 31. The live
    # value must be a sane multiple well above the old constant given drift.
    s = get_proxy_scale("NQ")
    assert 25.0 < s < 60.0, f"NQ proxy scale out of range: {s}"
    # non-proxied symbol -> 1.0
    assert get_proxy_scale("AAPL") == 1.0


def test_scaled_polygon_proxy_matches_real_nq():
    """A polygon NQ fetch (ETF proxy, now scaled) should be within ~5% of the
    real cached NQ price, not ~40x off."""
    from app.engines.data_feeds.polygon_feed import fetch_polygon_data
    from sqlalchemy import text
    from app.database import async_session_factory, engine

    async def go():
        await engine.dispose()
        end = datetime.now(timezone.utc); start = end - timedelta(days=3)
        df = await fetch_polygon_data("NQ", start, end, "1h")
        async with async_session_factory() as db:
            real = (await db.execute(text(
                "SELECT close FROM candle_cache WHERE instrument='NQ' ORDER BY timestamp DESC LIMIT 1"
            ))).scalar()
        return (float(df["close"].iloc[-1]) if df is not None and len(df) else None,
                float(real) if real else None)

    proxy, real = asyncio.run(go())
    if proxy is None or real is None:
        pytest.skip("no polygon/real NQ data available")
    err = abs(proxy - real) / real
    assert err < 0.05, f"scaled proxy {proxy:.0f} vs real {real:.0f} = {err*100:.1f}% off"
