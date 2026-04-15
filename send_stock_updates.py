from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd

from broker.angelone_client import AngelOneClient
from config.instruments import default_watchlist, project_symbol_for, symbols_from_xlsx
from config.settings import Settings, load_settings
from data.market_stream import MarketStream
from utils.telegram_alert import send_telegram_message


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a Telegram update with the latest daily market data for selected stocks."
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        help="Stock symbols like SBIN, INFY.NS, or RELIANCE.NS. Defaults to the watchlist when omitted.",
    )
    parser.add_argument(
        "--provider",
        choices=("yfinance", "angelone"),
        default="yfinance",
        help="Market-data source to use for the summary. Defaults to yfinance for easy read-only usage.",
    )
    parser.add_argument(
        "--period",
        default="5d",
        help="History window used to compute the latest day and previous close.",
    )
    parser.add_argument(
        "--excel-path",
        help="Optional xlsx file whose Equity sheet symbols should be used for the update.",
    )
    return parser.parse_args(argv)


def _expand_symbol_tokens(raw_symbols: Sequence[str]) -> list[str]:
    tokens: list[str] = []
    for raw in raw_symbols:
        tokens.extend(part for part in raw.replace(",", " ").split() if part)
    return tokens


def normalize_symbol(symbol: str) -> str:
    return project_symbol_for(symbol)


def resolve_requested_symbols(
    raw_symbols: Sequence[str],
    excel_path: str | Path | None = None,
) -> list[str]:
    tokens = _expand_symbol_tokens(raw_symbols)
    if tokens:
        symbols = tokens
    elif excel_path:
        symbols = symbols_from_xlsx(excel_path)
    else:
        symbols = [instrument.symbol for instrument in default_watchlist()]

    unique_symbols: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_symbols.append(normalized)
    return unique_symbols


def _build_market_stream(provider: str, period: str, settings: Settings) -> MarketStream:
    if provider == "angelone":
        missing = [
            name
            for name, value in (
                ("ANGELONE_API_KEY", settings.api_key),
                ("ANGELONE_CLIENT_ID", settings.client_id),
                ("ANGELONE_ACCESS_TOKEN", settings.access_token),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Angel One market data requires these settings: " + ", ".join(missing)
            )
        return MarketStream(
            interval="1d",
            period=period,
            data_provider="angelone",
            angel_client=AngelOneClient(
                api_key=settings.api_key,
                client_id=settings.client_id,
                access_token=settings.access_token,
            ),
        )

    return MarketStream(interval="1d", period=period, data_provider="yfinance")


def _snapshot_from_frame(symbol: str, frame: pd.DataFrame) -> dict[str, object]:
    latest = frame.iloc[-1]
    previous_close = float(frame.iloc[-2]["Close"]) if len(frame) > 1 else None
    close_price = float(latest["Close"])
    change_pct = None
    if previous_close not in (None, 0.0):
        change_pct = ((close_price - previous_close) / previous_close) * 100

    as_of = frame.index[-1]
    as_of_text = as_of.date().isoformat() if hasattr(as_of, "date") else str(as_of)
    return {
        "symbol": symbol,
        "as_of": as_of_text,
        "open": float(latest["Open"]),
        "high": float(latest["High"]),
        "low": float(latest["Low"]),
        "close": close_price,
        "previous_close": previous_close,
        "change_pct": change_pct,
        "volume": int(float(latest["Volume"])),
    }


def collect_daily_snapshots(
    stream: MarketStream,
    symbols: Sequence[str],
) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
    snapshots: list[dict[str, object]] = []
    failures: list[tuple[str, str]] = []

    for symbol in symbols:
        try:
            frame = stream.fetch_ohlcv(symbol)
            if frame.empty:
                raise ValueError("No market data returned")
            snapshots.append(_snapshot_from_frame(symbol, frame))
        except Exception as exc:
            failures.append((symbol, str(exc)))

    return snapshots, failures


def build_telegram_message(
    snapshots: Sequence[dict[str, object]],
    failures: Sequence[tuple[str, str]] = (),
) -> str:
    if not snapshots:
        raise ValueError("No stock snapshots available to send")

    report_date = max(str(snapshot["as_of"]) for snapshot in snapshots)
    lines = [f"Stock market update for {report_date}"]

    for snapshot in snapshots:
        change_pct = snapshot["change_pct"]
        change_text = "n/a"
        if isinstance(change_pct, (int, float)):
            change_text = f"{change_pct:+.2f}%"
        lines.append(
            f"{snapshot['symbol']}: Close {snapshot['close']:.2f} ({change_text})"
        )
        lines.append(
            f"O {snapshot['open']:.2f} H {snapshot['high']:.2f} L {snapshot['low']:.2f} V {int(snapshot['volume']):,}"
        )

    if failures:
        lines.append("")
        lines.append("Skipped symbols:")
        for symbol, reason in failures:
            lines.append(f"{symbol}: {reason}")

    return "\n".join(lines)


def send_market_update(
    raw_symbols: Sequence[str],
    provider: str = "yfinance",
    period: str = "5d",
    excel_path: str | Path | None = None,
) -> tuple[bool, str]:
    settings = load_settings()
    symbols = resolve_requested_symbols(raw_symbols, excel_path=excel_path)
    stream = _build_market_stream(provider=provider, period=period, settings=settings)
    snapshots, failures = collect_daily_snapshots(stream=stream, symbols=symbols)
    if not snapshots:
        failure_summary = ", ".join(f"{symbol}: {reason}" for symbol, reason in failures) or "unknown error"
        raise ValueError(f"No market data could be fetched. {failure_summary}")

    message = build_telegram_message(snapshots=snapshots, failures=failures)
    return send_telegram_message(message), message


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        delivered, message = send_market_update(
            raw_symbols=args.symbols,
            provider=args.provider,
            period=args.period,
            excel_path=args.excel_path,
        )
    except Exception as exc:
        print(f"Result: FAIL - {exc}", file=sys.stderr)
        return 1

    print(message)
    if delivered:
        print("\nResult: PASS - Telegram message sent.")
        return 0

    print(
        "\nResult: FAIL - Telegram message was not sent. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
