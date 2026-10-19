"""
Entry point. Run ONCE PER CYCLE by a scheduler (GitHub Actions every 15
minutes). The strategy itself works on CLOSED 4h candles, so most runs
only manage open trades; a new 4h candle closes every 4 hours.

Each run:
1. reads account equity and open positions
2. reconciles LIVE trades that have closed on the exchange (EXIT rows)
3. manages every open trade (LIVE or DRY_RUN paper) with its symbol's
   last closed 4h candle: stop check, then the ATR trailing stop moves
   forward (see risk_manager.py). Paper trades exit at their stop.
4. checks the daily-loss circuit breaker
5. scans the watched symbols for new entries (see strategy.py):
   a. daily trend filter: bullish / bearish / neutral (no trade)
   b. 4h Donchian breakout in the same direction, first close outside
      the BREAKOUT_LOOKBACK-candle channel
   c. optional momentum filter: longs only in the strongest coins,
      shorts only in the weakest (MOMENTUM_FILTER)
   When more signals fire than free position slots, the strongest
   momentum in the trade's direction goes first.
6. builds entry / ATR stop / ATR trail distance (no take-profit)
7. sizes from a fixed margin with leverage chosen so a stop-out risks at
   most RISK_PCT_PER_TRADE of equity (risk_manager.position_size)
8. DRY_RUN=true (default): journals the trade and tracks it on paper
9. DRY_RUN=false: sets leverage, sends a market entry plus a reduce-only
   stop; the stop is then trailed on later runs
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
    donchian_breakout,
    momentum_return,
    build_trend_entry,
)
from risk_manager import RiskManager


def _fetch(client, cfg, symbol, timeframe, limit):
    raw = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return drop_incomplete_last_candle(ohlcv_to_df(raw), timeframe)


def _manage_open_trades(client, risk, cfg, symbol):
    """Uses the symbol's last CLOSED 4h candle to resolve / trail its open
    trades. Returns the 4h dataframe for reuse, or None on fetch failure."""
    try:
        df = _fetch(client, cfg, symbol, cfg.entry_timeframe, cfg.entry_candle_limit)
    except Exception as e:
        print(f"[bot] {symbol}: could not fetch 4h candles for trade management ({e}).")
        return None
    if df.empty:
        return df
    last = df.iloc[-1]
    high, low, ts = float(last["high"]), float(last["low"]), last["ts"].isoformat()

    closed = risk.reconcile_dry_run_exits(symbol, high=high, low=low, candle_ts=ts)
    if closed:
        print(f"[bot] {symbol}: closed {len(closed)} DRY_RUN paper trade(s), logged EXIT row(s).")
    risk.trail_live_stops(symbol, high=high, low=low, exchange_client=client, candle_ts=ts)
    return df


def _momentum_ranks(client, cfg, daily_cache):
    """Returns {symbol: momentum return} for every symbol with enough
    daily history, fetching daily candles into daily_cache."""
    scores = {}
    for symbol in cfg.symbols:
        daily = daily_cache.get(symbol)
        if daily is None:
            try:
                daily = _fetch(client, cfg, symbol, cfg.daily_timeframe, cfg.daily_candle_limit)
            except Exception as e:
                print(f"[bot] {symbol}: could not fetch daily candles ({e}).")
                continue
            daily_cache[symbol] = daily
        m = momentum_return(daily, cfg.momentum_lookback_days)
        if m is not None:
            scores[symbol] = m
    return scores


def _momentum_allows(symbol, direction, scores, cfg):
    """Cross-sectional filter: longs in the top fraction, shorts in the
    bottom fraction of watched symbols by momentum."""
    if not cfg.momentum_filter:
        return True, ""
    if symbol not in scores:
        return False, "no momentum score"
    ordered = sorted(scores, key=scores.get, reverse=True)
    k = max(1, int(round(len(ordered) * cfg.momentum_top_fraction)))
    rank = ordered.index(symbol) + 1
    if direction == "bullish" and symbol in ordered[:k]:
        return True, f"momentum rank {rank}/{len(ordered)}"
    if direction == "bearish" and symbol in ordered[-k:]:
        return True, f"momentum rank {rank}/{len(ordered)}"
    return False, f"momentum rank {rank}/{len(ordered)} outside the {'top' if direction == 'bullish' else 'bottom'} {k}"


def _find_candidate(client, cfg, symbol, df_cache, daily_cache, scores):
    daily = daily_cache.get(symbol)
    if daily is None:
        try:
            daily = _fetch(client, cfg, symbol, cfg.daily_timeframe, cfg.daily_candle_limit)
        except Exception as e:
            print(f"[bot] {symbol}: could not fetch daily candles ({e}). Skipping.")
            return None
        daily_cache[symbol] = daily

    trend = detect_trend(daily, ema_period=cfg.trend_ema_period)
    if trend == "neutral":
        print(f"[bot] {symbol}: daily trend neutral -- no trade.")
        return None

    df = df_cache.get(symbol)
    if df is None:
        try:
            df = _fetch(client, cfg, symbol, cfg.entry_timeframe, cfg.entry_candle_limit)
        except Exception as e:
            print(f"[bot] {symbol}: could not fetch 4h candles ({e}). Skipping.")
            return None
        df_cache[symbol] = df

    breakout = donchian_breakout(df, lookback=cfg.breakout_lookback)
    if breakout != trend:
        print(f"[bot] {symbol}: daily trend {trend}, no fresh {trend} 4h breakout on the last closed candle.")
        return None

    ok, mom_desc = _momentum_allows(symbol, trend, scores, cfg)
    if not ok:
        print(f"[bot] {symbol}: {trend} breakout skipped by momentum filter ({mom_desc}).")
        return None

    entry = build_trend_entry(
        df, trend,
        atr_period=cfg.atr_period,
        stop_atr_mult=cfg.stop_atr_mult,
        trail_atr_mult=cfg.trail_atr_mult,
    )
    if entry is None:
        print(f"[bot] {symbol}: breakout fired but ATR could not be computed. Skipping.")
        return None

    trigger_desc = (
        f"trend following {trend}: daily close vs EMA{cfg.trend_ema_period} = {trend} | "
        f"4h close {entry['entry']:.6g} broke the {cfg.breakout_lookback}-candle "
        f"{'high' if trend == 'bullish' else 'low'} | ATR{cfg.atr_period}={entry['atr']:.6g}, "
        f"stop {cfg.stop_atr_mult:g} ATR, trail {cfg.trail_atr_mult:g} ATR, no take-profit"
        + (f" | {mom_desc}" if mom_desc else "")
    )
    strength = scores.get(symbol, 0.0) * (1 if trend == "bullish" else -1)
    return {"symbol": symbol, "direction": trend, "trigger_desc": trigger_desc,
            "entry": entry, "strength": strength}


def _try_open_candidate(client, risk, cfg, candidate, equity):
    symbol, entry, trigger_desc = candidate["symbol"], candidate["entry"], candidate["trigger_desc"]

    qty, leverage = risk.position_size(
        equity, entry["entry"], entry["stop_loss"], existing_notional=risk.total_open_notional(),
    )
    if qty <= 0:
        print(f"[bot] {symbol}: no leverage keeps a stop-out within the risk cap for this stop -- skipping.")
        return False
    notional = qty * entry["entry"]
    if notional < cfg.min_notional_usdt:
        print(f"[bot] {symbol}: size ~${notional:.2f} is below MIN_NOTIONAL_USDT -- skipping.")
        return False

    print(
        f"[bot] {symbol}: signal {entry['side'].upper()} qty~{qty:.6f} entry~{entry['entry']:.6g} "
        f"SL={entry['stop_loss']:.6g} trail={entry['trail_distance']:.6g} lev={leverage}x "
        f"margin~${notional / leverage:.2f}"
    )

    trade_id = risk.new_trade_id()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    mode = "DRY_RUN" if cfg.dry_run else "LIVE"

    risk_row = {
        "trade_id": trade_id, "timestamp_utc": now_iso, "mode": mode, "symbol": symbol,
        "side": entry["side"].upper(), "position_side": entry["position_side"],
        "qty": round(qty, 6), "price": round(entry["entry"], 6),
        "stop_loss": round(entry["stop_loss"], 6), "take_profit": "",
        "equity": round(equity, 2), "trigger": trigger_desc,
    }
    tracked = {
        "trade_id": trade_id, "entry_time_utc": now_iso, "mode": mode, "symbol": symbol,
        "side": entry["side"].upper(), "position_side": entry["position_side"],
        "qty": round(qty, 6), "entry_price": round(entry["entry"], 6),
        "stop_loss": round(entry["stop_loss"], 6), "take_profit": None,
        "trail_distance": entry["trail_distance"], "entry_candle_ts": entry["entry_candle_ts"],
        "trigger": trigger_desc,
    }

    if cfg.dry_run:
        risk.log_trade(risk_row)
        risk.track_dry_run_trade(tracked)
        print(f"[bot] {symbol}: DRY_RUN is on -- no order sent (logged to trade journal).")
        return True

    client.set_leverage(symbol, leverage)
    client.create_market_order_with_stops(
        symbol=symbol, side=entry["side"], amount=qty,
        stop_loss_price=entry["stop_loss"], take_profit_price=None,
        position_side=entry["position_side"],
    )
    risk.log_trade(risk_row)
    risk.track_open_trade(tracked)
    print(f"[bot] {symbol}: order submitted with a reduce-only stop, logged.")
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
    warning = risk.check_equity_jump(equity)
    if warning:
        print(f"[bot] WARNING: {warning}")

    open_positions = client.fetch_open_positions()
    exchange_open_symbols = {p.get("symbol") for p in open_positions}
    closed = risk.reconcile_closed_trades(open_positions, client)
    if closed:
        print(f"[bot] Reconciled {len(closed)} closed trade(s), logged EXIT row(s).")

    df_cache, daily_cache = {}, {}
    for symbol in sorted(risk.occupied_symbols()):
        df_cache[symbol] = _manage_open_trades(client, risk, cfg, symbol)

    if risk.circuit_breaker_tripped(equity):
        print("[bot] Daily loss circuit breaker is tripped. No new entries today.")
        return

    occupied = risk.occupied_symbols() | exchange_open_symbols
    print(
        f"[bot] Equity=${equity:.2f} | {len(occupied)}/{cfg.max_concurrent_positions} positions open "
        f"({sorted(occupied) or 'none'}) | watching {len(cfg.symbols)} symbols"
    )
    if len(occupied) >= cfg.max_concurrent_positions:
        print("[bot] Already at max concurrent positions. Skipping scan this cycle.")
        return

    scores = _momentum_ranks(client, cfg, daily_cache)

    candidates = []
    for symbol in cfg.symbols:
        if symbol in occupied:
            print(f"[bot] {symbol}: already have an open position, skipping.")
            continue
        c = _find_candidate(client, cfg, symbol, df_cache, daily_cache, scores)
        if c:
            candidates.append(c)

    candidates.sort(key=lambda c: c["strength"], reverse=True)
    open_count = len(occupied)
    for c in candidates:
        if open_count >= cfg.max_concurrent_positions:
            print(f"[bot] {c['symbol']}: signal skipped, max concurrent positions reached.")
            continue
        if _try_open_candidate(client, risk, cfg, c, equity):
            open_count += 1


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        print("[bot] Unhandled exception:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
