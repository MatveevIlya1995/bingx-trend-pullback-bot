"""
Entry point. Designed to be invoked ONCE PER CYCLE by a scheduler (cron,
GitHub Actions, systemd timer, etc.) running somewhere with real internet
access.

Each run:
1. reads account equity and open positions (across all watched symbols)
2. reconciles any previously tracked LIVE trades that have since closed
   on the exchange, logging an EXIT row and updating today's realized P/L
3. manages every currently-open trade (LIVE or DRY_RUN paper) against its
   symbol's freshly-fetched 4h candle: simulates/executes stop-loss,
   partial-take-profit and trailing-stop management exactly the way the
   order-block bot does (see risk_manager.py -- this part is unchanged)
4. checks the daily-loss circuit breaker (account-wide)
5. scans cfg.symbols, running a three-timeframe analysis on every symbol
   that isn't already holding a position (LIVE or DRY_RUN):
   a. weekly trend (see strategy.detect_trend on the weekly chart) -- must
      be clearly bullish or bearish, or the symbol is skipped this cycle
      (config.require_weekly_trend): a pullback needs a trend to pull
      back from.
   b. daily trend -- if it directly contradicts the weekly trend, the
      symbol is skipped (see strategy.combine_htf_bias). This bot never
      trades against its weekly trend, and never overrides a daily
      disagreement the way the order-block bot's weekly/daily override
      does -- keeping this bot's trend rule simple and unconditional.
   c. daily pullback zone -- has the daily chart pulled back to (but not
      broken through) a daily EMA "value zone" in the weekly trend's
      direction? See strategy.daily_pullback_zone(). If not active,
      nothing to do this cycle for this symbol.
   d. 4h entry trigger -- has the 4h chart just crossed back to the trend
      side of its fast EMA? See strategy.entry_trigger(). This is what
      actually fires the trade, timed off the 4h chart while direction
      and zone come from the weekly/daily charts.
6. builds the stop-loss (just beyond the 4h chart's nearest confirmed
   swing low/high) and a fixed-reward:risk take-profit -- see
   strategy.build_entry()
7. sizes each position by fixed fractional risk, hard-capped at 3% of
   equity, AND separately capped so it never requires more margin than
   MAX_MARGIN_UTILIZATION_PCT of the account's theoretical capacity at
   the configured leverage -- identical mechanics to the order-block bot,
   see risk_manager.position_size() and config.py.
8. if DRY_RUN=true (the default), logs what it WOULD do and starts
   tracking it so step 3 can resolve it on a future cycle
9. if DRY_RUN=false, sets leverage and submits entry + stop-loss +
   take-profit; a partial-take-profit, breakeven stop and trailing runner
   then take over from step 3, exactly as in the order-block bot.
10. stops scanning once max_concurrent_positions is reached for the cycle
    (DRY_RUN paper trades count the same as LIVE positions)

Unlike the order-block bot, there is no "zone" to dedupe against: the 4h
entry trigger is edge-triggered (see strategy.entry_trigger -- it only
fires on the specific candle where the fast EMA is reclaimed/rejected),
so it naturally fires at most once per pullback without needing extra
bookkeeping. The only dedup that matters is the normal
"don't open a second position on a symbol that already has one" rule
(risk.occupied_symbols()), same as the order-block bot.
"""
import datetime
import sys
import traceback

from config import load_config
from exchange_client import ExchangeClient
from strategy import (
    ohlcv_to_df,
    drop_incomplete_last_candle,
    detect_trend,
    combine_htf_bias,
    daily_pullback_zone,
    entry_trigger,
    build_entry,
)
from risk_manager import RiskManager


def _manage_open_trades(client, risk, cfg, symbol):
    """Fetches this symbol's latest 4h candle once and uses it to
    resolve/advance every currently-tracked open trade on it, LIVE or
    DRY_RUN. Returns the freshly-fetched OHLCV dataframe so the caller
    can also use it for entry-signal scanning without fetching twice, or
    None if the fetch failed."""
    try:
        raw = client.fetch_ohlcv(symbol, timeframe=cfg.entry_timeframe, limit=cfg.entry_candle_limit)
    except Exception as e:
        print(f"[bot] {symbol}: could not fetch 4h candles for trade management ({e}).")
        return None
    df = drop_incomplete_last_candle(ohlcv_to_df(raw), cfg.entry_timeframe)
    high = float(df.iloc[-1]["high"])
    low = float(df.iloc[-1]["low"])

    closed_dry_run = risk.reconcile_dry_run_exits(symbol, high=high, low=low)
    if closed_dry_run:
        print(f"[bot] {symbol}: closed {len(closed_dry_run)} DRY_RUN paper trade(s), logged EXIT row(s).")

    partial_live = risk.reconcile_live_exits(symbol, high=high, low=low, exchange_client=client)
    if partial_live:
        print(f"[bot] {symbol}: took partial take-profit on {len(partial_live)} LIVE trade(s), stop moved to breakeven.")
    risk.trail_live_stops(symbol, high=high, low=low, exchange_client=client)

    return df


def _find_candidate(client, cfg, symbol, df_cache):
    """Runs the weekly -> daily -> 4h analysis for one symbol (steps
    5-6 of the module docstring). Returns a dict describing a tradeable
    candidate, or None if this symbol has nothing to trade this cycle.
    Never sizes or opens anything -- that's left to the caller."""
    try:
        weekly_raw = client.fetch_ohlcv(symbol, timeframe=cfg.weekly_timeframe, limit=cfg.weekly_candle_limit)
        daily_raw = client.fetch_ohlcv(symbol, timeframe=cfg.daily_timeframe, limit=cfg.daily_candle_limit)
    except Exception as e:
        print(f"[bot] {symbol}: could not fetch weekly/daily candles ({e}). Skipping.")
        return None

    weekly_df = drop_incomplete_last_candle(ohlcv_to_df(weekly_raw), cfg.weekly_timeframe)
    daily_df = drop_incomplete_last_candle(ohlcv_to_df(daily_raw), cfg.daily_timeframe)

    weekly_trend = detect_trend(weekly_df, ema_period=cfg.trend_ema_period)
    daily_trend = detect_trend(daily_df, ema_period=cfg.trend_ema_period)

    if cfg.require_weekly_trend and weekly_trend == "neutral":
        print(f"[bot] {symbol}: weekly trend is neutral -- no trend to pull back from, skipping.")
        return None

    bias = combine_htf_bias([weekly_trend, daily_trend])
    if bias == "conflict":
        print(
            f"[bot] {symbol}: weekly trend ({weekly_trend}) and daily trend ({daily_trend}) "
            f"disagree -- no trade this cycle."
        )
        return None
    if bias is None:
        # Both readings neutral -- can only happen if require_weekly_trend
        # is turned off. No directional edge to trade in that case.
        print(f"[bot] {symbol}: weekly and daily trend are both neutral -- no trade this cycle.")
        return None

    direction = bias  # "bullish" or "bearish", weekly and daily agree (or daily is neutral)

    zone = daily_pullback_zone(
        daily_df,
        direction,
        pullback_ema_period=cfg.pullback_ema_period,
        touch_tolerance_pct=cfg.pullback_touch_tolerance_pct,
        fractal_n=cfg.fractal_n,
    )
    if not zone["active"]:
        print(f"[bot] {symbol}: weekly/daily trend is {direction} but no active daily pullback zone ({zone['detail']}).")
        return None

    df = df_cache.get(symbol)
    if df is None:
        try:
            raw = client.fetch_ohlcv(symbol, timeframe=cfg.entry_timeframe, limit=cfg.entry_candle_limit)
        except Exception as e:
            print(f"[bot] {symbol}: could not fetch 4h candles ({e}). Skipping.")
            return None
        df = drop_incomplete_last_candle(ohlcv_to_df(raw), cfg.entry_timeframe)

    if not entry_trigger(df, direction, trigger_ema_period=cfg.trigger_ema_period):
        print(
            f"[bot] {symbol}: {direction} pullback zone active ({zone['detail']}) but no 4h "
            f"entry trigger this candle yet."
        )
        return None

    entry = build_entry(
        df, direction,
        fractal_n=cfg.fractal_n,
        stop_buffer_pct=cfg.stop_buffer_pct,
        reward_risk_ratio=cfg.reward_risk_ratio,
    )
    if entry is None:
        print(f"[bot] {symbol}: 4h entry trigger fired but no valid stop/risk could be computed. Skipping.")
        return None

    trigger_desc = (
        f"trend+pullback {direction}: weekly={weekly_trend}, daily={daily_trend} | "
        f"daily pullback zone: {zone['detail']} | "
        f"4h trigger: close crossed back {'above' if direction == 'bullish' else 'below'} "
        f"EMA{cfg.trigger_ema_period}, swing reference {entry['swing_reference']:.4f}"
    )

    return {
        "symbol": symbol,
        "direction": direction,
        "trigger_desc": trigger_desc,
        "entry": entry,
    }


def _try_open_candidate(client, risk, cfg, candidate, equity):
    """Sizes, journals and (if not DRY_RUN) submits one candidate's
    trade. Returns True if a position was actually opened."""
    symbol = candidate["symbol"]
    entry = candidate["entry"]
    trigger_desc = candidate["trigger_desc"]

    leverage = risk.safe_leverage()
    existing_notional = risk.total_open_notional()
    qty = risk.position_size(
        equity, entry["entry"], entry["stop_loss"],
        leverage=leverage, existing_notional=existing_notional,
    )
    if qty <= 0:
        print(f"[bot] {symbol}: computed position size was zero (risk-based size or margin cap) -- skipping.")
        return False

    notional = qty * entry["entry"]
    if notional < cfg.min_notional_usdt:
        print(
            f"[bot] {symbol}: computed size (qty~{qty:.6f}, ~${notional:.2f} notional) is below "
            f"MIN_NOTIONAL_USDT=${cfg.min_notional_usdt:.2f} -- skipping."
        )
        return False

    print(
        f"[bot] {symbol}: signal {entry['side'].upper()} qty~{qty:.6f} "
        f"entry~{entry['entry']:.2f} SL={entry['stop_loss']:.2f} TP={entry['take_profit']:.2f}"
    )

    trade_id = risk.new_trade_id()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    journal_row = {
        "trade_id": trade_id,
        "timestamp_utc": now_iso,
        "mode": "DRY_RUN" if cfg.dry_run else "LIVE",
        "symbol": symbol,
        "side": entry["side"].upper(),
        "position_side": entry["position_side"],
        "qty": round(qty, 6),
        "price": round(entry["entry"], 4),
        "stop_loss": round(entry["stop_loss"], 4),
        "take_profit": round(entry["take_profit"], 4),
        "equity": round(equity, 2),
        "trigger": trigger_desc,
    }

    if cfg.dry_run:
        risk.log_trade(journal_row)
        risk.track_dry_run_trade({
            "trade_id": trade_id,
            "entry_time_utc": now_iso,
            "mode": "DRY_RUN",
            "symbol": symbol,
            "side": entry["side"].upper(),
            "position_side": entry["position_side"],
            "qty": round(qty, 6),
            "entry_price": round(entry["entry"], 4),
            "stop_loss": round(entry["stop_loss"], 4),
            "take_profit": round(entry["take_profit"], 4),
            "trigger": trigger_desc,
        })
        print(f"[bot] {symbol}: DRY_RUN is on -- no order sent (logged to trade journal).")
        return True

    client.set_leverage(symbol, leverage)
    client.create_market_order_with_stops(
        symbol=symbol,
        side=entry["side"],
        amount=qty,
        stop_loss_price=entry["stop_loss"],
        take_profit_price=entry["take_profit"],
        position_side=entry["position_side"],
    )
    risk.log_trade(journal_row)
    risk.track_open_trade({
        "trade_id": trade_id,
        "entry_time_utc": now_iso,
        "mode": "LIVE",
        "symbol": symbol,
        "side": entry["side"].upper(),
        "position_side": entry["position_side"],
        "qty": round(qty, 6),
        "entry_price": round(entry["entry"], 4),
        "stop_loss": round(entry["stop_loss"], 4),
        "take_profit": round(entry["take_profit"], 4),
        "trigger": trigger_desc,
    })
    print(f"[bot] {symbol}: order submitted with attached stop-loss/take-profit, logged.")
    return True


def run_once():
    cfg = load_config()

    if not cfg.api_key or not cfg.api_secret:
        print("[bot] BINGX_API_KEY / BINGX_API_SECRET are not set. Nothing to do.")
        return

    client = ExchangeClient(cfg)
    risk = RiskManager(cfg)

    equity = client.fetch_balance_usdt()
    if equity <= 0:
        print("[bot] Could not read a positive USDT balance -- aborting this cycle.")
        return

    equity_warning = risk.check_equity_jump(equity)
    if equity_warning:
        print(f"[bot] WARNING: {equity_warning}")

    open_positions = client.fetch_open_positions()
    exchange_open_symbols = {p.get("symbol") for p in open_positions}

    closed_trades = risk.reconcile_closed_trades(open_positions, client)
    if closed_trades:
        print(f"[bot] Reconciled {len(closed_trades)} closed trade(s), logged EXIT row(s).")

    df_cache = {}
    for symbol in sorted(risk.occupied_symbols()):
        df_cache[symbol] = _manage_open_trades(client, risk, cfg, symbol)

    if risk.circuit_breaker_tripped(equity):
        print("[bot] Daily loss circuit breaker is tripped. No new entries today.")
        return

    occupied_symbols = risk.occupied_symbols() | exchange_open_symbols
    open_count = len(occupied_symbols)

    print(
        f"[bot] Equity=${equity:.2f} | {open_count}/{cfg.max_concurrent_positions} "
        f"positions open ({sorted(occupied_symbols) or 'none'}) | watching {len(cfg.symbols)} symbols"
    )

    if open_count >= cfg.max_concurrent_positions:
        print("[bot] Already at max concurrent positions. Skipping scan this cycle.")
        return

    for symbol in cfg.symbols:
        if open_count >= cfg.max_concurrent_positions:
            print(f"[bot] Reached max concurrent positions ({cfg.max_concurrent_positions}) mid-scan. Stopping.")
            break
        if symbol in occupied_symbols:
            print(f"[bot] {symbol}: already have an open position (LIVE or DRY_RUN), skipping.")
            continue
        candidate = _find_candidate(client, cfg, symbol, df_cache)
        if candidate is None:
            continue
        opened = _try_open_candidate(client, risk, cfg, candidate, equity)
        if opened:
            open_count += 1
            occupied_symbols.add(symbol)


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        print("[bot] Unhandled exception:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
