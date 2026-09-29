"""Read-only FX reference prices from Twelve Data.

This module never places orders and never returns the server API key to clients.
The free plan is rate-limited, so quotes and their recent history are cached
centrally for all users and refreshed at most once per MARKET_DATA_CACHE_SECONDS.
Prices come from the provider's 1-minute close series so the quote and the chart
always describe the same observation; no value is ever synthesised locally.
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
HISTORY_INTERVAL = "1min"
HISTORY_POINTS = 90
HISTORY_LIMIT = 120
FAILURE_BACKOFF_SECONDS = 60
REQUEST_TIMEOUT = 8
_lock = threading.Lock()
_cache = {}
_failures = {}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_seconds():
    try:
        return max(60, min(3600, int(os.environ.get("MARKET_DATA_CACHE_SECONDS", DEFAULT_CACHE_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_SECONDS


def is_configured():
    return bool(os.environ.get("TWELVE_DATA_API_KEY", "").strip())


def _provider_message(payload):
    if isinstance(payload, dict):
        for field in ("message", "error"):
            if payload.get(field):
                return str(payload[field])[:200]
    return "Market data service returned an error."


def _request_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Xdigitex-FX/1.0"})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise ValueError(f"Market data service returned HTTP {response.status}.")
            return json.loads(response.read(262144))
    except HTTPError as exc:
        try:
            return json.loads(exc.read(4096))
        except Exception:
            raise ValueError(f"Market data service rejected the request (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("Market data is temporarily unavailable.") from exc


def _price(value):
    price = float(value)
    if not (0 < price < 1e12):
        raise ValueError("Market data service returned an invalid price.")
    return price


def _timestamp(value):
    return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _payload_error(payload):
    if not isinstance(payload, dict) or payload.get("status") == "error" or payload.get("code"):
        raise ValueError(_provider_message(payload))


def _fetch_series(symbol, key):
    query = urlencode({"symbol": symbol, "interval": HISTORY_INTERVAL, "outputsize": HISTORY_POINTS, "timezone": "UTC", "apikey": key})
    payload = _request_json(f"https://api.twelvedata.com/time_series?{query}")
    _payload_error(payload)
    values = payload.get("values")
    if not isinstance(values, list):
        raise ValueError("Market data service returned an invalid price series.")
    points = []
    for row in reversed(values):
        try:
            points.append({"t": _timestamp(row["datetime"]), "price": _price(row["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        raise ValueError("Market data service returned an empty price series.")
    return points[-1]["price"], points


def _fetch_price(symbol, key):
    query = urlencode({"symbol": symbol, "apikey": key})
    payload = _request_json(f"https://api.twelvedata.com/price?{query}")
    _payload_error(payload)
    try:
        return _price(payload["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Market data service returned an invalid price.") from exc


def _refresh(symbol, key, previous):
    try:
        price, points = _fetch_series(symbol, key)
    except ValueError:
        price = _fetch_price(symbol, key)
        points = list(previous["points"]) if previous else []
    return {"fetched_at": time.monotonic(), "received_at": _utc_now(), "price": price, "points": points[-HISTORY_LIMIT:]}


def _change(points, price):
    if price is None or len(points) < 2 or not points[0]["price"]:
        return None
    base = points[0]["price"]
    return {"absolute": round(price - base, 6), "percent": round((price - base) / base * 100, 4), "since": points[0]["t"]}


def _live_quote(symbol, entry, cached):
    return {
        "symbol": symbol,
        "label": LABELS[symbol],
        "price": entry["price"],
        "status": "live",
        "received_at": entry["received_at"],
        "cached": bool(cached),
        "change": _change(entry["points"], entry["price"]),
        "points": entry["points"],
    }


def _error_quote(symbol, message, cached):
    points = list(cached["points"]) if cached else []
    price = cached["price"] if cached else None
    return {
        "symbol": symbol,
        "label": LABELS[symbol],
        "price": price,
        "status": "stale" if cached else "unavailable",
        "received_at": cached["received_at"] if cached else None,
        "cached": bool(cached),
        "change": _change(points, price),
        "points": points,
        "message": message,
    }


def _blocked(symbol):
    record = _failures.get(symbol)
    return record["message"] if record and record["until"] > time.monotonic() else None


def _next_refresh(cache_seconds):
    waits = []
    now = time.monotonic()
    for symbol in SYMBOLS:
        entry = _cache.get(symbol)
        if not entry or _blocked(symbol):
            return 0
        waits.append(max(0, int(round(cache_seconds - (now - entry["fetched_at"])))))
    return min(waits) if waits else 0


def get_quotes():
    """Return centrally cached, read-only quotes and their recent history."""
    key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    cache_seconds = _cache_seconds()
    as_of = _utc_now()
    if not key:
        return {
            "provider": PROVIDER,
            "configured": False,
            "mode": "read_only",
            "live_execution": False,
            "interval": HISTORY_INTERVAL,
            "cache_seconds": cache_seconds,
            "as_of": as_of,
            "next_refresh_seconds": 0,
            "quotes": [],
            "message": "Set TWELVE_DATA_API_KEY on the server to enable read-only FX prices.",
        }

    quotes = []
    with _lock:
        for symbol in SYMBOLS:
            cached = _cache.get(symbol)
            if cached and time.monotonic() - cached["fetched_at"] < cache_seconds:
                quotes.append(_live_quote(symbol, cached, True))
                continue
            blocked = _blocked(symbol)
            if blocked:
                quotes.append(_error_quote(symbol, blocked, cached))
                continue
            try:
                entry = _refresh(symbol, key, cached)
            except ValueError as exc:
                message = str(exc)
                _failures[symbol] = {"until": time.monotonic() + FAILURE_BACKOFF_SECONDS, "message": message}
                quotes.append(_error_quote(symbol, message, cached))
                continue
            _cache[symbol] = entry
            _failures.pop(symbol, None)
            quotes.append(_live_quote(symbol, entry, False))
        next_refresh = _next_refresh(cache_seconds)

    return {
        "provider": PROVIDER,
        "configured": True,
        "mode": "read_only",
        "live_execution": False,
        "interval": HISTORY_INTERVAL,
        "cache_seconds": cache_seconds,
        "as_of": as_of,
        "next_refresh_seconds": next_refresh,
        "quotes": quotes,
        "message": "Reference prices only. This feed does not execute or confirm trades.",
    }


def get_history():
    """Return the same cached observations shaped for trend charts."""
    payload = get_quotes()
    return {
        "provider": payload["provider"],
        "configured": payload["configured"],
        "mode": payload["mode"],
        "live_execution": payload["live_execution"],
        "interval": payload["interval"],
        "cache_seconds": payload["cache_seconds"],
        "as_of": payload["as_of"],
        "next_refresh_seconds": payload["next_refresh_seconds"],
        "series": [{"symbol": q["symbol"], "label": q["label"], "price": q["price"], "status": q["status"], "points": q["points"]} for q in payload["quotes"]],
        "message": payload["message"],
    }
