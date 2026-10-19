# BingX Trend-Following Futures Bot

A rule-based bot for BingX USDT-M perpetual futures that follows trends on the **4h** chart, filtered by the **daily** trend. It enters on breakouts, exits only on an ATR trailing stop (no fixed take-profit), and risks a fixed slice of equity per trade. A daily-loss circuit breaker halts new entries after a bad day.

The repo name is historical: until October 2026 this bot traded a weekly-trend / daily-pullback / 4h-trigger strategy. It was replaced by trend following because published research finds crypto trends persist on multi-day horizons, and trend following has the strongest evidence of the common bot strategies. Sibling of [bingx-order-block-bot](https://github.com/MatveevIlya1995/bingx-order-block-bot).

## How a trade gets taken

1. **Daily trend filter** -- last daily close above a rising EMA(`TREND_EMA_PERIOD`, 50) = bullish, below a falling one = bearish, otherwise no trade (`strategy.detect_trend`). Longs only in a bullish trend, shorts only in a bearish one.
2. **4h breakout** -- the last closed 4h candle is the FIRST to close above the highest high (long) or below the lowest low (short) of the previous `BREAKOUT_LOOKBACK` (20) 4h candles (`strategy.donchian_breakout`).
3. **Optional momentum filter** (`MOMENTUM_FILTER`, off by default) -- longs only in the strongest `MOMENTUM_TOP_FRACTION` of watched coins by `MOMENTUM_LOOKBACK_DAYS` return, shorts only in the weakest. When more signals fire than free slots, the strongest momentum goes first.
4. **Initial stop** -- `STOP_ATR_MULT` (2.5) x ATR(`ATR_PERIOD`, 14) of the 4h chart from the entry (`strategy.build_trend_entry`).
5. **Trailing stop, no take-profit** -- the stop trails `TRAIL_ATR_MULT` (3) x ATR behind the best price since entry, moving only in the trade's favour. The trade ends when it is hit (`risk_manager.py`).
6. **Sizing** -- a fixed `MARGIN_PER_TRADE_USDT` of margin, with leverage (10x cap) chosen so a stop-out loses at most `RISK_PCT_PER_TRADE` of equity, itself hard-capped at 3% (`risk_manager.position_size`).

Trend following makes money on a few large winners and loses small amounts on many false breakouts; a 30-45% win rate is normal.

## Backtest

Run the **Backtest** workflow (Actions tab -> Backtest -> Run workflow), or locally: `python3 backtest.py --days 365`. It replays the strategy on real BingX 4h and daily candles with the repo's settings, fees, slippage and funding, and reports the result for both halves of the period plus sensitivity tables. The report appears on the run's summary page. `python3 backtest.py --synthetic 1` runs it on random candles to test the code without network access.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point -- one cycle: manage open trades, then scan for new breakouts |
| `strategy.py` | Daily trend filter, 4h Donchian breakout, ATR, momentum, entry/stop construction |
| `risk_manager.py` | Position sizing, daily-loss circuit breaker, ATR trailing stops, DRY_RUN P/L simulation, trade journal |
| `exchange_client.py` | ccxt BingX wrapper: market entry with reduce-only stop, stop replacement, exit-fill lookup |
| `backtest.py` | Backtest on real BingX candles with the bot's own sizing |
| `config.py` | Settings from environment variables, plus hard caps (3% risk, 10x leverage) |
| `.github/workflows/bot.yml` | Runs one cycle every 15 minutes and commits state/journal back to the repo |
| `.github/workflows/backtest.yml` | Manual backtest run |
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
1. Run `python3 bot.py` a few times with `DRY_RUN=true` (the default) and confirm it reads your balance, fetches daily and 4h candles, and logs sensible trend/breakout output without error.
2. If you can, test against BingX's demo trading (VST) environment first, or at minimum keep `RISK_PCT_PER_TRADE` very small for the first week live.
3. Open a Python REPL and place one manual small test order through `ExchangeClient` to confirm `positionSide` and stop-order params match your account's position mode (one-way vs. hedge), and confirm `fetch_trade_exit()`'s field-name assumptions against a real closed trade. Also watch the first trailing-stop move closely: `replace_stop_order()` must cancel the old stop and place the new one.
4. Only then set `DRY_RUN=false`.

Schedule it. The entry timeframe is 4h, so a schedule anywhere from every 5-30 minutes is enough to catch a fresh candle close promptly -- the bot itself is idempotent per cycle (it checks existing positions before opening a new one):

```
*/15 * * * * /path/to/bingx-trend-pullback-bot/run.sh >> /path/to/bingx-trend-pullback-bot/data/cron.log 2>&1
```

Or use the included `.github/workflows/bot.yml` (runs every 15 minutes via GitHub Actions; needs `BINGX_API_KEY`/`BINGX_API_SECRET` as repo Secrets and the config knobs below as repo Variables).

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOLS` | 15-symbol basket | Comma-separated ccxt symbols of the USDT-M perpetuals to watch |
| `DAILY_TIMEFRAME` / `ENTRY_TIMEFRAME` | `1d` / `4h` | Trend-filter and entry timeframes |
| `DAILY_CANDLE_LIMIT` / `ENTRY_CANDLE_LIMIT` | `200` / `300` | Candles fetched per cycle |
| `TREND_EMA_PERIOD` | `50` | Daily EMA for the trend filter |
| `BREAKOUT_LOOKBACK` | `20` | 4h candles in the breakout channel |
| `ATR_PERIOD` | `14` | ATR length on the 4h chart |
| `STOP_ATR_MULT` | `2.5` | Initial stop distance in ATRs |
| `TRAIL_ATR_MULT` | `3` | Trailing-stop distance in ATRs behind the best price |
| `MOMENTUM_FILTER` | `false` | Only long the strongest / short the weakest coins |
| `MOMENTUM_LOOKBACK_DAYS` | `30` | Momentum measured over this many days |
| `MOMENTUM_TOP_FRACTION` | `0.5` | Share of coins counted as strongest / weakest |
| `RISK_PCT_PER_TRADE` | `3` | % of equity lost at the initial stop; hard-capped at 3% |
| `MARGIN_PER_TRADE_USDT` | `50` | Fixed margin per trade; leverage (10x cap) flexes to hit the risk |
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
