from __future__ import annotations

import argparse
import sys
from typing import Sequence

from broker.angelone_client import AngelOneClient
from config.instruments import Instrument, default_watchlist
from config.settings import load_settings
from data.market_stream import MarketStream


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a read-only Angel One market-data connectivity check."
    )
    parser.add_argument(
        "--symbol",
        default=default_watchlist()[0].symbol,
        help="Project symbol to resolve and test, for example RELIANCE.NS.",
    )
    parser.add_argument(
        "--exchange",
        default="NSE",
        help="Angel One exchange segment to query.",
    )
    parser.add_argument(
        "--interval",
        default="5m",
        help="Local interval to request through MarketStream, for example 1m, 5m, or 15m.",
    )
    parser.add_argument(
        "--period",
        default="1d",
        help="Local lookback period to request through MarketStream, for example 1d or 2d.",
    )
    parser.add_argument(
        "--quote-mode",
        default="LTP",
        help="Angel One market quote mode to use for the batch quote check.",
    )
    return parser.parse_args(argv)


def _validate_settings() -> tuple[str, str, str, str]:
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("ANGELONE_API_KEY", settings.api_key),
            ("ANGELONE_CLIENT_ID", settings.client_id),
            ("ANGELONE_PIN", settings.pin),
            ("ANGELONE_TOTP_SECRET", settings.totp_secret),
        )
        if not value
    ]
    if missing:
        missing_list = ", ".join(missing)
        raise ValueError(
            f"Missing required Angel One settings: {missing_list}. Create a .env file or export the variables before running this check."
        )
    return settings.api_key, settings.client_id, settings.pin, settings.totp_secret


def _build_client() -> AngelOneClient:
    api_key, client_id, pin, totp_secret = _validate_settings()
    return AngelOneClient.login(
        api_key=api_key,
        client_id=client_id,
        pin=pin,
        totp_secret=totp_secret,
    )


def run_check(symbol: str, exchange: str, interval: str, period: str, quote_mode: str) -> int:
    print("Angel One data connectivity check")
    print(f"Symbol: {symbol}")
    print(f"Exchange: {exchange}")
    print(f"Interval: {interval}")
    print(f"Period: {period}")
    print("")

    client = _build_client()
    instrument = Instrument(symbol=symbol, exchange=exchange)
    stream = MarketStream(angel_client=client, interval=interval, period=period)

    resolved = stream.resolve_instrument(instrument)
    print("Instrument resolution: OK")
    print(f"Tradingsymbol: {resolved.tradingsymbol}")
    print(f"Symbol token: {resolved.symboltoken}")
    print("")

    ltp = client.get_ltp_data(
        exchange=resolved.exchange,
        tradingsymbol=resolved.tradingsymbol or "",
        symboltoken=resolved.symboltoken or "",
    )
    print("LTP check: OK")
    print(f"LTP payload keys: {', '.join(sorted(ltp.keys())) or '<empty>'}")
    if "ltp" in ltp:
        print(f"Last traded price: {ltp['ltp']}")
    print("")

    quote = client.get_market_data(
        mode=quote_mode,
        exchange_tokens={resolved.exchange: [resolved.symboltoken or ""]},
    )
    print("Batch quote check: OK")
    if isinstance(quote, dict):
        print(f"Quote payload keys: {', '.join(sorted(quote.keys())) or '<empty>'}")
    else:
        print(f"Quote payload type: {type(quote).__name__}")
    print("")

    candles = stream.fetch_ohlcv(resolved)
    print("Historical candle check: OK")
    print(f"Candle rows: {len(candles)}")
    if not candles.empty:
        latest = candles.iloc[-1]
        print(f"Latest candle time: {candles.index[-1]}")
        print(
            "Latest candle OHLCV: "
            f"O={latest['Open']} H={latest['High']} L={latest['Low']} C={latest['Close']} V={latest['Volume']}"
        )
    else:
        print("No candles returned for the requested window.")
    print("")
    print("Result: PASS")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        return run_check(
            symbol=args.symbol,
            exchange=args.exchange,
            interval=args.interval,
            period=args.period,
            quote_mode=args.quote_mode,
        )
    except Exception as exc:
        print(f"Result: FAIL - {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
