# Production Paper Loss-Trade Findings — 2026-08-29

## Scope and method

This report analyzes every completed losing strategy trade found in
`origin/main:logs/algo-trader-full.log` from 2026-05-30 through 2026-08-29.
The journal log was not counted separately because it overlaps the full log.

- 55 strategy entries were found.
- 49 entries have a matched paper-market exit.
- 28 of those completed trades lost money.
- 6 additional entries were orphaned by restarts and have no matched exit;
  they are excluded from the measurements below.
- Entry and exit prices are paper broker fills, not strategy decision prices.
- `MFE` is maximum favourable excursion: the highest logged position price
  between the entry fill and exit, measured from the entry fill.
- The logs retain the maximum price but do not always retain when that maximum
  first occurred. Time-to-peak therefore cannot be reconstructed reliably.
- Results exclude fees and real-market slippage.

## Main result

The losses are not one homogeneous pattern. They split into entry failures and
profit give-backs:

| Loss pattern | Trades | Share of losses | Gross loss | Description |
|---|---:|---:|---:|---|
| Immediate failure | 10 | 35.7% | -Rs.65,953.77 | Never exceeded +0.25% MFE |
| Weak bounce | 8 | 28.6% | -Rs.69,847.81 | Reached +0.25% to +1%, then failed |
| Rose 1% to 2% | 3 | 10.7% | -Rs.27,386.40 | Became meaningfully profitable, then reversed |
| Rose at least 2% | 7 | 25.0% | -Rs.27,066.35 | Reached +2.14% to +3.35%, then closed red |
| **Total** | **28** | **100%** | **-Rs.190,254.33** | |

Key observations:

- Six losses never printed above the actual paper entry fill.
- Ten losses never exceeded +0.25%.
- Eighteen of 28 losses (64.3%) never reached +1%; they produced
  Rs.135,801.58, or 71.4%, of all gross losses.
- Ten losses reached at least +1% before reversing.
- Seven reached at least +2%, with a median MFE of +2.67%, but still lost
  Rs.27,066.35.
- Median MFE across all losing trades was only +0.43%.
- Median holding time across all losing trades was approximately 71 minutes.

## Every completed losing trade

| Date | Symbol | Entry | Logged high | Exit | MFE | Peak-to-exit | P&L | Pattern |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 2026-06-04 | ZENTEC | 1,820.00 | 1,827.60 | 1,785.50 | +0.42% | -2.30% | -Rs.4,450.50 | Weak bounce |
| 2026-06-09 | THOMASCOOK | 112.86 | 112.98 | 110.43 | +0.11% | -2.26% | -Rs.9,817.20 | Immediate failure |
| 2026-06-11 | INNOVACAP | 962.45 | 966.45 | 943.90 | +0.42% | -2.33% | -Rs.9,145.15 | Weak bounce |
| 2026-06-16 | LALPATHLAB | 1,757.40 | 1,799.70 | 1,726.20 | +2.41% | -4.08% | -Rs.8,424.00 | Gave back at least 2% |
| 2026-06-16 | RML | 982.00 | 982.00 | 980.50 | +0.00% | -0.15% | -Rs.724.50 | Immediate failure |
| 2026-06-18 | CDSL | 1,367.30 | 1,396.50 | 1,347.60 | +2.14% | -3.50% | -Rs.6,835.90 | Gave back at least 2% |
| 2026-06-22 | GNFC | 599.70 | 615.70 | 596.00 | +2.67% | -3.20% | -Rs.2,930.40 | Gave back at least 2% |
| 2026-06-23 | LAOPALA | 191.16 | 192.00 | 187.22 | +0.44% | -2.49% | -Rs.9,790.90 | Weak bounce |
| 2026-06-24 | HUBTOWN | 216.50 | 218.49 | 212.27 | +0.92% | -2.85% | -Rs.9,272.16 | Weak bounce |
| 2026-06-25 | INDIANHUME | 340.85 | 340.85 | 333.90 | +0.00% | -2.04% | -Rs.9,681.35 | Immediate failure |
| 2026-06-25 | HIMATSEIDE | 94.03 | 94.50 | 92.38 | +0.50% | -2.24% | -Rs.8,335.80 | Weak bounce |
| 2026-06-30 | MUFIN | 134.19 | 134.19 | 133.66 | +0.00% | -0.39% | -Rs.1,875.67 | Immediate failure |
| 2026-07-02 | ECLERX | 1,433.00 | 1,438.20 | 1,404.20 | +0.36% | -2.36% | -Rs.9,532.80 | Weak bounce |
| 2026-07-03 | UDS | 199.01 | 199.50 | 195.51 | +0.25% | -2.00% | -Rs.8,330.00 | Immediate failure |
| 2026-07-06 | VERANDA | 262.59 | 265.55 | 257.79 | +1.13% | -2.92% | -Rs.8,678.40 | Rose 1% to 2% |
| 2026-07-07 | SAKSOFT | 180.42 | 185.80 | 179.21 | +2.98% | -3.55% | -Rs.3,187.14 | Gave back at least 2% |
| 2026-07-10 | APOLLOPIPE | 512.80 | 524.00 | 508.35 | +2.18% | -2.99% | -Rs.4,116.25 | Gave back at least 2% |
| 2026-07-15 | LTTS | 3,496.10 | 3,597.00 | 3,489.80 | +2.89% | -2.98% | -Rs.856.80 | Gave back at least 2% |
| 2026-07-16 | DEEPAKFERT | 1,635.00 | 1,639.80 | 1,601.00 | +0.29% | -2.37% | -Rs.9,860.00 | Weak bounce |
| 2026-07-17 | PREMIERPOL | 83.40 | 83.40 | 81.78 | +0.00% | -1.94% | -Rs.9,225.90 | Immediate failure |
| 2026-07-28 | NEOGEN | 2,194.90 | 2,217.20 | 2,148.90 | +1.02% | -3.08% | -Rs.9,936.00 | Rose 1% to 2% |
| 2026-07-29 | DEVYANI | 119.92 | 123.94 | 119.74 | +3.35% | -3.39% | -Rs.715.86 | Gave back at least 2% |
| 2026-07-30 | CHENNPETRO | 1,278.50 | 1,286.00 | 1,253.00 | +0.59% | -2.57% | -Rs.9,460.50 | Weak bounce |
| 2026-08-03 | GODFRYPHLP | 2,256.00 | 2,256.00 | 2,253.70 | +0.00% | -0.10% | -Rs.483.00 | Immediate failure |
| 2026-08-06 | VRLLOG | 293.35 | 293.35 | 289.00 | +0.00% | -1.48% | -Rs.7,042.65 | Immediate failure |
| 2026-08-07 | COSMOFIRST | 920.00 | 936.00 | 903.00 | +1.74% | -3.53% | -Rs.8,772.00 | Rose 1% to 2% |
| 2026-08-26 | FIVESTAR | 528.85 | 529.25 | 518.25 | +0.08% | -2.08% | -Rs.9,518.80 | Immediate failure |
| 2026-08-28 | RAMRAT | 599.85 | 600.55 | 588.15 | +0.12% | -2.06% | -Rs.9,254.70 | Immediate failure |

## Pattern 1: entries that failed immediately

The ten immediate failures had a median entry range position of 0.915, and
seven of ten entered below 0.95. Their median composite score was 0.764 and
their median one-minute volume ratio was 2.38.

Interpretation:

- A high volume ratio did not protect against failure. A transient partial-
  minute volume spike is therefore not sufficient evidence of continuation.
- Range position is more informative for this group: 70% were already too far
  from the intraday high under a proposed 0.95 threshold.
- Composite score did not identify the immediate failures reliably.

Work item:

1. Replay a `range_position >= 0.95` entry gate.
2. Replay confirmation using completed one-minute candles rather than the
   current partial-minute volume spike.
3. Test a breakout-hold or retest rule, such as requiring the price to remain
   above the breakout level for two completed one-minute observations.
4. Measure how many historical winners each rule rejects. Do not deploy a rule
   based only on the losing trades.

## Pattern 2: favourable movement was allowed to become a loss

Seven losing trades reached at least +2% after entry. All seven occurred when
the market gate was 4/4. Their median MFE was +2.67%, but they collectively
lost Rs.27,066.35. The current trailing calculation can remain below the entry
price even after a meaningful favourable move.

Work item:

1. Replay a break-even-plus-cost floor once MFE reaches +2%.
2. Separately test activation at +1.5%, +2.0%, and +2.5%.
3. Include fees, slippage, gaps, stop-order latency, and stop races.
4. Compare final expectancy, not merely the losses saved; an early break-even
   stop may remove trades that later recover and become large winners.

The idealized upper bound is approximately Rs.27,066 of avoided gross loss for
the seven historical give-back trades. This is not an expected saving and must
not be treated as one until replayed on all winners and losers.

## Pattern 3: the 1% to 2% group also needs protection testing

VERANDA, NEOGEN, and COSMOFIRST reached +1.02% to +1.74% before losing a total
of Rs.27,386.40. A +2% break-even trigger would not help these trades.

Work item:

- Test a staged stop policy: tighten downside after +1%, then use
  break-even-plus-cost only after a stronger threshold.
- Do not assume the staged rule is superior until full-path replay determines
  how it changes profitable trades.

## Priority order

1. Correct position sizing to make each stopped loss approximately 1% of
   account equity instead of approximately 9.5%.
2. Replay the `range_position >= 0.95` entry gate against every completed trade.
3. Replay completed-minute volume and breakout-hold confirmation.
4. Replay staged break-even protection against every trade path.
5. Enable production decision snapshots so future analysis has exact quotes,
   partial-minute candles, order book values, and broker-confirmed fills.
6. Keep these hypotheses in paper mode until they succeed on later,
   out-of-sample trades.

## Data-quality warning

The production-paper record also contains 89 filled one-share tradability
probes, 1,876 HTTP 403 responses, 968 authentication refresh attempts, and six
restart-orphaned strategy entries. These issues must be corrected before using
the log-derived P&L as proof of live profitability.
