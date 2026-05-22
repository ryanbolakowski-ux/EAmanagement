"""
Download 3 years of 1m candle data from Twelve Data, one trading day at a time.
"""
import os, sys, asyncio, httpx
from datetime import datetime, timedelta
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

API_KEY = os.getenv("TWELVEDATA_API_KEY", "dd3adc961e864f25a2d4b17899e315e8")
BASE_URL = "https://api.twelvedata.com/time_series"

SYMBOLS = {"SPY": "ES", "QQQ": "NQ", "IWM": "RTY", "DIA": "YM"}
RATE_LIMIT_DELAY = 1.2
MAX_CALLS = 50000
CALLS_MADE = 0


async def create_table():
    from app.database import engine
    from app.models.market_data import CandleCache
    async with engine.begin() as conn:
        await conn.run_sync(CandleCache.__table__.create, checkfirst=True)
    print("candle_cache table ready")


def trading_days(start, end):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


async def has_data_for_day(db, symbol, day):
    result = await db.execute(
        text("SELECT COUNT(*) FROM candle_cache WHERE symbol = :sym AND timestamp >= :s AND timestamp < :e"),
        {"sym": symbol, "s": day, "e": day + timedelta(days=1)}
    )
    count = result.scalar()
    return count and count > 100


async def download_day(client, db, symbol, instrument, day):
    global CALLS_MADE
    if CALLS_MADE >= MAX_CALLS:
        return -1

    start_str = day.strftime("%Y-%m-%d") + " 09:30:00"
    end_str = day.strftime("%Y-%m-%d") + " 16:00:00"

    try:
        resp = await client.get(BASE_URL, params={
            "symbol": symbol, "interval": "1min",
            "start_date": start_str, "end_date": end_str,
            "outputsize": 5000, "order": "ASC", "apikey": API_KEY
        })
        CALLS_MADE += 1
        data = resp.json()

        if data.get("status") == "error":
            if data.get("code") == 429:
                print("\n  Rate limited! Waiting 65s...")
                await asyncio.sleep(65)
                return 0
            return 0

        values = data.get("values", [])
        if not values:
            return 0

        insert_sql = text(
            "INSERT INTO candle_cache (symbol, instrument, timestamp, open, high, low, close, volume) "
            "VALUES (:symbol, :instrument, :timestamp, :open, :high, :low, :close, :volume) "
            "ON CONFLICT (symbol, timestamp) DO NOTHING"
        )

        for v in values:
            await db.execute(insert_sql, {
                "symbol": symbol, "instrument": instrument,
                "timestamp": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"),
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]),
                "volume": int(v.get("volume", 0)),
            })
        await db.commit()
        return len(values)

    except Exception as e:
        print(f"\n  Error {symbol} {day}: {e}")
        await asyncio.sleep(5)
        return 0


async def main():
    global CALLS_MADE
    await create_table()

    end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=365 * 3)

    print(f"Target: 1m data from {start_date.date()} to {end_date.date()}")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print()

    from app.database import async_session_factory

    async with httpx.AsyncClient(timeout=30.0) as client:
        async with async_session_factory() as db:
            for symbol, instrument in SYMBOLS.items():
                all_days = trading_days(start_date, end_date)
                all_days.reverse()

                total = 0
                skipped = 0
                print(f"{symbol} ({instrument}): {len(all_days)} trading days to check")

                for i, day in enumerate(all_days):
                    if await has_data_for_day(db, symbol, day):
                        skipped += 1
                        continue

                    bars = await download_day(client, db, symbol, instrument, day)
                    if bars == -1:
                        print(f"\n  Hit call limit. Run again to continue.")
                        return
                    if bars > 0:
                        total += bars
                    sys.stdout.write(f"\r  {symbol}: {day.date()} +{bars} bars | total new: {total} | calls: {CALLS_MADE} | {i+1}/{len(all_days)}")
                    sys.stdout.flush()

                    await asyncio.sleep(RATE_LIMIT_DELAY)

                print(f"\n  {symbol}: DONE - {total} new bars, {skipped} days skipped\n")

            result = await db.execute(text(
                "SELECT instrument, COUNT(*), MIN(timestamp), MAX(timestamp) FROM candle_cache GROUP BY instrument ORDER BY instrument"
            ))
            print("\nSummary:")
            for row in result.fetchall():
                print(f"  {row[0]}: {row[1]:,} bars | {row[2]} to {row[3]}")

    print(f"\nTotal API calls: {CALLS_MADE}")


if __name__ == "__main__":
    asyncio.run(main())
