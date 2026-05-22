import os, asyncio, asyncpg
from datetime import datetime, timedelta, timezone
import databento as db

DATABENTO_API_KEY = "db-FXYgMUSjANes6SfsQMue5AKP77nDw"
DATABASE_URL = "postgresql://edge_user:edge_pass@db:5432/edge_db"

INSTRUMENTS = {
    "ES.c.0": "ES",
    "NQ.c.0": "NQ",
    "RTY.c.0": "RTY",
    "YM.c.0": "YM",
}

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
YEARS_BACK = 3
CHUNK_DAYS = 30

async def insert_bars(pool, bars):
    if not bars:
        return 0
    inserted = 0
    async with pool.acquire() as conn:
        stmt = """INSERT INTO candle_cache (symbol, instrument, timestamp, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT ON CONSTRAINT uq_symbol_timestamp DO UPDATE
            SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume"""
        for i in range(0, len(bars), 500):
            batch = bars[i:i+500]
            await conn.executemany(stmt, batch)
            inserted += len(batch)
    return inserted

def download_chunk(client, symbol, start, end):
    print(f"  Requesting {symbol}: {start.date()} to {end.date()}...")
    try:
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=[symbol], schema=SCHEMA,
            start=start.strftime("%Y-%m-%dT%H:%M:%S"),
            end=end.strftime("%Y-%m-%dT%H:%M:%S"),
            stype_in="continuous",
        )
        return data.to_df()
    except Exception as e:
        print(f"  Error: {e}")
        return None

async def main():
    print("=" * 60)
    print("Databento Futures Data Downloader")
    print("=" * 60)
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS candle_cache (
            id BIGSERIAL PRIMARY KEY, symbol VARCHAR(10) NOT NULL,
            instrument VARCHAR(10) NOT NULL, timestamp TIMESTAMPTZ NOT NULL,
            open FLOAT NOT NULL, high FLOAT NOT NULL, low FLOAT NOT NULL,
            close FLOAT NOT NULL, volume BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT uq_symbol_timestamp UNIQUE (symbol, timestamp))""")
        await conn.execute("CREATE INDEX IF NOT EXISTS ix_candle_instrument_ts ON candle_cache (instrument, timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS ix_candle_symbol_ts ON candle_cache (symbol, timestamp)")

    client = db.Historical(DATABENTO_API_KEY)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=YEARS_BACK * 365)
    total_inserted = 0

    print("\nChecking cost estimates...")
    total_cost = 0.0
    for db_symbol, instrument in INSTRUMENTS.items():
        try:
            cost = client.metadata.get_cost(
                dataset=DATASET, symbols=[db_symbol], schema=SCHEMA,
                start=start_date.strftime("%Y-%m-%dT%H:%M:%S"),
                end=end_date.strftime("%Y-%m-%dT%H:%M:%S"),
                stype_in="continuous",
            )
            cost_usd = cost / 1_000_000_000
            total_cost += cost_usd
            print(f"  {instrument} ({db_symbol}): ${cost_usd:.4f}")
        except Exception as e:
            print(f"  {instrument}: cost error: {e}")
    print(f"\nTotal estimated cost: ${total_cost:.4f}")
    print("Proceeding with download...\n")

    for db_symbol, instrument in INSTRUMENTS.items():
        print(f"\n--- Downloading {instrument} ({db_symbol}) ---")
        chunk_start = start_date
        inst_total = 0
        while chunk_start < end_date:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end_date)
            df = download_chunk(client, db_symbol, chunk_start, chunk_end)
            if df is not None and len(df) > 0:
                bars = []
                for idx, row in df.iterrows():
                    ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    o = float(row["open"]) / 1e9 if float(row["open"]) > 1e6 else float(row["open"])
                    h = float(row["high"]) / 1e9 if float(row["high"]) > 1e6 else float(row["high"])
                    l = float(row["low"]) / 1e9 if float(row["low"]) > 1e6 else float(row["low"])
                    c = float(row["close"]) / 1e9 if float(row["close"]) > 1e6 else float(row["close"])
                    v = int(row["volume"]) if "volume" in row else 0
                    bars.append((instrument, instrument, ts, o, h, l, c, v))
                inserted = await insert_bars(pool, bars)
                inst_total += inserted
                print(f"  Inserted {inserted} bars ({chunk_start.date()} to {chunk_end.date()})")
            else:
                print(f"  No data ({chunk_start.date()} to {chunk_end.date()})")
            chunk_start = chunk_end
        print(f"  {instrument} total: {inst_total} bars")
        total_inserted += inst_total

    print(f"\n{=*60}")
    print(f"Done! Total bars: {total_inserted}")
    for db_sym, inst in INSTRUMENTS.items():
        r = await pool.fetchrow("SELECT COUNT(*) as cnt, MIN(timestamp) as mn, MAX(timestamp) as mx FROM candle_cache WHERE instrument=$1", inst)
        if r:
            print(f"  {inst}: {r[cnt]} bars ({r[mn]} to {r[mx]})")
    print("="*60)
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
