from data.market_stream import MarketStream
from utils.telegram_alert import send_telegram_message

NSE_SYMBOLS = [
    "RELIANCE.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "TCS.NS",
    "SBIN.NS",
    "AXISBANK.NS",
    "ITC.NS",
    "LT.NS",
    "HDFC.NS",
    "KOTAKBANK.NS",
    "IBULHSGFIN.NS",
    "BHARTIARTL.NS",
    "HCLTECH.NS",
    "MARUTI.NS",
    "BAJFINANCE.NS",
    "ONGC.NS",
    "SUNPHARMA.NS",
    "WIPRO.NS",
    "TITAN.NS",
]

# BSE symbols must be from yfinance BSE format, e.g., 500112.BO is TCS on BSE.
BSE_SYMBOLS = [
    "500325.BO",
    "532540.BO",
    "500209.BO",
    "500112.BO",
    "532454.BO",
    "532174.BO",
    "532789.BO",
    "532978.BO",
    "500312.BO",
    "532215.BO",
    "500111.BO",
    "532540.BO",
    "532281.BO",
    "500800.BO",
    "500460.BO",
    "500182.BO",
    "500800.BO",
    "531325.BO",
    "500010.BO",
    "532540.BO",
]


def _format_gainers(gainers: list[dict]) -> str:
    if not gainers:
        return "No gainers found or data not available."

    lines = ["Top gainers:\n"]
    for idx, g in enumerate(gainers, start=1):
        lines.append(
            f"{idx}. {g['symbol']} | {g['last_close']:.2f} | {g['prev_close']:.2f} | +{g['pct_change']:.2f}%"
        )
    return "\n".join(lines)


def send_top_gainers(exchange: str = "NSE", limit: int = 10) -> None:
    stream = MarketStream()
    symbols = NSE_SYMBOLS if exchange.upper() == "NSE" else BSE_SYMBOLS

    gainers = stream.top_gainers(symbols, limit=limit)
    message = f"{exchange.upper()} top {limit} gainers today:\n" + _format_gainers(gainers)

    sent = send_telegram_message(message)
    if not sent:
        print("Telegram message not sent; check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    else:
        print(f"Sent {exchange.upper()} top gainers to Telegram.")


if __name__ == "__main__":
    send_top_gainers("NSE", limit=10)
    send_top_gainers("BSE", limit=10)
