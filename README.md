# BingX Trend Pullback Futures Bot

A rule-based bot for BingX USDT-M perpetual futures that trades a trend + pullback strategy on exactly three timeframes: **weekly** (trend), **daily** (trend confirmation + pullback zone), and **4h** (entry trigger). Every entry gets an automatic stop-loss (hard-capped at 3% of equity) sized so it also never asks for more margin than the account can actually support. At 1R (the same distance as the initial risk) half the position takes profit and the stop for the rest moves to breakeven; the remainder then trails with no fixed cap instead of being capped at a fixed 2R target. A daily-loss circuit breaker halts new entries if losses get too large for the day.

This is the sibling of [bingx-order-block-bot](https://github.com/MatveevIlya1995/bingx-order-block-bot), reusing the same risk management, exchange wiring, and GitHub Actions scheduling, with a simpler trend-following entry model in place of order-block retests.

## How a trade gets taken

1. **Weekly (primary trend)** -- an EMA-slope trend read (`strategy.detect_trend`). The weekly trend must be clearly bullish or bearish for this strategy to look for a trade at all: a pullback needs a trend to pull back from, so a neutral weekly reading skips the symbol this cycle (`config.require_weekly_trend`).
2. **Daily (trend confirmation)** -- the same EMA-slope read on daily closes. If it directly contradicts the weekly trend, the symbol is skipped for the cycle (`strategy.combine_htf_bias`). Unlike the order-block bot, there is **no** weekly/daily override here -- this bot never trades against its weekly trend, keeping the rule simple and unconditional.
3. **Daily (pullback zone)** -- has the daily chart pulled back to (but not broken through) a daily EMA "value zone" in the weekly trend's direction? See `strategy.daily_pullback_zone()`.
4. **4h (entry trigger)** -- has the 4h chart just crossed back to the trend side of its fast EMA? See `strategy.entry_trigger()`. This is edge-triggered, so it fires once per pullback without needing extra zone-dedup bookkeeping (unlike the order-block bot's zone tracking).
5. **Stop-loss & take-profit** -- the stop sits just beyond the 4h chart's nearest confirmed swing low/high (`strategy.build_entry`); take-profit is a fixed 2:1 reward:risk target. Position size is a fixed `MARGIN_PER_TRADE_USDT` of margin; leverage is then chosen automatically (up to a 10x hard cap) so a stop-out never loses more than `RISK_PCT_PER_TRADE` of equity, itself hard-capped at 3% (`risk_manager.position_size`).
6. **Partial take-profit + trailing stop** -- at `PARTIAL_TP_R_MULTIPLE` times the initial risk (1R by default), `PARTIAL_TP_FRACTION` of the position (50% by default) closes and the stop for the remainder moves to breakeven. From there the remainder trails `TRAIL_R_MULTIPLE` behind the best price reached so far, with no fixed cap -- identical mechanics to the order-block bot (`risk_manager.py` is a direct port).

See `bot.py`'s module docstring for the full per-cycle sequence, and `strategy.py`'s module docstring for the entry logic in detail.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Entry point -- runs one full check-and-maybe-trade cycle: weekly/daily/4h analysis, trade management, and new entries |
| `strategy.py` | Trend detection (EMA slope), daily pullback zone, 4h entry trigger, and stop/target construction |
| `risk_manager.py` | Position sizing (risk + margin caps), leverage cap, daily-loss circuit breaker, partial-take-profit/trailing-stop management, DRY_RUN P/L simulation -- ported unchanged from the order-block bot |
| `exchange_client.py` | Thin wrapper around ccxt's BingX integration, including exit-fill lookup and the partial-close/replace-stop order calls -- ported unchanged from the order-block bot |
| `config.py` | Loads settings from environment variables, and enforces the hard caps (3% risk, margin utilization, fixed 2:1 reward:risk, 10x leverage) |
| `.env.example` | Template for your API keys and settings |
| `.gitignore` | Keeps `.env`, Python bytecode caches, and OS metadata files out of version control |
| `run.sh` | Loads `.env` and runs one cycle -- point cron at this |
| `.github/workflows/bot.yml` | GitHub Actions alternative to cron -- runs on a schedule and commits state/journal updates back to the repo |
| `data/state.json` | Auto-created; tracks today's P&L and open trades (including partial-TP/trailing state) for the circuit breaker and exit reconciliation |
| `data/trades.csv` | Auto-created trade journal (ENTRY/PARTIAL_EXIT/EXIT rows); open it on github.com and it renders as a table |

`risk_manager.py` and `exchange_client.py` are unmodified copies of the order-block bot's versions: every call this bot's `bot.py` makes to them (fetch_ohlcv with explicit timeframe/limit, position sizing, circuit breaker, partial-TP/trailing, exit reconciliation) matches their existing interface exactly, so no adaptation was needed. `risk_manager.py`'s `zone_already_traded`/`remember_zone_trade` helpers are order-block-specific and simply go unused here, since this strategy's 4h entry trigger is already edge-triggered and needs no separate zone dedup.

**Like the order-block bot, this strategy has not been backtested or run against live BingX data.** Validate it the same way (see Setup below) before trusting it with real funds.

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
1. Run `python3 bot.py` a few times with `DRY_RUN=true` (the default) and confirm it reads your balance, fetches weekly/daily/4h candles, and logs sensible trend/pullback/trigger output without error.
2. If you can, test against BingX's demo trading (VST) environment first, or at minimum keep `RISK_PCT_PER_TRADE` very small for the first week live.
3. Open a Python REPL and place one manual small test order through `ExchangeClient` to confirm `positionSide` and stop-order params match your account's position mode (one-way vs. hedge), and confirm `fetch_trade_exit()`'s field-name assumptions against a real closed trade. Also specifically exercise a partial take-profit once live (or watch closely the first time one fires) -- see the order-block bot's README for the detailed version of this check, since the mechanics are identical here.
4. Only then set `DRY_RUN=false`.

Schedule it. The entry timeframe is 4h, so a schedule anywhere from every 5-30 minutes is enough to catch a fresh candle close promptly -- the bot itself is idempotent per cycle (it checks existing positions before opening a new one):

```
*/15 * * * * /path/to/bingx-trend-pullback-bot/run.sh >> /path/to/bingx-trend-pullback-bot/data/cron.log 2>&1
```

Or use the included `.github/workflows/bot.yml` (runs every 15 minutes via GitHub Actions; needs `BINGX_API_KEY`/`BINGX_API_SECRET` as repo Secrets and the config knobs below as repo Variables).

## Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOLS` | 15-symbol default basket | Comma-separated ccxt unified symbols for the USDT-M perpetuals to scan |
| `WEEKLY_TIMEFRAME` / `DAILY_TIMEFRAME` / `ENTRY_TIMEFRAME` | `1w` / `1d` / `4h` | The three (and only three) timeframes this strategy looks at |
| `WEEKLY_CANDLE_LIMIT` / `DAILY_CANDLE_LIMIT` / `ENTRY_CANDLE_LIMIT` | `150` / `200` / `300` | How many candles to fetch per level per cycle |
| `TREND_EMA_PERIOD` | `50` | EMA period for the weekly/daily trend read |
| `REQUIRE_WEEKLY_TREND` | `true` | Weekly trend must be clearly bullish/bearish to look for a trade at all |
| `PULLBACK_EMA_PERIOD` | `20` | Daily EMA that defines the pullback "value zone" |
| `PULLBACK_TOUCH_TOLERANCE_PCT` | `0.5` | How close (%) the daily candle must get to the pullback EMA to count as tagged |
| `FRACTAL_N` | `2` | N-bar fractal window used for swing-high/low detection |
| `TRIGGER_EMA_PERIOD` | `9` | Fast 4h EMA the entry trigger crosses back over |
| `STOP_BUFFER_PCT` | `0.15` | Extra room (%) beyond the nearest confirmed 4h swing used for the stop-loss |
| `REWARD_RISK_RATIO` | `2` | Ignored: always forced to 2 (partial-TP/trailing usually manage the runner well past this before it's ever hit as a full exit) |
| `RISK_PCT_PER_TRADE` | `0.5` | % of account equity risked per trade; hard-capped at 3% regardless |
| `MARGIN_PER_TRADE_USDT` | `50` | Fixed USDT margin committed per trade; leverage is chosen automatically (up to a 10x hard cap) so a stop-out never loses more than `RISK_PCT_PER_TRADE` of equity |
| `MAX_MARGIN_UTILIZATION_PCT` | `50` | Open positions combined may never require more than this % of equity*leverage in margin |
| `MAX_DAILY_LOSS_PCT` | `3` | Daily realized-loss % that halts new entries for the rest of the UTC day |
| `MAX_CONCURRENT_POSITIONS` | `1` | Won't open a new position while this many are already open (counts DRY_RUN paper trades the same as LIVE) |
| `MIN_NOTIONAL_USDT` | `10` | Skip a signal if the computed position notional would be below this |
| `PARTIAL_TP_R_MULTIPLE` | `1.0` | R-multiple at which the partial take-profit fires |
| `PARTIAL_TP_FRACTION` | `0.5` | Fraction of the position closed at the partial take-profit; set to 0 to disable partial-TP/trailing entirely |
| `TRAIL_R_MULTIPLE` | `1.0` | How many R behind the best price reached so far the trailing stop sits, once armed |
| `DRY_RUN_FEE_PCT_PER_SIDE` | `0.05` (%) | Round-trip taker-fee estimate subtracted from DRY_RUN's simulated P/L only |
| `DRY_RUN` | `true` | Logs signals only (and simulates their outcome); set `false` to place real orders |

## Important limitations and risks -- please read

- Leverage amplifies losses, including to zero (liquidation). The code's hard leverage cap and margin-utilization cap are backstops, not a recommendation to use them.
- This is one reasonable interpretation of a trend + pullback concept, not a guaranteed edge. There is no backtest included proving this makes money -- backtest it against BingX historical data yourself before trusting it with real funds.
- API/library details can drift -- BingX's position-mode handling and ccxt's exact param names are the most likely things to need a small adjustment for your account (see Setup, step 3).
- The margin-utilization cap is a heuristic based on each open trade's entry price, not a live mark-to-market margin read from the exchange.
- Nothing here is financial advice, and this is not a financial advisor -- this is software that automates a rule set you should understand and agree with before it trades your money.
- Keep `.env` out of version control and off any machine you don't control; anyone with the API key/secret can trade on your account (not withdraw, since that permission is off).
