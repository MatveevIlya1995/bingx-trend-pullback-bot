# BingX Trend-Following Futures Bot

A rule-based bot for BingX USDT-M perpetual futures that follows trends on the **daily** chart: it buys a close above the 55-day high, shorts a close below the 55-day low, takes 1/3 profit at 3R and 1/3 at 5R and trails the rest on 1h market structure (exit plan B, since 2026-11-10), and risks a fixed slice of equity per trade (2%). A daily-loss circuit breaker halts new entries after a bad day.

The repo name is historical: until October 2026 this bot traded a weekly-trend / daily-pullback / 4h-trigger strategy, then a 4h Donchian breakout with a daily EMA filter. Since 2026-11-09 it trades the daily rule below, the one that held up in testing. Sibling of [bingx-order-block-bot](https://github.com/MatveevIlya1995/bingx-order-block-bot), whose order-block strategy was retired the same day after backtests showed it losing on every timeframe.

## How a trade gets taken (code defaults in `config.py` = the tested rule)

1. **Daily breakout** -- the last closed daily candle closes at or above the highest high (long) or at or below the lowest low (short) of the previous `BREAKOUT_LOOKBACK` (55) daily candles (`strategy.donchian_breakout`). A flat coin still beyond the channel is entered again after a stop-out (`BREAKOUT_EDGE_ONLY=false`).
2. **No trend or momentum filter by default** -- `TREND_FILTER` (daily EMA) and `MOMENTUM_FILTER` exist but are off; the tested rule has neither, and the EMA-filtered versions did slightly worse.
3. **Initial stop** -- `STOP_ATR_MULT` (2) x ATR(`ATR_PERIOD`, 20) from the entry (`strategy.build_trend_entry`).
4. **Exit plan B (`EXIT_PLAN=structure`, default since 2026-11-10)** -- R = the initial stop distance. TP1 at `TP1_R` (3R) closes `TP1_FRACTION` (1/3), TP2 at `TP2_R` (5R) closes `TP2_FRACTION` (1/3), the last third runs. On closed 1h candles (fractal swings, 2 candles each side, only swings after the entry hour): the first BOS in the trade's direction moves the stop to the entry; every later BOS moves it to the swing the breaking move started from; the runner ends at the first CHoCH against the trade (that stop being hit) (`strategy.structure_step`, `risk_manager.manage_structure_trades`). Live: TP1/TP2 rest on the exchange as reduce-only orders. Chosen after `research_mm.py`: at the real $157 account size B at 2% made +16.3%/yr with a 22% max drawdown, against +8.4%/yr and 35% for plan A (RESEARCH_NOTES.md section 4).
   **Exit plan A (`EXIT_PLAN=channel`, trades opened before the switch)** -- no take-profit; after every closed daily candle the stop moves to the tighter of the `EXIT_LOOKBACK` (20)-day channel (lowest low for a long, highest high for a short) and the close minus/plus `TRAIL_ATR_MULT` (2) x the current ATR, never backwards (`strategy.trail_levels`, `risk_manager._ratchet`).
5. **Volatility sizing** -- position size = (`RISK_PCT_PER_TRADE` of equity) / stop distance, so every trade risks the same amount whatever the coin's volatility. Margin per trade is at most an equal share of `MAX_MARGIN_UTILIZATION_PCT` of equity across the `MAX_CONCURRENT_POSITIONS` slots; leverage is the lowest that fits (10x cap); all positions together stay under `MAX_TOTAL_NOTIONAL_X` (3) x equity; risk is hard-capped at 3% (`risk_manager.position_size`).

Trend following makes money on a few large winners and loses small amounts on many false breakouts; a 35-45% win rate is normal, and losing months (2025 was a losing year in the test) are part of it.

## Backtest results and how to rerun them

Run the **Strategy research** workflow (Actions tab -> Strategy research -> Run workflow), or `python3 research.py --since 2021-01-01`. It tests daily and 4h Donchian / EMA-cross rules on 15 large-cap BingX perpetuals as one account (1% risk, max 5 positions, 3x notional cap, 0.05% taker fee + 0.03% slippage per side, 0.03%/day funding on longs), with 2022-01 to 2025-04 as in-sample and 2025-04 to 2026-11 as out-of-sample.

Result on 2026-11-09 for this rule (55/20, both directions), at 1% risk per trade: in-sample profit factor 1.38, out-of-sample 1.20; about 11% a year with a 23% worst drawdown over the whole period. At 2% risk expect roughly twice both. Every daily breakout length from 20 to 55 days was profitable in both periods; 100 days was not out-of-sample, and the 4h versions had 40-60% drawdowns.

Read before trusting it: the 15 coins are today's survivors (coins that collapsed since 2022 are not in the test), so the numbers are optimistic; and no backtest predicts the next year. `backtest.py` still replays the OLD 4h rule and is kept only for reference.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point -- one cycle: manage open trades, then scan for new breakouts |
| `strategy.py` | Daily Donchian breakout, trailing-stop levels, ATR, optional trend/momentum filters, entry/stop construction |
| `risk_manager.py` | Position sizing, daily-loss circuit breaker, trailing stops, DRY_RUN P/L simulation, trade journal |
| `exchange_client.py` | ccxt BingX wrapper: market entry with reduce-only stop, stop replacement, exit-fill lookup |
| `research.py` | Backtest of the daily rule (and alternatives) on years of BingX history |
| `backtest.py` | OUTDATED: backtest of the old 4h rule |
| `tests/` | `python3 -m pytest tests` |
| `config.py` | Settings from environment variables, plus hard caps (3% risk, 10x leverage) |
| `.github/workflows/bot.yml` | Runs one cycle every 15 minutes and commits state/journal back to the repo |
| `.github/workflows/research.yml` | Manual research/backtest run |
| `.github/workflows/backtest.yml` | Manual run of the old 4h backtest |
| `data/state.json` / `data/trades.csv` | Open trades and the trade journal (ENTRY / EXIT rows) |

**The live order path (entry, stop replacement) has not been verified against a real BingX account.** Keep `DRY_RUN=true` until the backtest and several weeks of paper trading agree.

## Where this needs to run

It cannot run inside a sandboxed chat environment -- that kind of environment's network is typically locked down and can't reach PyPI or BingX's API. Run it somewhere with normal internet access: your own computer, a small VPS, or GitHub Actions (included, see `.github/workflows/bot.yml`).

## Setup

Get the code onto a machine with internet access, then:

```
cd bingx-trend-pullback-bot
pip install -r requirements.txt
cp .env.example .env
```

Create a BingX API key (BingX -> Account -> API Management):
- Enable **Perpetual Futures** trading permission
- Leave **withdrawal permission OFF** -- always
- Set an **IP whitelist** to the machine that will run the bot

Paste the key/secret into `.env`. Never commit `.env` or paste your secret key into a chat.

Validate before trading real money, in this order:
1. Run `python3 bot.py` a few times with `DRY_RUN=true` (the default) and confirm it reads your balance, fetches daily candles, and logs sensible breakout output without error.
2. If you can, test against BingX's demo trading (VST) environment first, or at minimum keep `RISK_PCT_PER_TRADE` very small for the first week live.
3. Open a Python REPL and place one manual small test order through `ExchangeClient` to confirm `positionSide` and stop-order params match your account's position mode (one-way vs. hedge), and confirm `fetch_trade_exit()`'s field-name assumptions against a real closed trade. Also watch the first trailing-stop move closely: `replace_stop_order()` must cancel the old stop and place the new one.
4. Only then set `DRY_RUN=false`.

Schedule it. The strategy works on daily candles (they close at 00:00 UTC), so anything from every 15 minutes to hourly is enough -- the bot itself is idempotent per cycle (it checks existing positions before opening a new one):

```
*/15 * * * * /path/to/bingx-trend-pullback-bot/run.sh >> /path/to/bingx-trend-pullback-bot/data/cron.log 2>&1
```

Or use the included `.github/workflows/bot.yml` (runs every 15 minutes via GitHub Actions; needs `BINGX_API_KEY`/`BINGX_API_SECRET` as repo Secrets. The workflow passes only the account/risk knobs from repo Variables; the strategy settings are the code defaults, and `RISK_PCT_PER_TRADE` is set to 2 in the workflow file itself).

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOLS` | 15-symbol basket | Comma-separated ccxt symbols of the USDT-M perpetuals to watch |
| `ENTRY_TIMEFRAME` | `1d` | Entry / stop timeframe |
| `BREAKOUT_LOOKBACK` | `55` | Candles in the breakout channel |
| `BREAKOUT_EDGE_ONLY` | `false` | `true` = only the first close outside the channel |
| `EXIT_PLAN` | `structure` | `structure` = plan B (TP1/TP2 + 1h BOS/CHoCH stop), `channel` = plan A (daily channel / ATR trail). Only new trades; open ones keep their exit |
| `TP1_R` / `TP2_R` | `3` / `5` | Plan B targets in multiples of the initial risk |
| `TP1_FRACTION` / `TP2_FRACTION` | `1/3` / `1/3` | Share of the position closed at each target (the rest is the runner) |
| `STRUCTURE_TIMEFRAME` / `STRUCTURE_CANDLE_LIMIT` / `FRACTAL_N` | `1h` / `500` / `2` | Plan B structure candles and swing definition |
| `EXIT_LOOKBACK` | `20` | Candles in the trailing exit channel (0 = old chandelier trail) |
| `ATR_PERIOD` | `20` | ATR length |
| `STOP_ATR_MULT` | `2` | Initial stop distance in ATRs |
| `TRAIL_ATR_MULT` | `2` | Trailing stop: close minus this many current ATRs (or the exit channel, whichever is tighter) |
| `TREND_FILTER` | `false` | Only trade in the direction of the daily EMA trend |
| `DAILY_TIMEFRAME` / `DAILY_CANDLE_LIMIT` / `TREND_EMA_PERIOD` | `1d` / `200` / `50` | Trend filter settings |
| `ENTRY_CANDLE_LIMIT` | `300` | Candles fetched per cycle |
| `MOMENTUM_FILTER` | `false` | Only long the strongest / short the weakest coins |
| `MOMENTUM_LOOKBACK_DAYS` | `30` | Momentum measured over this many days |
| `MOMENTUM_TOP_FRACTION` | `0.5` | Share of coins counted as strongest / weakest |
| `RISK_PCT_PER_TRADE` | `3` | % of equity lost at the initial stop; hard-capped at 3% |
| `MARGIN_PER_TRADE_USDT` | `50` | Most margin one trade may use (also capped at an equal share of the utilization cap per slot); leverage is the lowest that fits (10x cap) |
| `MAX_TOTAL_NOTIONAL_X` | `3` | All open positions together stay under this many times equity |
| `MAX_MARGIN_UTILIZATION_PCT` | `50` | Cap on margin used by all open positions together |
| `MAX_DAILY_LOSS_PCT` | `3` | Daily loss that halts new entries until the next UTC day |
| `MAX_CONCURRENT_POSITIONS` | `1` | Maximum open positions (paper trades count) |
| `MIN_NOTIONAL_USDT` | `10` | Skip trades smaller than this |
| `DRY_RUN_FEE_PCT_PER_SIDE` | `0.05` (%) | Fee assumed in paper-trade P/L |
| `DRY_RUN` | `true` | Paper trading; set `false` to place real orders |

## Important limitations and risks -- please read

- Leverage amplifies losses, including to zero (liquidation). The code's hard leverage cap and margin-utilization cap are backstops, not a recommendation to use them.
- Trend following has the strongest published evidence of the common bot strategies, but no edge is guaranteed. Run the included backtest over at least a year, and paper trade for several weeks, before trusting it with real funds.
- API/library details can drift -- BingX's position-mode handling and ccxt's exact param names are the most likely things to need a small adjustment for your account (see Setup, step 3).
- The margin-utilization cap is a heuristic based on each open trade's entry price, not a live mark-to-market margin read from the exchange.
- Nothing here is financial advice, and this is not a financial advisor -- this is software that automates a rule set you should understand and agree with before it trades your money.
- Keep `.env` out of version control and off any machine you don't control; anyone with the API key/secret can trade on your account (not withdraw, since that permission is off).
