from __future__ import annotations

import time
from typing import Any

from broker.angelone_client import AngelOneClient


# Nifty 50 index token on NSE.
NIFTY_50_TOKEN = "99926000"

# Angel One batch quote limit.
BATCH_SIZE = 50

# Rate-limit delay between batch requests (seconds).
BATCH_DELAY = 0.3


def is_market_bullish(client: AngelOneClient) -> tuple[bool, float]:
    """Check if Nifty 50 is in a positive trend today.

    Returns (is_bullish, nifty_change_pct).
    """
    result = client.get_market_data("FULL", {"NSE": [NIFTY_50_TOKEN]})
    fetched = result.get("fetched", [])
    if not fetched:
        return False, 0.0

    nifty = fetched[0]
    ltp = float(nifty.get("ltp", 0))
    prev_close = float(nifty.get("close", 0))

    if prev_close <= 0:
        return False, 0.0

    change_pct = ((ltp - prev_close) / prev_close) * 100
    return change_pct > 0, round(change_pct, 2)


def load_nse_equity_tokens(client: AngelOneClient) -> list[dict[str, str]]:
    """Load all NSE equity stock tokens from the scrip master.

    Returns list of {symbol, token, name} dicts.
    """
    master = client._load_scrip_master()
    stocks = []
    for row in master:
        if row.get("exch_seg") != "NSE":
            continue
        symbol = str(row.get("symbol", ""))
        if not symbol.endswith("-EQ"):
            continue
        if "INAV" in symbol:  # Skip NAV-tracking instruments.
            continue
        stocks.append({
            "symbol": symbol,
            "token": str(row.get("token", "")),
            "name": str(row.get("name", "")),
        })
    return stocks


def scan_top_gainers(
    client: AngelOneClient,
    stocks: list[dict[str, str]],
    top_n: int = 2,
    min_price: float = 50.0,
    max_price: float = 5000.0,
    min_volume: int = 100000,
) -> list[dict[str, Any]]:
    """Scan all NSE stocks and return the top N gainers by percentage change.

    Filters:
    - Price between min_price and max_price (avoids penny stocks and very expensive stocks).
    - Volume above min_volume (ensures liquidity for entry/exit).
    - Positive percentage change only.
    """
    all_tokens = [s["token"] for s in stocks]
    token_to_stock = {s["token"]: s for s in stocks}

    all_quotes: list[dict[str, Any]] = []
    for i in range(0, len(all_tokens), BATCH_SIZE):
        batch = all_tokens[i : i + BATCH_SIZE]
        try:
            result = client.get_market_data("FULL", {"NSE": batch})
            fetched = result.get("fetched", [])
            all_quotes.extend(fetched)
        except Exception:
            pass  # Skip failed batches; partial data is acceptable for scanning.
        if i + BATCH_SIZE < len(all_tokens):
            time.sleep(BATCH_DELAY)

    # Filter and rank.
    candidates: list[dict[str, Any]] = []
    for q in all_quotes:
        ltp = float(q.get("ltp", 0))
        prev_close = float(q.get("close", 0))
        volume = int(float(q.get("tradeVolume", 0)))
        token = str(q.get("symbolToken", ""))

        if prev_close <= 0 or ltp <= 0:
            continue
        if ltp < min_price or ltp > max_price:
            continue
        if volume < min_volume:
            continue

        pct_change = ((ltp - prev_close) / prev_close) * 100
        if pct_change <= 0:
            continue

        stock_info = token_to_stock.get(token, {})
        candidates.append({
            "symbol": stock_info.get("symbol", q.get("tradingSymbol", "")),
            "token": token,
            "name": stock_info.get("name", ""),
            "ltp": ltp,
            "prev_close": prev_close,
            "open": float(q.get("open", 0)),
            "high": float(q.get("high", 0)),
            "low": float(q.get("low", 0)),
            "volume": volume,
            "pct_change": round(pct_change, 2),
        })

    # Sort by percentage change descending, return top N.
    candidates.sort(key=lambda x: x["pct_change"], reverse=True)
    return candidates[:top_n]
