# Algo Trader

An automated intraday trading bot for Indian equities (NSE). Scans the entire NSE market at 10 AM, picks the top 2 gaining stocks, enters with 5x intraday leverage, and manages exits with a trailing stop-loss.

All market data and order execution is powered by **Angel One SmartAPI**.

## How It Works

```
09:15  Bot starts, logs into Angel One (auto-generates access token)
10:00  Checks if Nifty 50 is positive (bullish filter)
       Scans all ~2,500 NSE stocks for top 2 gainers
       Fetches available capital from broker account
       Applies 5x intraday leverage
       Buys top 2 stocks, splitting capital equally
10:00–15:15  Monitors positions with trailing stop-loss
       Exits on trailing stop hit or hard 2% max loss
       Stops trading after 2 consecutive losing trades
15:15  Force-closes any remaining positions
```

## Risk Management

| Rule | Detail |
|---|---|
| Trailing stop | 2% below highest price since entry |
| Tightened trail | 1.5% below high after 5% profit |
| Hard max loss | 2% below entry price (never moves, absolute floor) |
| Consecutive loss limit | Stops trading for the day after 2 consecutive losses |
| Intraday leverage | 5x (configurable) |

## Stock Selection Filters

Stocks must pass these filters to be considered as top gainers:

- Price between Rs.50 and Rs.5,000
- Volume above 1,00,000 shares
- Positive percentage change from previous close

## Project Structure

```
main.py                        Entry point — daily trading loop
Stock.py                       Manual stock scanner (no orders)
check_angelone_data.py         Angel One API connectivity test
send_stock_updates.py          Send daily market summary via Telegram

broker/angelone_client.py      Angel One SmartAPI wrapper + auto-login
config/settings.py             Environment variable loader
config/instruments.py          Stock instrument definitions + Excel import
data/market_stream.py          OHLCV data fetcher (Angel One)
strategy/momentum_strategy.py  EMA + VWAP momentum signal generator
strategy/market_scanner.py     Full-market scanner + Nifty trend check
execution/order_manager.py     Order construction + placement
risk/risk_manager.py           Position sizing
monitor/position_tracker.py    Position tracking + trailing stop logic
utils/logger.py                Console + file logging
utils/telegram_alert.py        Telegram notifications
deploy/algo-trader.service     systemd service file for EC2
tests/                         Unit tests
```

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Angel One credentials
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_PIN=your_pin
ANGELONE_TOTP_SECRET=your_totp_secret

# Runtime settings
SCAN_INTERVAL_SECONDS=300
ALERT_EVERY_CHECK=false
ORDER_PRODUCT_TYPE=INTRADAY
ORDER_VARIETY=NORMAL

# Risk settings
CAPITAL=100000
RISK_PER_TRADE_PCT=1.0
INTRADAY_LEVERAGE=5.0
MAX_CONSECUTIVE_LOSSES=2

# Telegram alerts (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 3. Verify Angel One connection

```bash
python3 check_angelone_data.py --symbol SBIN.NS
```

### 4. Run the bot

```bash
python3 main.py
```

## Angel One SmartAPI App Setup

1. Go to [smartapi.angelone.in](https://smartapi.angelone.in/)
2. Create an app with:
   - **Redirect URL**: `http://localhost`
   - **Primary Static IP**: the public IP of the machine running the bot
3. Copy the API key into `.env`

The bot auto-generates access tokens at startup using your PIN and TOTP secret. No manual token refresh needed.

## EC2 Deployment

```bash
# Install dependencies
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# Clone and setup
git clone <your-repo-url>
cd algo-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials

# Test
python3 check_angelone_data.py --symbol SBIN.NS

# Install as service
sudo cp deploy/algo-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader

# Monitor
sudo systemctl status algo-trader
tail -f algo_trader.log
```

Attach an **Elastic IP** to your EC2 instance and use it as the Primary Static IP in your SmartAPI app.

## Other Scripts

**Manual stock scanner** (no orders placed):
```bash
python3 Stock.py
python3 Stock.py --excel-path holdings.xlsx
```

**Daily Telegram market summary**:
```bash
python3 send_stock_updates.py SBIN INFY RELIANCE
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Disclaimer

This project is for educational purposes only. Live trading involves real financial risk. Use at your own discretion with proper safeguards.
