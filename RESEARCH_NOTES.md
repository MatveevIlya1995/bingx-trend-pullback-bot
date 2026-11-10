# Research notes: where we are (saved 2026-11-10)

Everything tested on 2026-11-09/10, so work can pick up from here.

## Current state of both bots

| | Order block bot | Trend bot (this repo) |
|---|---|---|
| Strategy | Order block / smart-money (retired) | Daily 55-day Donchian breakout, exit plan **B** since 2026-11-10 (trades opened before keep A) |
| New entries | **Off** (`ENTRY_SYMBOLS: "NONE"` in its bot.yml) | On |
| Mode | DRY_RUN | DRY_RUN |
| Risk per trade | 2% | **2%** (set in this repo's `bot.yml`) |
| Open paper trades (end of 2026-11-09) | none | BTC short, ETH short (new rule); HBAR, LTC, XLM (old 4h rule, run to their own stops) |

Fixed on 2026-11-09 (both bots where relevant): BingX 100410 rate limits (paced + retried
candle requests), slots 4-5 unusable because of the margin cap, paper-trade simulation
(order block bot: exits checked on every candle, no fills from before an order existed,
one fill per symbol), trend bot read free balance instead of equity.

Open items:
- **Real account balance is falling** ($263 in Aug -> $157 now) while both bots are paper
  trading. Something else trades on the account: check BingX trade history.
- The repo Variables could not be edited from the Claude session, so the strategy settings
  are code defaults (`config.py`) and old 4h-strategy Variables are no longer passed in.
- cron-job.org: the order block bot's job can be paused (nothing left to manage). The trend
  bot is fine every 15 min.
- Keep paper trading the trend bot for a few weeks before going live.

## 1. Order block / SMC strategy: no edge

Bot's own `backtest.py` (order block repo), every entry model, ~45-100 days of history:

| Entry timeframe | Baseline result | Best variant |
|---|---|---|
| 5m (2026-09-19 to 11-02) | -45% | -15% |
| 15m (2026-08-04 to 11-09) | -28% | +5% (lost in the 2nd half) |
| 1h (2026-08-01 to 11-09) | -37% | -5% |
| 4h (2026-08-01 to 11-09) | -11% | -11% |

Every entry model (order block, fair value gap, OTE, breaker) and filter set lost; fees and
noise on tight stops eat the edge.

## 2. Trend following: what worked (`research.py`, "Strategy research" workflow)

15 large-cap BingX perpetuals, daily candles, 2022-01-19 to 2026-11-09, one account, 1% risk,
max 5 positions, 3x notional cap, 0.05% fee + 0.03% slippage per side, 0.03%/day funding on
longs. In-sample (IS) 2022-01 to 2025-04, out-of-sample (OOS) 2025-04 to 2026-11.

| Rule (daily) | IS PF | OOS PF | OOS return | Whole CAGR | Whole max DD |
|---|---|---|---|---|---|
| Donchian 20/10 | 1.24 | 1.11 | +8.8% | 10.3% | 24.9% |
| Donchian 30/15 | 1.09 | 1.22 | +13.7% | 5.8% | 27.6% |
| Donchian 40/20 | 1.16 | 1.19 | +12.0% | 7.2% | 26.3% |
| **Donchian 55/20 (chosen)** | **1.38** | **1.20** | **+10.0%** | **11.0%** | **22.6%** |
| Donchian 55/20 long-only | 1.55 | 1.31 | +10.0% | 9.4% | 21.1% |
| Donchian 100/50 | 1.37 | 0.95 | -1.7% | 5.4% | 18.0% |
| EMA 50/200 | 2.35 | 1.81 | +6.9% | 8.1% | 12.0% |
| 4h Donchian (all lengths) | ~1.2 | ~1.0 | mixed | - | 40-60% |

Caveats: the 15 coins are today's survivors (results flattering); 2025 was a losing year for
every rule; at 2% risk expect roughly double the return and drawdown.

## 3. Exit plans on the chosen entry (`research_exits.py`, "Exit plan research" workflow)

Same daily 55-day entries, price walked on 1h candles (BingX 1h history only from
2024-04-27), 1% risk, max 5 positions. Whole period 2024-04-27 to 2026-11-09:

| Plan | Trades | Win rate | Avg R | PF | Return | Per year | Max DD | Reached 3R / 5R | Days held | Per year, 1st / 2nd half |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** current: 20-day channel / 2 ATR trail, whole position | 173 | 40.5% | +0.163 | 1.37 | **+24.9%** | **+9.2%** | 17.3% | 4% / 2% | 13.1 | +8.0% / +9.9% |
| B 1h: TP1 3R (1/3), TP2 5R (1/3), BE at 1st 1h BOS, trail to last swing each BOS, runner to 1h CHoCH | 372 | 25.5% | +0.050 | 1.32 | +18.4% | +6.9% | 9.9% | 2% / 1% | 1.5 | +9.6% / +4.3% |
| B 4h: same, structure on 4h | 253 | 24.5% | +0.042 | 1.14 | +6.6% | +2.5% | 14.1% | 8% / 2% | 5.5 | +3.8% / +1.3% |
| C: TP1 3R (1/3), TP2 5R (1/3), stop moves only at TPs (TP1 -> entry, TP2 -> TP1) | 47 | 34.0% | +0.060 | 1.09 | +1.8% | +0.7% | 11.9% | 30% / 9% | 88.6 | +0.8% / +1.1% |
| C 1h: C + runner trails on 1h BOS/CHoCH after TP2 | 61 | 31.1% | -0.031 | 0.96 | -2.1% | -0.9% | 17.6% | 26% / 8% | 66.9 | - |
| C 4h: C + runner trails on 4h BOS/CHoCH after TP2 | 57 | 31.6% | -0.041 | 0.94 | -2.9% | -1.2% | 13.9% | 26% / 5% | 70.3 | - |
| D: TP1 3R (50%), TP2 5R (50%), BE after TP1, no trail | 65 | 33.8% | +0.163 | 1.24 | +9.9% | +3.8% | 10.1% | 28% / 11% | 62.6 | +10.5% / +0.8% |

What the exit tests show:
- Structure on 1h/4h moves the stop to breakeven within hours/days, so 3R/5R are rarely hit (B).
- Leaving the stop alone until a target holds trades for 2-3 months: slots stay full, far
  fewer trades, losers held too long, funding adds up (C, D).
- D earns the same per trade as A (+0.16R) but takes ~1/3 as many trades; most of its profit
  was in 2024; it has the smallest drawdown besides B 1h.
- **Decided 2026-11-10 (after section 4): switched to B at 2% risk.**

## 4. Money management (`research_mm.py`, "Money management research" workflow), 2026-11-10

One account simulated hour by hour on 1h candles, 2024-04-28 to 2026-11-10, $1000 start,
max 5 positions, daily 55-day entries. Max DD marked to market every hour.

| Scenario | Per year | Max DD | Per year / DD | 1st / 2nd half per year |
|---|---|---|---|---|
| A 1% | +10.2% | 19.7% | 0.52 | +8.4% / +11.0% |
| A 1.5% | +14.2% | 28.2% | 0.50 | +10.6% / +16.0% |
| A 2% (bot now) | +17.4% | 35.8% | 0.48 | +11.7% / +20.8% |
| B 1% | +8.2% | 12.1% | 0.68 | +11.4% / +4.8% |
| B 1.5% | +11.9% | 17.6% | 0.68 | +17.1% / +6.6% |
| **B 2%** | **+15.5%** | **22.9%** | **0.68** | +22.9% / +8.0% |
| A 1.5% + caps (6% total risk, max 3 one direction) | +5.2% | 22.2% | 0.23 | |
| A 1.5% + caps + drawdown brake (half risk >10% below peak) | -0.8% | 18.0% | - | |
| B 1.5% + caps + brake | +6.2% | 12.5% | 0.49 | |
| A 1.5% + pyramiding (+1 ATR, +2 ATR) | +11.4% | 57.1% | 0.20 | |
| A 1.5% + pyramiding + caps + brake | +8.9% | 29.1% | 0.31 | |

Non-crypto perps (gold, oil, indices, forex; 1h history only from late 2025), same window from
2025-10-18, A 1.5% + caps + brake: crypto only +27.6%/yr, with non-crypto -2.6%/yr (non-crypto
trades lost $136, crypto made $108); B with non-crypto -21%/yr.

Real account size ($157) with BingX minimum order sizes:

| Scenario | Per year | Max DD | Worst calendar year |
|---|---|---|---|
| A 1.5% | +7.0% | 29.8% | -11.0% |
| A 2% (bot now) | +8.4% | 35.4% | -12.2% |
| B 1.5% | +11.3% | 18.3% | +1.2% |
| **B 2%** | **+16.3%** | **22.1%** | **+2.4%** |

Conclusions: risk caps, the drawdown brake, pyramiding and non-crypto symbols all made results
worse -> rejected. B beats A on return per unit of drawdown at every risk level, and at the
real $157 size it beats A outright (minimum order sizes hurt A's wider trades). Caveat: B made
most of its money in the 1st half (to Aug 2025), A in the 2nd half.
**Decided and switched 2026-11-10: exit plan B at 2% risk, crypto only, no caps/brake/pyramiding.** The bot's exit code was checked against the backtest's plan B: identical fills on 300 random trades.

## How to rerun

- Strategy comparison: Actions -> "Strategy research" -> Run workflow (`research.py`).
- Money management: Actions -> "Money management research" (`research_mm.py`; the workflow currently passes `--small-only`, remove it for the full run).
- Exit plans: Actions -> "Exit plan research" -> Run workflow (`research_exits.py`). Plans are
  defined in `run_trade()`; `PLANS` in `main()` picks which ones run.
- Results show in the run's annotations (summary page).
