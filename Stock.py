import yfinance as yf
import pandas as pd
from ta.trend import EMAIndicator

stocks = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "TCS.NS", "SBIN.NS", "AXISBANK.NS", "ITC.NS",
    "LT.NS", "JUBLFOOD.NS"
]

# ---------------- VWAP ----------------
def calculate_vwap(df):
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    return (typical_price * df['Volume']).cumsum() / df['Volume'].cumsum()

# ---------------- Scanner ----------------
def scan_stock(symbol):
    try:
        df = yf.download(symbol, interval="5m", period="1d", progress=False)

        if df.empty:
            return None

        # ✅ FORCE CONVERT TO SERIES (MAIN FIX)
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = pd.Series(df[col].values.flatten(), index=df.index)

        # EMA
        df['EMA20'] = EMAIndicator(df['Close'], window=20).ema_indicator()
        df['EMA50'] = EMAIndicator(df['Close'], window=50).ema_indicator()

        # VWAP
        df['VWAP'] = calculate_vwap(df)

        latest = df.iloc[-1]

        avg_volume = df['Volume'].rolling(10).mean().iloc[-1]
        day_high = df['High'].max()

        if (
            latest['Close'] > latest['VWAP']
            and latest['Close'] > latest['EMA20']
            and latest['Close'] > latest['EMA50']
            and latest['Volume'] > avg_volume
            and latest['Close'] >= 0.995 * day_high
        ):
            return {
                "Stock": symbol,
                "Price": round(latest['Close'], 2),
                "VWAP": round(latest['VWAP'], 2),
                "Volume": int(latest['Volume'])
            }

    except Exception as e:
        print(f"Error scanning {symbol}: {e}")

    return None


# ---------------- RUN ----------------
results = []

for stock in stocks:
    res = scan_stock(stock)
    if res:
        results.append(res)

if results:
    print("\n🔥 Uptrend Intraday Stocks Found:\n")
    print(pd.DataFrame(results))
else:
    print("\nNo strong uptrend stocks found currently.")
