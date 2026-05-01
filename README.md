# Algo Trader — Intraday Trading Bot for Indian Stock Market

Automated intraday trading bot that scans the entire NSE equity market, selects the best momentum stocks, and trades them with trailing stop-loss protection. Uses Angel One SmartAPI as the sole broker and data source.

## How It Works (Daily Flow)

```
09:15  Market opens. Bot logs in via auto-TOTP.
09:45  Phase 0 — Daily housekeeping:
         • Clear daily candle cache (fresh data for new day).
         • Refresh session if stale (auto re-login every 2 hours).
         • Initialize daily P&L tracker (stops new trades if loss > 5% of capital).
09:45  Phase 1 — Scan window opens.
09:45  Phase 2 — 4-factor market bullishness check (3-of-4 must pass):
         • Index Direction — Nifty 50 change > 0%
         • Market Breadth — Nifty 50 advancers/decliners > 1.2
         • Intraday Strength — Nifty LTP > Open AND NIFTYBEES > VWAP
         • Volatility Filter — India VIX change < +5%
         If score < 3/4 → skip the day.
09:45  Phase 3 — Scan all ~2500 NSE equity stocks:
         3a. Fetch FULL quotes in parallel batches of 50 (3 concurrent workers).
         3b. Filter to 5–10% intraday gainers (price ₹50–₹5000, volume > 1 lakh).
         3c. Fetch 5-day candles for candidates (cached across scan retries).
         3d. Filter out ASM/GSM/T2T stocks (NSE lists + circuit heuristic).
         3e. Probe remaining candidates with test orders (cached per session).
         3f. Exclude any stock already traded today.
09:46  Phase 4 — Real-time entry validation (5 checks, batch + parallel):
         4a. Check daily P&L limit — skip if loss > 5% of capital.
         4b. Batch-fetch FULL quotes for all candidates (1 API call).
         4c. Check momentum (5–10%), range position (≥0.85), spread (<0.2%).
         4d. Parallel candle checks: micro breakout + volume confirmation.
         4e. If no candidates pass → wait 2 min → retry from Phase 3.
         4f. Retry loop continues until valid entries found or 12:30 PM.
~10:00 Phase 5 — Enter top 2 stocks with MARKET orders + server-side SL-M backup.
         Capital allocation: 1 stock = 50% buying power, 2 stocks = 50% each.
~10:00 Phase 6 — Monitor every 10 seconds using FULL quotes (LTP + best bid).
         • Trail stop-loss upward based on LTP.
         • Compare best bid price against stop-loss for exit decisions.
         • Before software exit: check if SL-M already executed (prevents double-sell).
         • Exit orders use safe_exit() with 3× retry + Telegram escalation on failure.
         • Refresh session every 2 hours during monitoring.
         If all positions close → 15 min cooldown → re-check market → re-scan → re-enter.
         Up to 3 re-entry rounds (3 scan attempts each). Never re-enter the same stock.
15:15  Phase 7 — Force-close remaining positions (SL-M race check + safe_exit).
         Daily P&L summary sent via Telegram.
```

## Market Bullishness — 4-Factor Model

Before scanning for stocks, the bot checks if the overall market is favorable. At least 3 of 4 factors must pass (same scoring approach as a quorum vote):

| Factor | Condition | Data Source |
|---|---|---|
| Index Direction | Nifty 50 change > 0% | Nifty 50 index quote |
| Market Breadth | Advancers/Decliners > 1.2 among Nifty 50 constituents | Batch quote of all 50 constituent tokens |
| Intraday Strength | Nifty LTP > Open AND NIFTYBEES ETF > VWAP | Nifty index + NIFTYBEES (token 10576) avgPrice |
| Volatility Filter | India VIX change < +5% | India VIX (token 99926017) |

**Holiday detection**: If Nifty `tradeVolume == 0` and `exchFeedTime` is not today, the market is closed (holiday). The bot skips the day without placing any trades.

## Entry Validation — 5 Real-Time Checks

After the scanner picks candidates, each must pass 5 checks using live data fetched immediately before order placement. This prevents entering on stale scan-time prices.

| Check | Condition | Purpose |
|---|---|---|
| 1. Momentum | Gain still 5–10% from prev close | Reject if momentum faded or overextended since scan |
| 2. Range Position | LTP in top 15% of day's range (≥ 0.85) | Confirm price is near high, not retreating |
| 3. Micro Breakout | LTP > last completed 5-min candle high | Confirm active upward movement |
| 4. Volume | Current 1-min volume ≥ 1.2× avg of prior 5 mins | Confirm buying interest, not fading |
| 5. Spread | Bid-ask spread < 0.2% of price | Ensure liquidity for clean entry |

**Batch validation**: Checks 1, 2, 5 run from a single batch FULL quote fetch (1 API call for all candidates). Checks 3, 4 run in parallel threads (one per candidate). This reduces validation from ~15 seconds sequential to ~3-5 seconds.

If no candidates pass all 5 checks, the bot waits 2 minutes and rescans. This retry loop continues until 12:30 PM.

## Stock Selection — Multi-Factor Scoring

Stocks must pass these filters before scoring:

| Filter | Value | Why |
|---|---|---|
| Intraday gain | 5–10% | Sweet spot: strong momentum without being overextended |
| Price range | ₹50–₹5,000 | Avoids penny stocks and illiquid expensive stocks |
| Volume | > 1,00,000 | Ensures liquidity for entry and exit |

Qualifying stocks are scored on 5 factors:

| Factor | Weight | What it measures |
|---|---|---|
| Relative Volume | 25% | Today's volume ÷ average of previous days. 2x avg volume = max score. |
| Momentum | 25% | Where LTP sits in today's range. Near day high = strong momentum. |
| Buy Pressure | 20% | Buy quantity ÷ (buy + sell) in order book. More buyers = bullish. |
| Stability | 15% | How much the stock gapped up at open. Gradual rise preferred over gap-up. |
| Previous Day Trend | 15% | How many of the last 3–5 days closed higher. Confirms multi-day uptrend. |

**Composite score** = weighted sum of all 5 factors (0.0 to 1.0). Top 2 by score are selected.

### Example Score Breakdown

```
CONCORDBIO-EQ: +5.79% @ ₹1134.10
  Score=0.605 | Vol=1.0x | Momentum=0.92 | BuyPressure=0.53 | Stability=0.96 | PrevTrend=0.00
```

This stock scored high on momentum (price near day high) and stability (no gap-up), but low on volume (1x average) and previous trend (no multi-day uptrend).

## Risk Management

### Three-Layer Stop-Loss System

```
Layer 1: Hard Stop (exchange-enforced)
  └─ SL-M order placed on the exchange at entry_price × 0.98
  └─ Triggers automatically — no polling needed
  └─ Guarantees max 2% loss per stock

Layer 2: Trailing Stop (software, 10-second polling)
  └─ Tracks highest price since entry
  └─ Stop-loss = highest_price × (1 - trail_pct)
  └─ Only moves UP, never down

Layer 3: 15% Profit Lock
  └─ Once gain crosses 15%, stop-loss locks at entry_price × 1.15
  └─ Guarantees at least 15% profit regardless of subsequent price action
```

### Trailing Stop Tightening

| Profit from entry | Trail % | Stop-loss level |
|---|---|---|
| 0–5% | 2.0% | `highest_price × 0.98` |
| 5–15% | 1.5% | `highest_price × 0.985` (tighter) |
| 15%+ | Locked | `entry_price × 1.15` (fixed, guaranteed profit) |

### Example: Entry at ₹100

```
Price rises to ₹103 → SL = ₹100.94 (2% trail)
Price rises to ₹106 → SL = ₹104.41 (1.5% trail, tightened at 5% profit)
Price rises to ₹115 → SL = ₹115.00 (LOCKED at +15%)
Price rises to ₹125 → SL = ₹115.00 (still locked — guaranteed ₹15 profit)
Price drops to ₹115 → EXIT at ₹115.00 (PnL = +₹15 per share)
```

### Daily Loss Limits

| Control | Value | Behavior |
|---|---|---|
| Daily P&L limit | 5% of capital | If cumulative daily loss exceeds 5%, stop all new trades. Existing positions still monitored. Critical Telegram alert sent. |
| Max consecutive losses | 2 | After 2 losing trades in a row, stop trading for the day |
| Hard stop per stock | 2% | SL-M on exchange at entry × 0.98 — max loss per position |
| Max re-entry rounds | 3 | After all positions close, can re-scan up to 3 more times |
| Re-entry cooldown | 15 min | Wait 15 minutes between rounds to avoid churning |
| No repeat stocks | — | Never re-enter a stock already traded today |

### Capital Allocation

```
Available capital (from broker RMS API)
  × Intraday leverage (default 5x)
  = Total buying power

1 validated stock → 50% of buying power (never 100% on one stock)
2 validated stocks → 50% each (split equally)

Example: ₹1,00,000 × 5x = ₹5,00,000 buying power
  1 stock → ₹2,50,000 allocated
  2 stocks → ₹2,50,000 each
```

## Tradability Filter — 4 Layers

Angel One rejects orders for certain stocks. The bot pre-filters to avoid wasted API calls and failed entries.

```
Layer 1: NSE ASM List (~256 stocks)
  └─ Fetched from nseindia.com/api/reportASM at startup
  └─ Additional Surveillance Measure — exchange-restricted

Layer 2: NSE GSM List (~53 stocks)
  └─ Fetched from nseindia.com/api/reportGSM at startup
  └─ Graded Surveillance Measure — exchange-restricted

Layer 3: Circuit Limit Heuristic
  └─ If both upper and lower circuit bands are ≤5% → likely T2T/BE stock
  └─ These stocks cannot be traded intraday

Layer 4: Broker Probe (Angel One cautionary list)
  └─ Angel One maintains its own internal blocklist (broader than NSE)
  └─ No public API to check — only way is to test
  └─ Bot places a LIMIT BUY for 1 share at ₹0.05 (below min tick — prevents accidental fills)
  └─ If accepted → cancel immediately (3× retry on cancel failure) → stock is tradable
  └─ If cancel fails after retries → critical Telegram alert sent
  └─ If rejected with "cautionary" → blacklist for the session
  └─ Results cached per session — same stock is never probed twice
  └─ Runs concurrently (4 threads) — takes ~2-5 seconds for 10 candidates
```

### Optional: F&O-Only Mode

Set `FNO_ONLY=true` to restrict trading to the ~213 stocks in NSE's F&O universe. These are the most liquid large-caps. However, most 5–10% intraday gainers are small/mid-caps outside F&O, so this mode may find zero candidates on most days.

## Order Execution

### Entry Orders

1. **MARKET order** (default) — fastest fill
2. If MARKET is rejected (non-tradability reason) → retry with **LIMIT order** at current price
3. If rejected for tradability (cautionary/ASM/GSM) → raise `OrderRejectedError` → try next candidate from fallback queue

### Exit Orders

All exits go through `_safe_exit()` which guarantees delivery or escalation:

1. **MARKET order** — must close position regardless of restrictions
2. If MARKET fails → retry with **LIMIT order** at current price
3. Retries up to 3 times with 1-second delay between attempts
4. If all 3 attempts fail → sends critical Telegram alert: "EXIT FAILED — MANUAL INTERVENTION REQUIRED" with symbol, quantity, and action instructions
5. Exit orders skip tradability checks (you must be able to sell what you own)

### Server-Side SL-M (Stop-Loss Market)

After each entry, the bot places a SL-M SELL order on the exchange:

- **On entry**: SL-M at hard stop (entry × 0.98)
- **As trailing stop moves up**: Modifies the SL-M trigger price to match
- **On software exit**: Checks order book first — if SL-M is already `COMPLETE`/`TRIGGERED`, skips the software sell (prevents double-sell / accidental short). Otherwise cancels the SL-M, then places the exit via `_safe_exit()`.
- **On market close**: Same SL-M race check before force-closing each position.

This ensures the exchange enforces the stop-loss even if the bot crashes, loses network, or the polling interval misses a flash crash. The race condition check prevents the bot from selling shares that the exchange already sold.

## Re-Entry Logic

After all positions close via stop-loss:

```
1. Wait 15 minutes (cooldown — avoids excessive trade charges)
2. Check daily P&L limit — if loss > 5% of capital → stop for the day
3. Re-check market bullishness (full 4-factor model) — if < 3/4 → stop for the day
4. Re-scan all NSE stocks for fresh top gainers
5. Validate entries with live data (5 checks)
6. If no valid entries → retry up to 3 times (60s apart) before giving up
7. Exclude all stocks already traded today
8. Enter new positions and monitor again
9. Repeat up to 3 times (MAX_REENTRY_ROUNDS)
```

**Stops re-entering when:**
- Market closes (3:15 PM)
- 2 consecutive losses reached
- Daily P&L loss exceeds 5% of capital
- 3 re-entry rounds exhausted
- Market turns bearish between rounds (4-factor check)
- No new tradable candidates found after 3 scan attempts

## Authentication

Auto-login using PIN + TOTP with automatic session management:

```
1. Generate TOTP code from secret using pyotp
2. POST to /rest/auth/angelone/user/v1/loginByPassword
3. Receive JWT access token
4. Token used for all subsequent API calls
5. Session auto-refreshes every 2 hours (refresh_if_stale)
6. On 401/403 errors: auto re-login and retry the failed request
7. All API calls have 3× retry with exponential backoff
8. Rate limited to ~8 requests/second to avoid Angel One throttling
```

## Configuration (.env)

```env
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_PIN=your_pin
ANGELONE_TOTP_SECRET=your_totp_secret
MONITOR_INTERVAL_SECONDS=10
ORDER_PRODUCT_TYPE=INTRADAY
ORDER_VARIETY=NORMAL
CAPITAL=100000
INTRADAY_LEVERAGE=5.0
MAX_CONSECUTIVE_LOSSES=2
SAFE_MODE=true
FNO_ONLY=false
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

**Do not use inline comments in `.env`** — systemd's `EnvironmentFile` treats everything after `=` as the value.

| Variable | Default | Description |
|---|---|---|
| `ANGELONE_API_KEY` | — | SmartAPI app key from smartapi.angelone.in |
| `ANGELONE_CLIENT_ID` | — | Angel One trading account ID |
| `ANGELONE_PIN` | — | 4-digit trading PIN |
| `ANGELONE_TOTP_SECRET` | — | Base32 TOTP secret for auto-login |
| `MONITOR_INTERVAL_SECONDS` | 10 | Seconds between price checks during monitoring |
| `ORDER_PRODUCT_TYPE` | INTRADAY | MIS (auto square-off by broker at 3:15 PM) |
| `ORDER_VARIETY` | NORMAL | Regular order variety |
| `CAPITAL` | 100000 | Fallback capital if broker RMS API fails |
| `INTRADAY_LEVERAGE` | 5.0 | Margin multiplier (5x = ₹1000 buys ₹5000 worth) |
| `MAX_CONSECUTIVE_LOSSES` | 2 | Stop trading after N consecutive losing trades |
| `SAFE_MODE` | true | Enable ASM/GSM/T2T pre-filtering |
| `FNO_ONLY` | false | Restrict to F&O universe only (~213 stocks) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for alerts |

## Hardcoded Constants (in source code)

These are not configurable via `.env` — change them in the source files:

| Constant | File | Value | Description |
|---|---|---|---|
| `SCAN_START_HOUR, SCAN_START_MIN` | `main.py` | 9:45 | Scan window opens |
| `SCAN_END_HOUR, SCAN_END_MIN` | `main.py` | 12:30 | Scan window closes (stop retrying) |
| `SCAN_RETRY_SECONDS` | `main.py` | 120 | Seconds between scan retry attempts |
| `MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN` | `main.py` | 15:15 | When to force-close all positions |
| `TOP_N` | `main.py` | 2 | Number of stocks to trade simultaneously |
| `MAX_REENTRY_ROUNDS` | `main.py` | 3 | Max re-scan rounds after stop-loss exits |
| `REENTRY_COOLDOWN_MINUTES` | `main.py` | 15 | Minutes to wait between re-entry rounds |
| `REENTRY_SCAN_ATTEMPTS` | `main.py` | 3 | Scan+validate attempts per re-entry round |
| `REENTRY_SCAN_DELAY` | `main.py` | 60s | Seconds between re-entry scan attempts |
| `MAX_DAILY_LOSS_PCT` | `main.py` | 5% | Stop new trades if daily loss exceeds this |
| `_EXIT_MAX_RETRIES` | `main.py` | 3 | Max retries for exit orders before alert |
| `DEFAULT_TRAIL_PCT` | `position_tracker.py` | 2% | Default trailing stop distance |
| `TIGHT_TRAIL_PCT` | `position_tracker.py` | 1.5% | Tighter trail after 5% profit |
| `TIGHT_TRAIL_PROFIT_THRESHOLD` | `position_tracker.py` | 5% | Profit level to tighten trail |
| `LOCK_PROFIT_THRESHOLD` | `position_tracker.py` | 15% | Profit level to lock stop-loss |
| `MAX_LOSS_PCT` | `position_tracker.py` | 2% | Hard max loss per stock |
| `MIN_GAIN_PCT` | `market_scanner.py` | 5% | Minimum intraday gain to consider |
| `MAX_GAIN_PCT` | `market_scanner.py` | 10% | Maximum intraday gain to consider |
| `MIN_PRICE` | `market_scanner.py` | ₹50 | Minimum stock price |
| `MAX_PRICE` | `market_scanner.py` | ₹5,000 | Maximum stock price |
| `MIN_VOLUME` | `market_scanner.py` | 1,00,000 | Minimum intraday volume |
| `BATCH_SIZE` | `market_scanner.py` | 50 | Angel One quote batch limit |
| `MIN_GAIN_PCT` | `entry_validator.py` | 5% | Minimum gain for entry validation |
| `MAX_GAIN_PCT` | `entry_validator.py` | 10% | Maximum gain for entry validation |
| `MIN_RANGE_POSITION` | `entry_validator.py` | 0.85 | Price must be in top 15% of day's range |
| `MIN_VOLUME_RATIO` | `entry_validator.py` | 1.2 | Current volume ≥ 1.2× avg of prior candles |
| `MAX_SPREAD_PCT` | `entry_validator.py` | 0.2% | Max bid-ask spread as % of price |

## Known Bottlenecks and Limitations

### 1. Scan Speed (~5-6 seconds)

The full NSE scan fetches quotes for ~2500 stocks in parallel batches of 50 using 3 concurrent workers. This takes ~5-6 seconds. During this time, prices can move.

**Impact**: The LTP used for scoring may be stale by the time the entry order is placed. A stock at +5.1% during scan could be at +4.8% (below threshold) or +10.5% (above threshold) by entry time.

**Mitigation**: Real-time entry validation re-fetches live quotes immediately before order placement. The bot also fetches 5x more candidates than needed (pool of 10 for top 2), so even if some candidates move out of range, there are fallbacks.

### 2. 10-Second Polling Gap

The bot checks prices every 10 seconds. A flash crash between polls can blow through the trailing stop.

**Impact**: Exit price may be significantly below the trailing stop level.

**Mitigation**: Server-side SL-M order on the exchange acts as a backup. The exchange triggers it automatically regardless of polling. However, SL-M is a market order — in a fast crash, the fill price may still be below the trigger.

### 3. Angel One Cautionary List (No Public API)

Angel One maintains an internal blocklist broader than NSE's ASM/GSM. There is no API to fetch this list. The bot discovers blocked stocks by placing test orders.

**Impact**: Each probe takes ~0.5 seconds. With 10 candidates and 4 threads, probing takes ~2-5 seconds. If most candidates are blocked, the bot may exhaust all fallbacks.

**Mitigation**: Probe results are cached in the session blacklist. Stocks rejected once won't be probed again.

### 4. Market Order Slippage

Entry and exit orders use MARKET type for fastest fill. In volatile stocks (which 5-10% gainers are), the fill price can differ from the LTP.

**Impact**: Entry may be higher than expected (negative slippage), reducing profit potential. Exit may be lower than expected, increasing losses.

**Mitigation**: For entries, the bot calculates quantity based on LTP, so slight slippage is absorbed. For exits, the SL-M backup ensures the exchange handles it.

### 5. Session Management

The bot auto-refreshes its JWT session every 2 hours via `refresh_if_stale()`. On 401/403 errors, it automatically re-authenticates and retries the failed request.

**Impact**: Session expiry is handled transparently. No manual intervention needed.

**Mitigation**: Login credentials (PIN + TOTP secret) are stored in memory for auto re-login. The systemd service has `Restart=always` as a final safety net.

### 6. No Partial Fill Handling

The bot assumes orders are fully filled. If a MARKET order is partially filled, the position tracker will have incorrect quantities.

**Impact**: Exit orders may try to sell more shares than actually held, or leave orphan shares.

**Mitigation**: Using INTRADAY product type means the broker auto-squares off any remaining positions at 3:15-3:30 PM.

### 7. Weekend/Holiday Handling

The bot checks `weekday >= 5` for weekends. For holidays, it detects market closure by checking if Nifty 50 `tradeVolume == 0` and `exchFeedTime` is not today's date.

**Impact**: On holidays, the bot detects the closure during the market bullishness check and skips the day without placing any trades or wasting API calls on scanning.

### 8. NSE API Rate Limits

The NSE website APIs (ASM/GSM/F&O lists) are rate-limited and may block requests from cloud IPs.

**Impact**: If NSE blocks the request, the ASM/GSM lists won't load, and some restricted stocks may pass through to the probe layer.

**Mitigation**: The probe layer (Layer 4) catches anything the NSE lists miss. The bot continues even if NSE lists fail to load.

### 9. Re-Entry Round Timing

Each re-entry round includes a 15-minute cooldown + ~6-second scan + ~5-second probe. If a stop-loss exit happens at 2:45 PM, the re-entry would start at ~3:01 PM — only 14 minutes before market close.

**Impact**: Late re-entries have very little time for the trade to develop. The position will likely be force-closed at 3:15 PM regardless.

**Mitigation**: The bot checks if market is still open after cooldown. Consider adding a "no re-entry after 2:30 PM" cutoff.

### 10. Concurrent SL-M Modification (Mitigated)

When the trailing stop moves up, the bot modifies the SL-M order. If the exchange triggers the SL-M between the bot's price check and the modify call, the modify will fail.

**Impact**: The bot logs a warning but continues. Before any software exit, `_check_slm_executed()` checks the order book — if the SL-M is already `COMPLETE`/`TRIGGERED`, the software exit is skipped (prevents double-sell).

**Mitigation**: The SL-M race condition check handles this case. If the order book check itself fails, the bot proceeds with the exit (safe default — better to risk a rejection than leave a position unmanaged).

## Project Structure

```
algo-trader/
├── main.py                      # Entry point — daily trading loop (scan window + retry + safety)
├── backtest.py                  # Historical backtest using Angel One candle data
├── broker/
│   └── angelone_client.py       # Angel One SmartAPI client (retry, rate limit, auto re-login)
├── config/
│   ├── settings.py              # Environment variable loader
│   └── instruments.py           # Instrument resolution and scrip master
├── strategy/
│   ├── market_scanner.py        # Full NSE scan, 4-factor market check, multi-factor scoring
│   └── momentum_strategy.py     # EMA-based signal generator (legacy, not used by main loop)
├── execution/
│   ├── order_manager.py         # Order placement with retry and tradability checks
│   ├── entry_validator.py       # 5-check real-time entry validation (batch + parallel)
│   └── tradability_filter.py    # ASM/GSM/circuit/probe filtering
├── monitor/
│   └── position_tracker.py      # Position state, trailing stop, profit lock
├── risk/
│   └── risk_manager.py          # Position sizing (legacy, not used by main loop)
├── data/
│   └── market_stream.py         # Historical candle fetcher (used by utility scripts)
├── utils/
│   ├── logger.py                # File + console logging setup
│   └── telegram_alert.py        # Telegram notification sender
├── deploy/
│   └── algo-trader.service      # systemd service file for EC2
├── tests/
│   ├── test_core.py             # Core logic tests (39 tests)
│   ├── test_angelone_check.py   # Settings and data check tests
│   ├── test_stock_scanner.py    # Scanner tests
│   ├── test_excel_watchlist.py  # Excel watchlist tests
│   └── test_stock_updates.py    # Alert utility tests
├── Stock.py                     # CLI: fetch and display stock data
├── send_stock_updates.py        # CLI: send stock alerts via Telegram
├── check_angelone_data.py       # CLI: verify Angel One connectivity
├── requirements.txt             # Python dependencies
├── .env.example                 # Template environment file
└── .gitignore
```

## Deployment (EC2)

```bash
# Clone and setup
git clone https://github.com/YOUR_USERNAME/algo-trader.git ~/algo-trader
cd ~/algo-trader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements.txt

# Create .env (no inline comments!)
nano .env

# Install systemd service
sudo cp deploy/algo-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader

# Monitor
tail -f ~/algo-trader/algo_trader.log
sudo systemctl status algo-trader
```

## Dependencies

```
pandas          # DataFrame operations for candle data
requests        # HTTP client for Angel One API and NSE
pyotp           # TOTP generation for auto-login and session refresh
numpy           # Fast EMA calculation in momentum strategy
```

Note: The scrip master (~30MB JSON) is cached to `data/.scrip_master_cache.json` with a 24-hour TTL. Delete this file to force a fresh download.
