# stock.py
from flask import Flask, request
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta, date
from pytz import timezone
from pymongo import MongoClient
from dotenv import load_dotenv
import os
import json
import time
import requests
import httpx
import asyncio
import atexit
from flask_cors import CORS
import pytz

# =========================
# Environment & Constants
# =========================
load_dotenv()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
BASE_URL = "https://paper-api.alpaca.markets"
KEY_ID = os.getenv("KEY_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# Timezones
UTC = pytz.utc
LA = pytz.timezone("America/Los_Angeles")
NY = timezone("America/New_York")

def to_utc(dt):
    """Return a tz-aware UTC datetime (assumes naive is already UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return UTC.localize(dt)
    return dt.astimezone(UTC)

# =========================
# MongoDB (tz-aware)
# =========================
mongo_client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC)
db = mongo_client["alpaca"]
orders_collection = db["orders-live"]
settings_collection = db["settings"]
qualified_stocks_collection = db["qualified-stocks"]
daily_cash_log_collection = db["daily-reports"]

def get_settings():
    """Read the single global settings doc, fallback to latest by timestamp."""
    s = settings_collection.find_one({"_id": "global"})
    if s:
        return s
    return settings_collection.find_one(sort=[("timestamp", -1)]) or {}

# =========================
# Alpaca
# =========================
api = REST(key_id=KEY_ID, secret_key=SECRET_KEY, base_url=BASE_URL)

# =========================
# Flask
# =========================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# =========================
# Global Filters / Defaults
# =========================
EXCHANGES_ALLOWED = ["XNAS", "XNYS"]
MIN_MARKET_CAP_DEFAULT = 1_000_000_000
MIN_PRICE = 1.0

# Simple in-memory cache to reduce repeated metadata calls
ticker_metadata_cache = {}

# =========================
# Market status
# =========================
def is_market_open():
    try:
        url = "https://api.polygon.io/v1/marketstatus/now"
        response = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("market", "") == "open"
    except Exception as e:
        print(f"[ERROR] Checking market status: {e}")
        return False

# =========================
# Order / Position helpers
# =========================
def already_bought_today(symbol):
    """Check by LA (Pacific) trading day, query stored in UTC."""
    now_la = datetime.now(LA)
    start_la = now_la.replace(hour=0, minute=0, second=0, microsecond=0)
    end_la = start_la + timedelta(days=1)
    start_utc = start_la.astimezone(UTC)
    end_utc = end_la.astimezone(UTC)

    return orders_collection.find_one({
        "symbol": symbol,
        "side": "long",
        "timestamp": {"$gte": start_utc, "$lt": end_utc}
    }) is not None

def count_open_positions(symbol):
    return orders_collection.count_documents({
        "symbol": symbol,
        "side": "long",
        "status": "filled",
        "sell_time": {"$exists": False}
    })

def get_active_orders(symbol, side):
    try:
        orders = api.list_orders(status="open")
        return any(o.symbol == symbol and o.side == side for o in orders)
    except Exception as e:
        print(f"Error checking active orders: {e}")
        return False

def get_position_qty(symbol, side):
    try:
        positions = api.list_positions()
        for p in positions:
            if p.symbol == symbol and p.side == side:
                return float(p.qty)
    except Exception as e:
        print(f"Error getting position: {e}")
    return 0.0

def total_buys_today():
    """Count buys by LA day, in UTC for storage."""
    now_la = datetime.now(LA)
    start_la = now_la.replace(hour=0, minute=0, second=0, microsecond=0)
    end_la = start_la + timedelta(days=1)
    start_utc = start_la.astimezone(UTC)
    end_utc = end_la.astimezone(UTC)
    return orders_collection.count_documents({
        "side": "long",
        "timestamp": {"$gte": start_utc, "$lt": end_utc}
    })

def get_filled_order_info(order_id):
    try:
        order = api.get_order(order_id)
        if order.filled_avg_price and order.filled_qty:
            return {"avg_price": float(order.filled_avg_price), "qty": float(order.filled_qty)}
        else:
            print(f"[INFO] Order {order_id} not fully filled yet.")
    except Exception as e:
        print(f"[ERROR] Fetching order {order_id}: {e}")
    return None

# =========================
# Polygon sync helpers (sync)
# =========================
def fetch_ticker_details(symbol):
    """Synchronous metadata with tiny delay for rate-limit friendliness."""
    if symbol in ticker_metadata_cache:
        return ticker_metadata_cache[symbol]
    try:
        url = f"https://api.polygon.io/v3/reference/tickers/{symbol}"
        response = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=10)
        response.raise_for_status()
        data = response.json().get("results", {})
        ticker_metadata_cache[symbol] = data
        time.sleep(0.25)  # Polygon ~4 rps
        return data
    except Exception as e:
        print(f"[ERROR] Fetching metadata for {symbol}: {e}")
        return {}

def get_price_52_weeks_ago(symbol):
    """Kept for compatibility; uses lookback ~43 days per your setting (name retained)."""
    try:
        one_year_ago = (datetime.utcnow() - timedelta(days=43)).strftime("%Y-%m-%d")
        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{one_year_ago}/{one_year_ago}"
        params = {"adjusted": "true", "sort": "asc", "limit": 1, "apiKey": POLYGON_API_KEY}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "results" in data and data["results"]:
            return data["results"][0]["c"]
    except Exception as e:
        print(f"[ERROR] Fetching lookback price for {symbol}: {e}")
    return None

# =========================
# Async HTTP helpers (bounded)
# =========================
async def _bounded_get_json(client: httpx.AsyncClient, url: str, params: dict, sem: asyncio.Semaphore, label: str):
    """GET JSON with concurrency bound and simple retry."""
    tries = 2
    for attempt in range(tries):
        try:
            async with sem:
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == tries - 1:
                print(f"[{label} ERROR] url={url} err={e!r}")
                return None
            await asyncio.sleep(0.4)

async def fetch_window_closes(client, symbol, days, sem):
    """Return last (days+1) daily bars ascending."""
    start_date = (datetime.utcnow() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
    data = await _bounded_get_json(client, url, {
        "adjusted": "true", "sort": "asc", "limit": 500, "apiKey": POLYGON_API_KEY
    }, sem, "SPIKE CLOSES")
    if not data:
        return []
    return (data.get("results") or [])[-(days + 1):]

async def fetch_ath_info(client, symbol, lookback_days, sem):
    """Return (symbol, ath_close, ath_date_iso)."""
    start_date = (datetime.utcnow() - timedelta(days=lookback_days + 7)).strftime("%Y-%m-%d")
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
    data = await _bounded_get_json(client, url, {
        "adjusted": "true", "sort": "asc", "limit": 500, "apiKey": POLYGON_API_KEY
    }, sem, "ATH")
    if not data:
        return symbol, None, None
    bars = data.get("results") or []
    if not bars:
        return symbol, None, None
    max_bar = max((b for b in bars if "c" in b and b["c"] is not None), key=lambda b: b["c"], default=None)
    if not max_bar:
        return symbol, None, None
    ath_close = float(max_bar["c"])
    ath_date = datetime.utcfromtimestamp(max_bar["t"] / 1000).date().isoformat()
    return symbol, ath_close, ath_date

async def fetch_lookback_price(client, symbol, days, sem):
    """Return (symbol, price) at closest to target day."""
    target_date = datetime.utcnow() - timedelta(days=days)
    start = (target_date - timedelta(days=3)).strftime("%Y-%m-%d")
    end = (target_date + timedelta(days=3)).strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
    data = await _bounded_get_json(client, url, {
        "adjusted": "true", "sort": "asc", "limit": 10, "apiKey": POLYGON_API_KEY
    }, sem, "SURGE")
    if not data:
        return symbol, None
    results = data.get("results") or []
    if not results:
        return symbol, None
    closest = min(results, key=lambda r: abs(target_date - datetime.utcfromtimestamp(r["t"] / 1000)))
    return symbol, closest.get("c")

async def fetch_metadata(client, symbol, sem):
    url = f"https://api.polygon.io/v3/reference/tickers/{symbol}"
    data = await _bounded_get_json(client, url, {"apiKey": POLYGON_API_KEY}, sem, "META")
    return symbol, (data.get("results") if data else {})

async def fetch_52w_price(client, symbol, sem):
    current_settings = get_settings()
    lookback_days = current_settings.get("price_lookback_days", 43)
    start_date = (datetime.utcnow() - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    end_date = (datetime.utcnow() - timedelta(days=lookback_days - 10)).strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
    data = await _bounded_get_json(client, url, {
        "adjusted": "true", "sort": "asc", "limit": 150, "apiKey": POLYGON_API_KEY
    }, sem, "52W")
    if not data:
        return symbol, None, None
    results = data.get("results") or []
    if not results:
        return symbol, None, None
    target_date = datetime.utcnow() - timedelta(days=lookback_days)
    closest = min(results, key=lambda r: abs(target_date - datetime.utcfromtimestamp(r["t"] / 1000)))
    price = closest.get("c")
    dt = datetime.utcfromtimestamp(closest["t"] / 1000).date().isoformat()
    return symbol, price, dt

# =========================
# Core scanners
# =========================
def get_stocks_down_5_percent_fast():
    """Pull ALL snapshot tickers and filter. No fixed cap; buy logic filters later."""
    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    response = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=15)
    response.raise_for_status()
    tickers = response.json().get("tickers", [])

    settings = get_settings()
    min_change = settings.get("MIN_CHANGE_PERCENT", -5)
    min_volume = settings.get("min_volume", 2_000_000)

    selected = []
    for t in tickers:
        symbol = t.get("ticker", "")
        change = t.get("todaysChangePerc", 0)
        price = t.get("day", {}).get("c", 0)
        volume = t.get("day", {}).get("v", 0)
        if (change <= min_change and price > MIN_PRICE and volume >= min_volume and
                not any(suffix in symbol for suffix in [".", "W", "U", "WS"])):
            selected.append({
                "symbol": symbol,
                "change_percent": change,
                "price": price,
                "volume": volume
            })
    return selected

async def enrich_tickers(tickers):
    """Async enrichment with bounded concurrency and robust error handling."""
    enriched = {}

    s = get_settings()
    MIN_MARKET_CAP = s.get("MIN_MARKET_CAP", MIN_MARKET_CAP_DEFAULT)

    # Surge lookback / ceiling
    lookback_surge_days = s.get("lookback_surge_days", 30)
    lookback_surge_percentage = s.get("lookback_surge_percentage", 100)

    # Multi-horizon gains
    short_up_days = s.get("short_up_days", 5);    short_up_gain = s.get("short_up_gain", 1.0)
    mid_up_days   = s.get("mid_up_days", 10);     mid_up_gain   = s.get("mid_up_gain", 1.0)
    long_up_days  = s.get("long_up_days", 30);    long_up_gain  = s.get("long_up_gain", 1.0)
    xlong_up_days = s.get("xlong_up_days", 90);   xlong_up_gain = s.get("xlong_up_gain", 5.0)

    # Spike
    spike_days = s.get("spike_days", 5)
    spike_gain = s.get("spike_gain", 5.0)

    # ATH block (STRICTLY TIME-BASED)
    ath_lookback_days = s.get("ath_lookback_days", 90)
    ath_block_days = s.get("ath_block_days", 10)
    # NOTE: ath_within_pct is ignored by design now

    # For diagnostics / fields
    price_lookback_days = s.get("price_lookback_days", 43)

    # Concurrency controls
    concurrency = int(s.get("concurrency", 20))
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=max(30, concurrency), max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        # Create tasks per category
        metadata_tasks = [fetch_metadata(client, t["symbol"], sem) for t in tickers]
        price52w_tasks = [fetch_52w_price(client, t["symbol"], sem) for t in tickers]
        surge_tasks = [fetch_lookback_price(client, t["symbol"], lookback_surge_days, sem) for t in tickers]
        short_tasks = [fetch_lookback_price(client, t["symbol"], short_up_days, sem) for t in tickers]
        mid_tasks = [fetch_lookback_price(client, t["symbol"], mid_up_days, sem) for t in tickers]
        long_tasks = [fetch_lookback_price(client, t["symbol"], long_up_days, sem) for t in tickers]
        xlong_tasks = [fetch_lookback_price(client, t["symbol"], xlong_up_days, sem) for t in tickers]
        spike_window_tasks = [fetch_window_closes(client, t["symbol"], spike_days, sem) for t in tickers]
        ath_tasks = [fetch_ath_info(client, t["symbol"], ath_lookback_days, sem) for t in tickers]

        # Await each group with return_exceptions=True
        meta_results = await asyncio.gather(*metadata_tasks, return_exceptions=True)
        price52w_results = await asyncio.gather(*price52w_tasks, return_exceptions=True)
        surge_results = await asyncio.gather(*surge_tasks, return_exceptions=True)
        short_results = await asyncio.gather(*short_tasks, return_exceptions=True)
        mid_results = await asyncio.gather(*mid_tasks, return_exceptions=True)
        long_results = await asyncio.gather(*long_tasks, return_exceptions=True)
        xlong_results = await asyncio.gather(*xlong_tasks, return_exceptions=True)
        spike_windows = await asyncio.gather(*spike_window_tasks, return_exceptions=True)
        ath_results = await asyncio.gather(*ath_tasks, return_exceptions=True)

    # Store metadata
    for r in meta_results:
        if isinstance(r, Exception) or r is None:
            continue
        symbol, meta = r
        enriched.setdefault(symbol, {})["metadata"] = meta or {}

    # Store 52w lookback & date
    for r in price52w_results:
        if isinstance(r, Exception) or r is None:
            continue
        symbol, price_52w, price_lookback_date = r
        enriched.setdefault(symbol, {})["price_52w"] = price_52w
        enriched[symbol]["price_lookback_date"] = price_lookback_date

    # Per-period single-point lookbacks
    for r in surge_results:
        if isinstance(r, Exception) or r is None:
            continue
        symbol, price = r
        enriched.setdefault(symbol, {}).setdefault("lookbacks", {})["surge"] = price
        enriched[symbol]["lookback_surge_date"] = (datetime.utcnow() - timedelta(days=lookback_surge_days)).date().isoformat()

    def _store_group(results, key):
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            symbol, price = r
            enriched.setdefault(symbol, {}).setdefault("lookbacks", {})[key] = price

    _store_group(short_results, "short")
    _store_group(mid_results, "mid")
    _store_group(long_results, "long")
    _store_group(xlong_results, "xlong")

    for r in ath_results:
        if isinstance(r, Exception) or r is None:
            continue
        symbol, ath_close, ath_date_iso = r
        enriched.setdefault(symbol, {})["ath_close"] = ath_close
        enriched[symbol]["ath_date"] = ath_date_iso

    for (t, bars) in zip(tickers, spike_windows):
        if isinstance(bars, Exception) or bars is None:
            bars = []
        enriched.setdefault(t["symbol"], {})["spike_bars"] = bars

    # Construct final list applying filters
    final = []
    for t in tickers:
        symbol = t["symbol"]
        meta = enriched.get(symbol, {}).get("metadata", {})
        price_52w = enriched.get(symbol, {}).get("price_52w")
        price_lookback_date = enriched.get(symbol, {}).get("price_lookback_date")
        lookbacks = enriched.get(symbol, {}).get("lookbacks", {})
        lookback_surge_date = enriched.get(symbol, {}).get("lookback_surge_date")
        spike_bars = enriched.get(symbol, {}).get("spike_bars", [])
        ath_close = enriched.get(symbol, {}).get("ath_close")
        ath_date_iso = enriched.get(symbol, {}).get("ath_date")

        market_cap = meta.get("market_cap", 0)
        exchange = meta.get("primary_exchange", "")
        current_price = t["price"]

        # Hard filters
        if exchange not in EXCHANGES_ALLOWED:
            continue
        if market_cap is None or market_cap < MIN_MARKET_CAP:
            continue

        # Spike guard
        spike_max_pct = None
        spike_max_date = None
        spike_ref_close = None
        if spike_bars:
            today_utc = datetime.utcnow().date()
            prior_closes = []
            for r in spike_bars:
                d = datetime.utcfromtimestamp(r["t"] / 1000).date()
                if d < today_utc and r.get("c"):
                    prior_closes.append((d, r["c"]))
            prior_closes = prior_closes[-spike_days:]
            for d, close_px in prior_closes:
                if close_px and close_px > 0:
                    pct = ((current_price - close_px) / close_px) * 100
                    if spike_max_pct is None or pct > spike_max_pct:
                        spike_max_pct = pct
                        spike_max_date = d.isoformat()
                        spike_ref_close = close_px
        if spike_max_pct is not None and spike_max_pct > spike_gain:
            print(f"[SKIP SPIKE] {symbol} {round(spike_max_pct,2)}% above prior close on {spike_max_date} (>{spike_gain}%).")
            continue

        # Surge filter + 1% rule
        surge_price = lookbacks.get("surge")
        if not surge_price or surge_price <= 0:
            continue
        surge_percent = ((current_price - surge_price) / surge_price) * 100
        if surge_percent > lookback_surge_percentage:
            print(f"[SKIP SURGE] {symbol} surged {round(surge_percent,2)}% in {lookback_surge_days}d (> {lookback_surge_percentage}%).")
            continue
        if surge_percent < 1.0:
            continue

        # Positive long lookback
        if not price_52w or price_52w <= 0:
            continue
        change_52w = round(((current_price - price_52w) / price_52w) * 100, 2)
        if change_52w <= 0:
            if 500_000_000 <= (market_cap or 0) < 2_000_000_000:
                print(f"[SKIP UP] ⛔ {symbol} 52w uptrend {change_52w}%")
            continue

        # STRICT time-based ATH block
        if ath_close and ath_date_iso:
            try:
                ath_date = datetime.fromisoformat(ath_date_iso).date()
                days_since_ath = (datetime.utcnow().date() - ath_date).days
                if days_since_ath <= ath_block_days:
                    print(f"[SKIP ATH] {symbol} blocked by days: {days_since_ath}d since {ath_date_iso} "
                          f"(block_days={ath_block_days})")
                    continue
            except Exception as e:
                print(f"[ATH PARSE WARN] {symbol}: {e}")

        def pct_vs(px):
            return ((current_price - px) / px) * 100 if px and px > 0 else None

        short_pct = pct_vs(lookbacks.get("short"))
        mid_pct = pct_vs(lookbacks.get("mid"))
        long_pct = pct_vs(lookbacks.get("long"))
        xlong_pct = pct_vs(lookbacks.get("xlong"))

        if (short_pct is None or short_pct < short_up_gain or
                mid_pct is None or mid_pct < mid_up_gain or
                long_pct is None or long_pct < long_up_gain or
                xlong_pct is None or xlong_pct < xlong_up_gain):
            if 500_000_000 <= (market_cap or 0) < 2_000_000_000:
                print(f"[SKIP HORIZON] {symbol} short:{short_pct}, mid:{mid_pct}, long:{long_pct}, xlong:{xlong_pct}")
            continue

        final.append({
            "symbol": symbol,
            "price": current_price,
            "volume": t["volume"],
            "change_percent": round(t["change_percent"], 2),
            "market_cap": market_cap,
            "exchange": exchange,

            # 52w-like lookback
            "change_52w_percent": change_52w,
            "price_lookback_days": price_lookback_days,
            "price_lookback_date": price_lookback_date,
            "lookback_price_52w": price_52w,

            # surge
            "surge_price": surge_price,
            "surge_percent": round(surge_percent, 2),
            "surge_lookback_days": lookback_surge_days,
            "lookback_surge_date": lookback_surge_date,

            # horizons (diagnostics)
            "short_up_days": short_up_days, "short_up_gain": short_up_gain, "short_up_pct": round(short_pct, 2) if short_pct is not None else None,
            "mid_up_days": mid_up_days, "mid_up_gain": mid_up_gain, "mid_up_pct": round(mid_pct, 2) if mid_pct is not None else None,
            "long_up_days": long_up_days, "long_up_gain": long_up_gain, "long_up_pct": round(long_pct, 2) if long_pct is not None else None,
            "xlong_up_days": xlong_up_days, "xlong_up_gain": xlong_up_gain, "xlong_up_pct": round(xlong_pct, 2) if xlong_pct is not None else None,

            # spike (diagnostics)
            "spike_days": spike_days,
            "spike_gain": spike_gain,
            "spike_price_mode": "close",
            "spike_max_pct": round(spike_max_pct, 2) if spike_max_pct is not None else None,
            "spike_max_date": spike_max_date,
            "spike_ref_close": round(spike_ref_close, 4) if spike_ref_close is not None else None,

            "ath_close": ath_close,
            "ath_date": ath_date_iso,
            "ath_block_days": ath_block_days,
            # we keep ath_gap_pct out since it's not used for blocking anymore
        })

    # Sort by worst % change first
    return sorted(final, key=lambda x: x["change_percent"])

# =========================
# Cleanup & logging
# =========================
def cleanup_stale_orders():
    if not is_market_open():
        print("[CLEANUP SKIPPED] Market is closed.")
        return
    try:
        alpaca_symbols = [p.symbol for p in api.list_positions() if p.side == "long"]
        mongo_symbols = orders_collection.distinct("symbol", {"side": "long"})
        stale_symbols = [s for s in mongo_symbols if s not in alpaca_symbols]
        for symbol in stale_symbols:
            result = orders_collection.delete_many({"symbol": symbol, "side": "long"})
            print(f"[CLEANUP] Removed {result.deleted_count} stale orders for {symbol} from MongoDB.")
    except Exception as e:
        print(f"[ERROR] Cleanup failed: {e}")

def log_qualified_stocks(tickers):
    today_str = date.today().isoformat()
    for stock in tickers:
        symbol = stock["symbol"]
        already_logged = qualified_stocks_collection.find_one({"symbol": symbol, "date": today_str})
        if already_logged:
            continue
        doc = {
            **stock,
            "date": today_str,
            "created_at": datetime.now(UTC),
        }
        qualified_stocks_collection.insert_one(doc)
        print(f"[LOGGED] Qualified stock logged: {symbol}")

def log_daily_cash():
    now = datetime.now(NY)
    today_str = now.date().isoformat()

    already_logged = daily_cash_log_collection.find_one({"date": today_str})
    if already_logged:
        print("[SKIP] Cash already logged today.")
        return

    try:
        account = api.get_account()
        cash = float(account.cash)
        portfolio_value = float(account.portfolio_value)

        yesterday = now.date() - timedelta(days=1)
        yesterday_entry = daily_cash_log_collection.find_one({"date": yesterday.isoformat()})

        pct_change = None
        if yesterday_entry:
            prev_portfolio = yesterday_entry.get("portfolio_value", 0)
            if prev_portfolio > 0:
                pct_change = round(((portfolio_value - prev_portfolio) / prev_portfolio) * 100, 2)

        entry = {
            "date": today_str,
            "cash": cash,
            "portfolio_value": portfolio_value,
            "pct_change": pct_change,
            "created_at": datetime.now(UTC)
        }
        daily_cash_log_collection.insert_one(entry)
        print(f"[LOGGED] Daily cash: ${cash:.2f} (Change: {pct_change}%)")
    except Exception as e:
        print(f"[ERROR] Logging cash: {e}")

# =========================
# Strategy Runner
# =========================
def auto_buy_from_losers():
    if not is_market_open():
        print("[MARKET CLOSED] Skipping auto-buy.")
        return

    # --- Update actual buy data for all unsold orders, and opportunistic sells ---
    unsold_orders = list(orders_collection.find({"sell_time": {"$exists": False}}))

    for order in unsold_orders:
        alpaca_order_id = order.get("alpaca_order_id")

        # Fill info (avg price, qty) if we have an order id
        if alpaca_order_id:
            filled_info = get_filled_order_info(alpaca_order_id)
            if not filled_info:
                print(f"[SKIP] Could not fetch fill info for order {alpaca_order_id}")
            else:
                real_buy_price = filled_info["avg_price"]
                real_qty = filled_info["qty"]
                try:
                    current_price = float(api.get_latest_trade(order["symbol"]).p)
                except Exception as e:
                    print(f"[ERROR] Fetching current price for {order['symbol']}: {e}")
                    current_price = None

                update_data = {"price": real_buy_price, "qty": real_qty}
                if current_price is not None:
                    update_data["current_price"] = current_price

                orders_collection.update_one({"alpaca_order_id": alpaca_order_id}, {"$set": update_data})

        # Evaluate profit for potential sell
        try:
            position_qty = get_position_qty(order["symbol"], "long")
            if position_qty <= 0:
                continue
            position = api.get_position(order["symbol"])
            avg_price = float(position.avg_entry_price)
            current_price = float(position.current_price)
            profit_pct = ((current_price - avg_price) / avg_price) * 100
        except Exception as e:
            print(f"[ERROR] Position check for {order['symbol']}: {e}")
            continue

        s = get_settings()
        profit_target = s.get("profit_percent", 10)
        same_day_target = s.get("same_day_profit_percent", 2)
        mid_term_target = s.get("mid_term_profit_percent", 5)
        long_term_target = s.get("long_term_profit_percent", 10)

        # Decide if same-day (LA)
        buy_time = order.get("timestamp")
        if buy_time:
            buy_time_utc = to_utc(buy_time)
            now_la = datetime.now(LA)
            buy_time_la = buy_time_utc.astimezone(LA)
            is_same_day = (now_la.date() == buy_time_la.date())
        else:
            is_same_day = False

        target = same_day_target if is_same_day else profit_target

        # Primary sell rule
        if profit_pct >= target:
            try:
                api.close_position(symbol=order["symbol"])
                orders_collection.update_many(
                    {"symbol": order["symbol"], "side": "long", "sell_time": {"$exists": False}},
                    {"$set": {"sell_time": datetime.now(UTC), "sell_price": current_price}}
                )
                print(f"[SELL] {order['symbol']} at ${current_price} (Gain: {round(profit_pct, 2)}%)")
                continue
            except Exception as e:
                print(f"[ERROR] Sell failed for {order['symbol']}: {e}")

        # Mid / Long bonus rules by hold days
        now_utc = datetime.now(UTC)
        hold_days = None
        if buy_time:
            buy_time_utc = to_utc(buy_time)
            hold_days = (now_utc - buy_time_utc).days

        if hold_days is not None and 1 < hold_days <= 12 and profit_pct >= mid_term_target:
            try:
                api.close_position(symbol=order["symbol"])
                orders_collection.update_many(
                    {"symbol": order["symbol"], "side": "long", "sell_time": {"$exists": False}},
                    {"$set": {"sell_time": datetime.now(UTC), "sell_price": current_price}}
                )
                print(f"[SELL BONUS] {order['symbol']} after {hold_days} days (Gain: {round(profit_pct, 2)}%)")
                continue
            except Exception as e:
                print(f"[ERROR] Bonus Sell failed for {order['symbol']}: {e}")

        if hold_days is not None and hold_days > 12 and profit_pct >= long_term_target:
            try:
                api.close_position(symbol=order["symbol"])
                orders_collection.update_many(
                    {"symbol": order["symbol"], "side": "long", "sell_time": {"$exists": False}},
                    {"$set": {"sell_time": datetime.now(UTC), "sell_price": current_price}}
                )
                print(f"[SELL BONUS] {order['symbol']} after {hold_days} days (Gain: {round(profit_pct, 2)}%)")
                continue
            except Exception as e:
                print(f"[ERROR] Bonus Sell failed for {order['symbol']}: {e}")

    # --- Scan & enrich losers (no artificial cap) ---
    tickers = get_stocks_down_5_percent_fast()
    losers = asyncio.run(enrich_tickers(tickers))
    log_qualified_stocks(losers)

    # --- Determine buy budgets ---
    s = get_settings()
    max_buys_per_symbol = s.get("max_buys", 10)
    total_limit = s.get("total_buys_today", 5)

    # amount via percent-of-portfolio or fixed
    use_percent = s.get("use_percent", False)
    if use_percent:
        try:
            account = api.get_account()
            portfolio_value = float(account.portfolio_value)
            amount_percent = s.get("amount_in_percent", 0)
            amount = round(portfolio_value * amount_percent / 100, 2)
        except Exception as e:
            print(f"[ERROR] Calculating percent-based amount: {e}")
            amount = s.get("amount", 100)
    else:
        amount = s.get("amount", 100)

    # Ensure minimum cash
    try:
        account = api.get_account()
        cash_balance = float(account.cash)
        if cash_balance < 300:
            print(f"[CASH LOW] Skipping buy. Available cash is ${cash_balance:.2f}")
            return
    except Exception as e:
        print(f"[ERROR] Failed to fetch Alpaca cash: {e}")
        return

    # --- Place buys based on enriched list ---
    for stock in losers:
        symbol = stock["symbol"]

        if total_buys_today() >= total_limit:
            print(f"[LIMIT REACHED] Already bought {total_limit} stocks today.")
            return

        try:
            if already_bought_today(symbol):
                print(f"[SKIP] Already bought {symbol} today.")
                continue

            if count_open_positions(symbol) >= max_buys_per_symbol:
                print(f"[SKIP] Max buys reached for {symbol}.")
                continue

            # Defensive ATH check again at buy time (STRICTLY TIME-BASED)
            s = get_settings()
            ath_block_days = s.get("ath_block_days", 10)

            ath_date_iso = stock.get("ath_date")
            if ath_date_iso:
                try:
                    ath_date = datetime.fromisoformat(ath_date_iso).date()
                    days_since_ath = (datetime.utcnow().date() - ath_date).days
                    if days_since_ath <= ath_block_days:
                        print(f"[SKIP BUY ATH] {symbol} blocked by days at buy time: "
                              f"{days_since_ath}d since {ath_date_iso} (block_days={ath_block_days})")
                        continue
                except Exception as e:
                    print(f"[ATH BUY CHECK WARN] {symbol}: {e}")

            # Get real-time quote
            try:
                quote = api.get_latest_quote(symbol)
                ask_price = float(getattr(quote, "ask_price", 0.0) or getattr(quote, "ap", 0.0))
                bid_price = float(getattr(quote, "bid_price", 0.0) or getattr(quote, "bp", 0.0))
                if ask_price <= 0:
                    print(f"[SKIP] Invalid ask price for {symbol}: {ask_price}")
                    continue
            except Exception as e:
                print(f"[QUOTE ERROR] Failed to fetch quote for {symbol}: {e}")
                continue

            # Market buy using notional amount
            buy_order = api.submit_order(
                symbol=symbol,
                notional=amount,
                side="buy",
                type="market",
                time_in_force="day"
            )

            time.sleep(2)  # allow position update

            # Use position to get exact qty
            try:
                position = api.get_position(symbol)
                qty = float(position.qty) if position and position.qty else round(amount / ask_price, 6)
            except Exception:
                qty = round(amount / ask_price, 6)

            if qty <= 0:
                print(f"[SKIP] No qty bought for {symbol}")
                continue

            # Record in Mongo (UTC-aware timestamp)
            orders_collection.insert_one({
                "symbol": symbol,
                "side": "long",
                "qty": qty,
                "price": ask_price if ask_price > 0 else bid_price,
                "status": "filled",
                "timestamp": datetime.now(UTC),
                "alpaca_order_id": buy_order.id
            })

            print(f"[BUY] Successfully bought {symbol}")

        except Exception as e:
            print(f"[ERROR] Failed to buy {symbol}: {e}")

# =========================
# Flask Endpoints
# =========================
@app.route("/stocks/optimized", methods=["GET"])
def fast_stocks_endpoint():
    tickers = get_stocks_down_5_percent_fast()
    result = asyncio.run(enrich_tickers(tickers))
    return {"tickers": result}

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data)
        symbol = data["tickers"]
        side = data["side"]
        amount = data["amount"]
        max_buys = int(data.get("max_buys", 3))

        print(f"{symbol} {side} signal received.")

        if not is_market_open():
            return {"code": "error", "message": "Market is closed."}

        if total_buys_today() >= get_settings().get("total_buys_today", 5):
            return {"code": "error", "message": "Max daily buy limit reached."}

        if side == "long":
            if already_bought_today(symbol):
                return {"code": "error", "message": f"Already bought {symbol} today."}
            if count_open_positions(symbol) >= max_buys:
                return {"code": "error", "message": f"Max positions reached for {symbol}."}

            # Quote
            quote = api.get_latest_quote(symbol)
            ask_price = float(getattr(quote, "ask_price", 0.0) or getattr(quote, "ap", 0.0))
            if ask_price <= 0:
                return {"code": "error", "message": "Invalid ask price."}

            # Market buy (notional)
            buy_order = api.submit_order(
                symbol=symbol,
                notional=amount,
                side="buy",
                type="market",
                time_in_force="day"
            )

            time.sleep(2)
            # Exact qty
            try:
                position = api.get_position(symbol)
                qty = float(position.qty)
            except Exception:
                qty = round(amount / ask_price, 6)

            # Log
            orders_collection.insert_one({
                "symbol": symbol,
                "side": "long",
                "qty": qty,
                "price": ask_price,
                "status": "filled",
                "timestamp": datetime.now(UTC),
                "alpaca_order_id": buy_order.id
            })

            return {"code": "success", "message": f"Buy order for {symbol} placed."}

        elif side == "long_close":
            position_qty = get_position_qty(symbol, "long")
            if position_qty > 0:
                # Cancel open orders for that symbol
                open_orders = api.list_orders(status="open")
                for o in open_orders:
                    if o.symbol == symbol:
                        api.cancel_order(o.id)
                        time.sleep(1)
                # Close position
                api.close_position(symbol)
                print(f"[SUCCESS] Long position for {symbol} closed.")

                # Delete all MongoDB logs for this symbol
                deleted_result = orders_collection.delete_many({"symbol": symbol})
                print(f"[MONGO] Deleted {deleted_result.deleted_count} order records for {symbol}")

                return {
                    "code": "success",
                    "message": f"Closed long position for {symbol}. Deleted {deleted_result.deleted_count} records from MongoDB."
                }
            else:
                print(f"[SKIPPED] No open long position for {symbol}.")
                return {"code": "skipped", "message": f"No open long position for {symbol}"}

        return {"code": "skipped", "message": "Conditions not met."}

    except Exception as e:
        print(f"Error: {e}")
        return {"code": "error", "message": str(e)}

@app.route("/stocks", methods=["GET"])
def down_5_endpoint():
    tickers = get_stocks_down_5_percent_fast()
    return {"tickers": tickers}

@app.route("/stocks/yesterday", methods=["GET"])
def down_5_yesterday_endpoint():
    try:
        poly_key = os.getenv("POLYGON_API_KEY")
        dt = datetime.utcnow() - timedelta(days=2)
        formatted_date = dt.strftime("%Y-%m-%d")
        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{formatted_date}"
        params = {"apiKey": poly_key}
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        losers = [
            {
                "symbol": t["T"],
                "percent_change": round(((t["c"] - t["o"]) / t["o"]) * 100, 2)
            }
            for t in data["results"]
            if t["o"] > 1 and t["v"] > 500000 and
               not any(s in t["T"] for s in [".", "W", "U", "WS"]) and
               ((t["c"] - t["o"]) / t["o"]) <= -0.05
        ]
        losers.sort(key=lambda x: x["percent_change"])
        return {"tickers": losers}
    except Exception as e:
        print(f"Polygon API Error: {e}")
        return {"tickers": []}

@app.route("/market-status", methods=["GET"])
def market_status():
    status = "open" if is_market_open() else "closed"
    return {"market": status}

@app.route("/api/orders", methods=["GET"])
def get_orders():
    try:
        orders = list(orders_collection.find({}, {"_id": 0}))
        return {"orders": orders}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/api/settings", methods=["POST"])
def save_settings():
    try:
        data = request.json
        if data.get("MIN_CHANGE_PERCENT", 0) >= 0:
            return {"code": "error", "message": "MIN_CHANGE_PERCENT must be negative."}, 400

        if "same_day_profit_percent" not in data:
            data["same_day_profit_percent"] = 0

        data["_id"] = "global"
        data["timestamp"] = datetime.now(UTC)

        db["settings"].update_one({"_id": "global"}, {"$set": data}, upsert=True)
        return {"code": "success", "message": "Settings updated."}
    except Exception as e:
        return {"code": "error", "message": str(e)}, 500

@app.route("/api/settings", methods=["GET"])
def get_latest_settings():
    try:
        latest = get_settings()
        if not latest:
            return {"code": "empty", "message": "No settings found."}, 404
        latest = {k: v for k, v in latest.items() if k != "_id"}
        return {"code": "success", "settings": latest}
    except Exception as e:
        return {"code": "error", "message": str(e)}, 500

@app.route("/api/cash", methods=["GET"])
def get_cash():
    try:
        account = api.get_account()
        return {
            "status": "success",
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value)
        }
    except Exception as e:
        print(f"[ERROR] Fetching Alpaca cash balance: {e}")
        return {"status": "error", "message": str(e)}, 500

@app.route("/api/qualified-stocks", methods=["GET"])
def get_qualified_stocks():
    try:
        stocks = list(qualified_stocks_collection.find({}, {"_id": 0}))
        return {"qualified_stocks": stocks}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/api/reports", methods=["GET"])
def get_cash_reports():
    try:
        reports = list(db["daily-reports"].find({}, {"_id": 0}))
        return {"reports": sorted(reports, key=lambda r: r["date"], reverse=True)}
    except Exception as e:
        return {"error": str(e)}, 500

# =========================
# Scheduler
# =========================
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()

# Every 3 minutes
scheduler.add_job(func=auto_buy_from_losers, trigger="interval", minutes=3)

# Log daily cash at 8:00pm ET
scheduler.add_job(
    log_daily_cash,
    trigger="cron",
    hour=20,
    minute=0,
    timezone="America/New_York"
)

scheduler.start()
atexit.register(lambda: scheduler.shutdown())
