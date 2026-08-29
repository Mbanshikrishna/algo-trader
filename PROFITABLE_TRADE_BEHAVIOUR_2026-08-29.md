# Production Paper Profitable-Trade Behaviour — 2026-08-29

## Scope and data quality

This report analyzes all 21 completed profitable strategy trades in
`origin/main:logs/algo-trader-full.log` from 2026-05-30 through 2026-08-29.
It is the companion to `LOSS_TRADE_FINDINGS_2026-08-29.md`.

For 18 winners, cached intraday candles were available: 17 at five-minute
resolution and SOLARA at one-minute resolution. Only fully completed candles
after the entry timestamp and before the exit timestamp were used. PPL,
JTLIND, and LTFOODS have no matching cached intraday file, so their adverse
excursion is unknown and their high is taken from the production log.

Consequently, the reported `MAE` is an observable estimate, not an exact tick
MAE. It can miss movement inside the partial entry and exit candles. Exact
stop calibration requires captured production quotes or one-minute/tick data.

Definitions:

- `MAE`: maximum adverse excursion from the paper entry fill.
- `MFE`: maximum favourable excursion from the paper entry fill.
- `Give-back`: decline from the highest observed/logged price to the exit fill.
- Results exclude fees and real-market slippage.

## Summary

| Metric | Finding |
|---|---:|
| Completed profitable trades | 21 |
| Winners with intraday MAE coverage | 18 |
| Median observed MAE | -0.72% |
| Mean observed MAE | -0.70% |
| Worst observed winner MAE | -1.72% |
| Median MFE | +2.44% |
| Mean MFE | +3.90% |
| Median peak-to-exit give-back | -1.24% |
| Mean peak-to-exit give-back | -1.23% |
| Median holding time | 241 minutes |

Winner drawdown distribution among the 18 covered trades:

| Observed drawdown | Winners reaching this drawdown |
|---|---:|
| At least -0.25% | 14 of 18 |
| At least -0.50% | 12 of 18 |
| At least -0.75% | 8 of 18 |
| At least -1.00% | 5 of 18 |
| At least -1.50% | 2 of 18 |
| At least -2.00% | 0 of 18 |

This means an initial stop at -0.5% or -1.0% would have removed many eventual
winners. The observed winners support an initial volatility-aware stop near
the existing maximum of approximately 2%, provided quantity is sized from
that stop distance so account risk remains near 1%.

## Every completed profitable trade

`n/a` means the historical candle path required for MAE was unavailable.

| Date | Symbol | Entry | Observed low | Observed high | Exit | MAE | MFE | Final return | Peak give-back | Exit type |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-06-02 | HEXT | 546.55 | 543.25 | 550.90 | 548.30 | -0.60% | +0.80% | +0.32% | -0.47% | Market close |
| 2026-06-04 | IIFL | 530.50 | 527.75 | 534.90 | 530.55 | -0.52% | +0.83% | +0.01% | -0.81% | Market close |
| 2026-06-09 | DATAPATTNS | 4,390.80 | 4,390.50 | 4,587.10 | 4,555.00 | -0.01% | +4.47% | +3.74% | -0.70% | Market close |
| 2026-06-10 | UNICHEMLAB | 415.00 | 415.00 | 447.45 | 437.10 | 0.00% | +7.82% | +5.33% | -2.31% | Profit lock |
| 2026-06-12 | MOTILALOFS | 875.00 | 860.70 | 889.55 | 889.25 | -1.63% | +1.66% | +1.63% | -0.03% | Market close |
| 2026-06-15 | HDBFS | 687.00 | 686.85 | 715.00 | 697.80 | -0.02% | +4.08% | +1.57% | -2.41% | Market close |
| 2026-06-17 | KIRLPNU | 1,740.50 | 1,734.70 | 1,840.00 | 1,839.00 | -0.33% | +5.72% | +5.66% | -0.05% | Market close |
| 2026-06-29 | PPL | 282.33 | n/a | 309.63 | 302.57 | n/a | +9.67% | +7.17% | -2.28% | Profit lock |
| 2026-07-01 | IXIGO | 210.50 | 209.00 | 215.64 | 214.23 | -0.71% | +2.44% | +1.77% | -0.65% | Market close |
| 2026-07-09 | AZAD | 2,392.60 | 2,375.10 | 2,478.00 | 2,466.10 | -0.73% | +3.57% | +3.07% | -0.48% | Market close |
| 2026-07-13 | ROUTE | 577.25 | 572.05 | 589.45 | 583.00 | -0.90% | +2.11% | +1.00% | -1.09% | Market close |
| 2026-07-14 | STYL | 387.30 | 383.10 | 394.90 | 390.00 | -1.08% | +1.96% | +0.70% | -1.24% | Market close |
| 2026-07-20 | BECTORFOOD | 198.81 | 196.67 | 211.83 | 207.20 | -1.08% | +6.55% | +4.22% | -2.19% | Profit lock |
| 2026-07-23 | MBAPL | 146.75 | 145.10 | 149.30 | 147.30 | -1.12% | +1.74% | +0.37% | -1.34% | Market close |
| 2026-07-27 | RKFORGE | 620.85 | 610.15 | 635.55 | 627.20 | -1.72% | +2.37% | +1.02% | -1.31% | Market close |
| 2026-07-31 | HYUNDAI | 2,170.00 | 2,162.60 | 2,208.00 | 2,187.20 | -0.34% | +1.75% | +0.79% | -0.94% | Market close |
| 2026-08-05 | AGARWALEYE | 519.90 | 515.90 | 548.10 | 535.10 | -0.77% | +5.42% | +2.92% | -2.37% | Trailing stop |
| 2026-08-10 | POLYPLEX | 1,173.00 | 1,162.00 | 1,188.00 | 1,173.20 | -0.94% | +1.28% | +0.02% | -1.25% | Market close |
| 2026-08-20 | SOLARA | 591.15 | 590.60 | 646.00 | 637.20 | -0.09% | +9.28% | +7.79% | -1.36% | Market close |
| 2026-08-21 | JTLIND | 79.34 | n/a | at least 79.49 | 79.49 | n/a | at least +0.19% | +0.19% | 0.00% observed | Market close |
| 2026-08-24 | LTFOODS | 453.30 | n/a | 490.30 | 477.30 | n/a | +8.16% | +5.29% | -2.65% | Profit lock |

## Stop behaviour inferred from winners

### Initial stop

The initial stop must allow normal post-entry noise:

- 12 of 18 covered winners traded at least 0.5% below entry.
- 5 of 18 traded at least 1% below entry.
- MOTILALOFS and RKFORGE needed approximately 1.63% and 1.72% of room.
- No covered winner reached a 2% drawdown.

This does not prove that 2% is optimal, but it shows that a blanket 0.5% or 1%
initial stop is too tight for this strategy. The stop distance and monetary
risk must remain separate: a wider price stop should produce a smaller
quantity, not a larger account loss.

### Moving to break-even after +1%

Six of the 16 covered winners that reached +1% subsequently traded back to or
below entry on a later completed candle:

- IXIGO
- AZAD
- STYL
- MBAPL
- RKFORGE
- POLYPLEX

A break-even stop immediately after +1% would therefore have removed multiple
eventual winners. A break-even-plus-cost stop would be even more aggressive.

A provisional floor of entry minus 1.25% after +1% would have remained below
the later observed lows of all 16 covered winners. This is only a replay
candidate, not a production setting.

### Protecting after +2%

Eleven covered winners reached +2%. Only RKFORGE subsequently traded below
entry on a later completed candle, reaching approximately entry minus 0.30%.
SOLARA remained above entry but retraced to only approximately entry plus
0.14%; therefore a break-even-plus-cost floor near +0.15% would also have
removed this large winner.

This supports testing:

- Activation at +2% MFE.
- A protective floor near entry minus 0.35%, rather than immediate
  break-even-plus-cost.

The loss analysis found seven losing trades that reached at least +2% before
closing red. This stage is therefore the best current compromise to test.

### Protecting after +3%

Eight covered winners reached +3%. None subsequently returned to entry, and
their lowest later observed price remained at least approximately +1.27% above
entry. A floor around entry plus 1% after a confirmed +3% move is therefore a
reasonable replay candidate.

### Trailing from the high

Large winners require room. The profitable trades gave back a median 1.24%
from their observed high before exit. Several important winners gave back more
than 2%:

- LTFOODS: 2.65%
- HDBFS: 2.41%
- AGARWALEYE: 2.37%
- UNICHEMLAB: 2.31%
- PPL: 2.28%
- BECTORFOOD: 2.19%

A universal 1% trail would cut the strategy's large winners. A 2.0% to 2.5%
high-water trail should be replayed only after meaningful profit has already
been established.

## Candidate staged fixture for replay

This is an experiment specification, not an approved production change:

| Confirmed MFE | Candidate protective rule | Evidence |
|---|---|---|
| Below +1% | Existing ATR stop, capped near -2% | Winners needed as much as -1.72% |
| At least +1% | Raise floor no higher than entry -1.25% | Preserved all 16 covered winners reaching +1% |
| At least +2% | Raise floor near entry -0.35% | Preserved all 11 covered winners reaching +2% |
| At least +3% | Raise floor near entry +1.0% | Preserved all 8 covered winners reaching +3% |
| At least +5% | Test a 2.5% high-water trail | Major winners commonly gave back 2% or more |
| At least +8% | Test a 2.0% high-water trail | Protect exceptional moves while allowing normal noise |

Required evaluation:

1. Replay this fixture against every winner and loser, not only these winners.
2. Use adverse intrabar ordering when both a new high and stop are touched in
   the same candle.
3. Include broker-confirmed fills, gaps, fees, slippage, and stop latency.
4. Compare net expectancy, profit factor, drawdown, large-winner retention, and
   the number of trades converted from losses to scratches.
5. Keep the initial monetary risk at 1% of equity through risk-based sizing.

## Exit behaviour

Of the 21 winners:

- 16 survived until the market-close exit.
- 4 exited through the intraday profit lock.
- Only 1 exited profitably through the ordinary trailing-stop path.

The profitable-trade record therefore supports retaining room for trend
continuation while adding explicit profit floors. It does not support making
the initial stop universally tighter.
