"""
TradingView data feed - fetches real CME futures OHLCV data.
Uses TradingView WebSocket API with auth for full historical data.
Falls back to Yahoo Finance if TV connection fails.
"""
import json, random, string, re, asyncio
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket"
TV_ORIGIN = "https://www.tradingview.com"
TV_SYMBOLS = {"ES": "CME_MINI:ES1!", "NQ": "CME_MINI:NQ1!", "RTY": "CME_MINI:RTY1!", "YM": "CBOT_MINI:YM1!"}
INTERVAL_MAP = {"1m": "1", "2m": "2", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "1h": "60", "4H": "240", "4h": "240", "1D": "1D", "1d": "1D", "1W": "1W"}

def _gen_session():
    return "qs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))

def _gen_chart():
    return "cs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))

def _make_msg(func, params):
    payload = json.dumps({"m": func, "p": params})
    return "~m~" + str(len(payload)) + "~m~" + payload

def _parse_messages(raw):
    results = []
    pattern = re.compile(r"~m~(\d+)~m~")
    pos = 0
    while pos < len(raw):
        match = pattern.match(raw, pos)
        if not match:
            break
        length = int(match.group(1))
        start = match.end()
        msg = raw[start:start + length]
        pos = start + length
        if msg.startswith("{") or msg.startswith("["):
            try:
                results.append(json.loads(msg))
            except json.JSONDecodeError:
                pass
        elif msg.startswith("~h~"):
            results.append({"heartbeat": msg})
    return results

async def _get_tv_auth_token(username, password):
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://www.tradingview.com/accounts/signin/",
                data={"username": username, "password": password},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tradingview.com/"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("user", {}).get("auth_token", "")
                    if token:
                        logger.info("TradingView auth successful")
                        return token
                logger.warning("TradingView auth failed: status=" + str(resp.status))
    except Exception as e:
        logger.warning("TradingView auth error: " + str(e))
    return None

async def fetch_tv_data(instrument, start_date, end_date, interval="15m", tv_username="", tv_password=""):
    tv_symbol = TV_SYMBOLS.get(instrument.upper())
    if not tv_symbol:
        return await _fetch_yfinance_robust(instrument, start_date, end_date, interval)
    tv_interval = INTERVAL_MAP.get(interval, "15")
    n_bars = _estimate_bars(start_date, end_date, interval)
    n_bars = min(n_bars, 10000)
    if tv_username and tv_password:
        try:
            df = await _fetch_via_websocket(tv_symbol, tv_interval, n_bars, start_date, end_date, tv_username, tv_password)
            if df is not None and not df.empty:
                logger.info("TradingView: " + str(len(df)) + " bars for " + instrument + " @ " + interval)
                return df
        except Exception as e:
            logger.warning("TradingView WebSocket failed: " + str(e))
    logger.info("Falling back to Yahoo Finance for " + instrument)
    return await _fetch_yfinance_robust(instrument, start_date, end_date, interval)


async def _fetch_via_websocket(symbol, interval, n_bars, start_date, end_date, username, password):
    import websockets
    auth_token = await _get_tv_auth_token(username, password)
    qs = _gen_session()
    cs = _gen_chart()
    bars = []
    try:
        async with websockets.connect(TV_WS_URL, origin=TV_ORIGIN, additional_headers={"User-Agent": "Mozilla/5.0"}, max_size=2**24, close_timeout=10) as ws:
            if auth_token:
                await ws.send(_make_msg("set_auth_token", [auth_token]))
            else:
                await ws.send(_make_msg("set_auth_token", ["unauthorized_user_token"]))
            await ws.send(_make_msg("chart_create_session", [cs, ""]))
            await ws.send(_make_msg("quote_create_session", [qs]))
            resolve_payload = json.dumps({"symbol": symbol, "adjustment": "splits"})
            await ws.send(_make_msg("resolve_symbol", [cs, "sds_sym_1", "=" + resolve_payload]))
            await ws.send(_make_msg("create_series", [cs, "sds_1", "s1", "sds_sym_1", interval, n_bars, ""]))
            deadline = asyncio.get_event_loop().time() + 30
            got_data = False
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    if got_data:
                        break
                    continue
                if "~h~" in raw and len(raw) < 50:
                    hb = re.search(r"~h~(\d+)", raw)
                    if hb:
                        await ws.send("~m~" + str(len("~h~" + hb.group(1))) + "~m~~h~" + hb.group(1))
                    continue
                messages = _parse_messages(raw)
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("m") in ("timescale_update", "du"):
                        for p in msg.get("p", []):
                            if isinstance(p, dict):
                                for key in ("sds_1", "s1", "$prices"):
                                    s_data = p.get(key, {})
                                    if isinstance(s_data, dict) and "s" in s_data:
                                        for bar in s_data["s"]:
                                            v = bar.get("v", [])
                                            if len(v) >= 6:
                                                bars.append({"timestamp": pd.Timestamp(v[0], unit="s", tz="UTC"), "open": v[1], "high": v[2], "low": v[3], "close": v[4], "volume": int(v[5]) if v[5] else 0})
                                                got_data = True
                    if msg.get("m") == "symbol_error":
                        logger.error("TradingView symbol error: " + str(msg))
                        return None
    except Exception as e:
        logger.error("WebSocket error: " + str(e))
        if not bars:
            return None
    if not bars:
        return None
    df = pd.DataFrame(bars)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    s_ts = pd.Timestamp(start_date)
    e_ts = pd.Timestamp(end_date)
    if s_ts.tz is None:
        s_ts = s_ts.tz_localize("UTC")
    if e_ts.tz is None:
        e_ts = e_ts.tz_localize("UTC")
    df = df[(df.index >= s_ts) & (df.index <= e_ts)]
    logger.info("TradingView WebSocket: got " + str(len(df)) + " bars")
    return df


async def _fetch_yfinance_robust(instrument, start_date, end_date, interval="15m"):
    try:
        import yfinance as yf
    except ImportError:
        return None
    from datetime import timedelta as td
    import pandas as pd

    YAHOO_SYMBOLS = {"ES": "ES=F", "NQ": "NQ=F", "RTY": "RTY=F", "YM": "YM=F"}
    INDEX_SYMBOLS = {"ES": "^GSPC", "NQ": "^IXIC", "RTY": "^RUT", "YM": "^DJI"}
    if hasattr(start_date, "tzinfo") and start_date.tzinfo:
        start_date = start_date.replace(tzinfo=None)
    if hasattr(end_date, "tzinfo") and end_date.tzinfo:
        end_date = end_date.replace(tzinfo=None)

    total_days = (end_date - start_date).days
    yf_interval = interval.lower()
    if interval == "1H":
        yf_interval = "1h"
    if interval == "4H":
        yf_interval = "1h"
    if interval == "1D":
        yf_interval = "1d"
    if yf_interval in ("1m",) and total_days > 7:
        yf_interval = "5m"

    # For intraday intervals over 60 days, fetch in chunks to keep granularity
    needs_chunking = yf_interval in ("5m", "15m", "30m") and total_days > 55
    if yf_interval in ("1h",) and total_days > 730:
        yf_interval = "1d"

    symbol = YAHOO_SYMBOLS.get(instrument.upper())
    if not symbol:
        return None

    syms_to_try = [symbol]
    idx_sym = INDEX_SYMBOLS.get(instrument.upper(), "")
    if idx_sym:
        syms_to_try.append(idx_sym)

    for sym in syms_to_try:
        if not sym:
            continue
        try:
            ticker = yf.Ticker(sym)

            if needs_chunking:
                # Fetch in 25-day chunks to stay under Yahoo's 60-day limit for intraday
                chunk_days = 25
                all_chunks = []
                chunk_start = start_date
                while chunk_start < end_date:
                    chunk_end = min(chunk_start + td(days=chunk_days), end_date)
                    try:
                        chunk_df = await asyncio.to_thread(
                            ticker.history,
                            start=chunk_start.strftime("%Y-%m-%d"),
                            end=chunk_end.strftime("%Y-%m-%d"),
                            interval=yf_interval, auto_adjust=True,
                        )
                        if chunk_df is not None and not chunk_df.empty:
                            all_chunks.append(chunk_df)
                            logger.info(f"Yahoo chunk: {len(chunk_df)} bars for {sym} @ {yf_interval} ({chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')})")
                    except Exception as ce:
                        logger.warning(f"Yahoo chunk failed for {sym} ({chunk_start} to {chunk_end}): {ce}")
                    chunk_start = chunk_end

                if not all_chunks:
                    continue
                df = pd.concat(all_chunks)
                df = df[~df.index.duplicated(keep='first')]
                df = df.sort_index()
            else:
                df = await asyncio.to_thread(
                    ticker.history,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    interval=yf_interval, auto_adjust=True,
                )

            if df is not None and not df.empty:
                df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
                df = df[["open", "high", "low", "close", "volume"]].copy()
                df.index.name = "timestamp"
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                else:
                    df.index = df.index.tz_convert("UTC")
                logger.info(f"Yahoo Finance: {len(df)} total bars for {instrument} ({sym}) @ {yf_interval}")
                return df
        except Exception as e:
            logger.warning("Yahoo failed for " + sym + ": " + str(e))
            continue
    return None


def _estimate_bars(start_date, end_date, interval):
    days = (end_date - start_date).days
    mins_map = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}
    mins = mins_map.get(interval, 15)
    return int(days * 23 * 60 / mins)
