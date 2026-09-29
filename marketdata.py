"""Read-only FX reference prices from Twelve Data.

This module never places orders and never returns the server API key to clients.
The free plan is rate-limited, so quotes are cached centrally for all users.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROVIDER = "Twelve Data"
SYMBOLS = ("EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF")
LABELS = {"EUR/USD": "Euro / US Dollar", "GBP/USD": "British Pound / US Dollar", "USD/JPY": "US Dollar / Japanese Yen", "USD/CHF": "US Dollar / Swiss Franc"}
DEFAULT_CACHE_SECONDS = 600
_lock = threading.Lock()
_cache = {}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_seconds():
    try:
        return max(60, min(3600, int(os.environ.get("MARKET_DATA_CACHE_SECONDS", DEFAULT_CACHE_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS


def is_configured():
    return bool(os.environ.get("TWELVE_DATA_API_KEY", "").strip())


def _fetch_price(symbol, key):
    query = urlencode({"symbol": symbol, "apikey": key})
    request = Request(
        f"https://api.twelvedata.com/price?{query}",
        headers={"Accept": "application/json", "User-Agent": "Xdigitex-FX/1.0"},
    )
    try:
        with urlopen(request, timeout=6) as response:
            if response.status != 200:
                raise ValueError("Market data service returned an error.")
            payload = json.loads(response.read(32768))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("Market data is temporarily unavailable.") from exc
    if not isinstance(payload, dict) or payload.get("status") == "error" or payload.get("code"):
        raise ValueError("Market data is temporarily unavailable.")
    try:
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Market data service returned an invalid price.")
    if not (0 < price < 1e12):
        raise ValueError("Market data service returned an invalid price.")
    return {"symbol": symbol, "label": LABELS[symbol], "price": price, "status": "live", "received_at": _utc_now()}


def get_quotes():
    """Return centrally cached, read-only quotes without exposing the API key."""
    key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    cache_seconds = _cache_seconds()
    if not key:
        return {
            "provider": PROVIDER,
            "configured": False,
            "mode": "read_only",
            "live_execution": False,
            "cache_seconds": cache_seconds,
            "quotes": [],
            "message": "Set TWELVE_DATA_API_KEY on the server to enable read-only FX prices.",
        }

    quotes = []
    with _lock:
        for symbol in SYMBOLS:
            cached = _cache.get(symbol)
            now = time.monotonic()
            if cached and now - cached["cached_at"] < cache_seconds:
                quote = dict(cached["quote"])
                quote["cached"] = True
                quotes.append(quote)
                continue
            try:
                quote = _fetch_price(symbol, key)
                _cache[symbol] = {"cached_at": time.monotonic(), "quote": quote}
                quote = dict(quote)
                quote["cached"] = False
            except ValueError as exc:
                quote = {
                    "symbol": symbol,
                    "label": LABELS[symbol],
                    "price": cached["quote"]["price"] if cached else None,
                    "status": "stale" if cached else "unavailable",
                    "received_at": cached["quote"]["received_at"] if cached else None,
                    "cached": bool(cached),
                    "message": str(exc),
                }
            quotes.append(quote)

    return {
        "provider": PROVIDER,
        "configured": True,
        "mode": "read_only",
        "live_execution": False,
        "cache_seconds": cache_seconds,
        "quotes": quotes,
        "message": "Reference prices only. This feed does not execute or confirm trades.",
    }
