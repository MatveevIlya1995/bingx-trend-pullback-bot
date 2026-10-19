"""
Backtest of the trend-following strategy on real BingX USDT-M perpetual
candles, using the bot's own configuration (env vars / repo Variables).

    python3 backtest.py --days 365

What it simulates, candle by candle on CLOSED 4h candles (same as bot.py):
- open trades first: if the candle's range reaches the stop, the trade
  exits at the stop (or at the candle's open if it gapped through it);
  otherwise the trailing stop ratchets with the candle's extreme;
- then new entries: daily trend filter (daily candles closed by then),
  first 4h close beyond the BREAKOUT_LOOKBACK channel, optional momentum
  filter, strongest momentum first when slots are limited;
- entry at the signal candle's close plus slippage; sizing with the bot's
  own RiskManager.position_size() on current (realized) equity;
- the daily-loss circuit breaker on realized P/L per UTC day.

Costs: taker fee on entry and exit (BingX base tier 0.05%), slippage on
both, and a funding charge (default 0.01% per 8h, the usual baseline)
taken from every position, long or short -- conservative, since shorts
usually receive funding.

Limits: no intra-candle path, no partial fills, funding is a flat rate,
and every variant beyond the baseline was chosen after seeing the data
(see the "Reading this" notes in the report).
"""
import argparse
import csv
import itertools
import math
import os
import sys
import tempfile
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from config import load_config
from risk_manager import RiskManager

H4 = pd.Timedelta(hours=4)
D1 = pd.Timedelta(days=1)


# --------------------------------------------------------------- data ----

def fetch_all(exchange, symbol, timeframe, since_ms, until_ms, cache_dir=None):
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{symbol.replace('/', '_').replace(':', '_')}_{timeframe}_{since_ms}_{until_ms}.csv")
        if os.path.exists(path):
            return pd.read_csv(path, parse_dates=["ts"])
    step = exchange.parse_timeframe(timeframe) * 1000
    rows, cursor = [], since_ms
    while cursor < until_ms:
        for attempt in range(4):
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(2 + attempt * 3)
        if not batch:
            break
        rows.extend(batch)
        last = batch[-1][0]
        if last + step <= cursor:
            break
        cursor = last + step
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df = df[(df["ts"] >= since_ms) & (df["ts"] < until_ms)].sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    if path:
        df.to_csv(path, index=False)
    return df


def synthetic(symbol, timeframe, start, end, seed):
    """Random-walk candles with regime changes, for testing the code path."""
    rng = np.random.default_rng(seed + hash(symbol) % 1000)
    freq = {"4h": "4h", "1d": "1D"}[timeframe]
    ts = pd.date_range(start, end, freq=freq, inclusive="left")
    vol = 0.012 if timeframe == "4h" else 0.03
    drift = np.repeat(rng.normal(0, vol / 3, len(ts) // 60 + 1), 60)[: len(ts)]
    c = 100 * np.exp(np.cumsum(drift + rng.normal(0, vol, len(ts))))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, vol / 2, len(ts)))
    l = np.minimum(o, c) * (1 - rng.uniform(0, vol / 2, len(ts)))
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


# ---------------------------------------------------------- indicators ---

def prepare(h4, d1, p):
    """Precomputes per-candle signals so the simulation loop is cheap."""
    h4 = h4.copy()
    prev_close = h4["close"].shift(1)
    tr = pd.concat([h4["high"] - h4["low"], (h4["high"] - prev_close).abs(),
                    (h4["low"] - prev_close).abs()], axis=1).max(axis=1)
    h4["atr"] = tr.ewm(alpha=1.0 / p["atr_period"], adjust=False).mean()
    n = p["breakout_lookback"]
    upper = h4["high"].rolling(n).max().shift(1)
    lower = h4["low"].rolling(n).min().shift(1)
    long_now = h4["close"] > upper
    short_now = h4["close"] < lower
    # first close outside the channel (edge-triggered, as in strategy.donchian_breakout)
    h4["breakout"] = np.where(long_now & ~long_now.shift(1, fill_value=False), 1,
                              np.where(short_now & ~short_now.shift(1, fill_value=False), -1, 0))

    d1 = d1.copy()
    ema = d1["close"].ewm(span=p["trend_ema_period"], adjust=False).mean()
    trend = np.where((d1["close"] > ema) & (ema > ema.shift(1)), 1,
                     np.where((d1["close"] < ema) & (ema < ema.shift(1)), -1, 0))
    trend[: p["trend_ema_period"] + 1] = 0
    d1["trend"] = trend
    d1["mom"] = d1["close"] / d1["close"].shift(p["momentum_lookback_days"]) - 1
    d1["avail"] = d1["ts"] + D1  # a daily candle is usable once it has closed

    # attach the latest CLOSED daily candle to each 4h candle (at its close time)
    h4["close_time"] = h4["ts"] + H4
    merged = pd.merge_asof(h4.sort_values("close_time"), d1[["avail", "trend", "mom"]].sort_values("avail"),
                           left_on="close_time", right_on="avail", direction="backward")
    return merged.set_index("ts")


# ---------------------------------------------------------- simulation ---

def simulate(data, cfg, p, start_ts, start_equity, fee_pct, slip_pct, funding_pct_8h):
    """data: {symbol: prepared 4h frame}. Returns (trades, equity_curve)."""
    with tempfile.TemporaryDirectory() as tmp:
        sizer = RiskManager(replace(cfg, state_path=os.path.join(tmp, "s.json"),
                                    trades_csv_path=os.path.join(tmp, "t.csv"),
                                    log_path=os.path.join(tmp, "t.log")))
    sizer.cfg = replace(sizer.cfg, max_concurrent_positions=p["max_positions"])

    all_ts = sorted(set().union(*[set(df.index[df.index >= start_ts]) for df in data.values()]))
    equity, open_trades, trades, curve = start_equity, {}, [], []
    day, day_start_equity = None, equity

    for ts in all_ts:
        if ts.normalize() != day:
            day, day_start_equity = ts.normalize(), equity

        # 1. manage open trades with this candle
        for sym in list(open_trades):
            df = data[sym]
            if ts not in df.index:
                continue
            c = df.loc[ts]
            t = open_trades[sym]
            long = t["dir"] == 1
            hit = (c["low"] <= t["stop"]) if long else (c["high"] >= t["stop"])
            if hit:
                raw_exit = min(c["open"], t["stop"]) if long else max(c["open"], t["stop"])
                exit_px = raw_exit * (1 - slip_pct) if long else raw_exit * (1 + slip_pct)
                gross = (exit_px - t["entry"]) * t["qty"] * t["dir"]
                fee = t["qty"] * exit_px * fee_pct
                held_h = (ts + H4 - t["entry_time"]).total_seconds() / 3600
                funding = t["qty"] * t["entry"] * funding_pct_8h * held_h / 8
                pnl = gross - fee - t["entry_fee"] - funding
                equity += pnl
                trades.append({
                    "symbol": sym, "side": "LONG" if long else "SHORT",
                    "entry_time_utc": t["entry_time"].isoformat(), "exit_time_utc": (ts + H4).isoformat(),
                    "entry": t["entry"], "exit": exit_px, "initial_stop": t["initial_stop"],
                    "qty": t["qty"], "leverage": t["lev"], "risk_usd": t["risk_usd"],
                    "pnl": pnl, "r_multiple": pnl / t["risk_usd"] if t["risk_usd"] else 0.0,
                    "fees": fee + t["entry_fee"], "funding": funding, "hours_held": held_h,
                    "exit_kind": "trailing stop" if t["stop"] != t["initial_stop"] else "initial stop",
                })
                del open_trades[sym]
                continue
            if long:
                t["best"] = max(t["best"], c["high"])
                t["stop"] = max(t["stop"], t["best"] - t["trail"])
            else:
                t["best"] = min(t["best"], c["low"])
                t["stop"] = min(t["stop"], t["best"] + t["trail"])

        curve.append((ts + H4, equity))

        # 2. daily-loss circuit breaker (realized)
        if day_start_equity > 0 and (day_start_equity - equity) / day_start_equity >= cfg.max_daily_loss_pct:
            continue
        if len(open_trades) >= p["max_positions"]:
            continue

        # 3. new signals on this candle
        moms = {s: data[s].loc[ts, "mom"] for s in data if ts in data[s].index and not math.isnan(data[s].loc[ts, "mom"])}
        ordered = sorted(moms, key=moms.get, reverse=True)
        k = max(1, int(round(len(ordered) * p["momentum_top_fraction"]))) if ordered else 0
        cands = []
        for sym, df in data.items():
            if sym in open_trades or ts not in df.index:
                continue
            c = df.loc[ts]
            d = int(c["breakout"])
            if d == 0 or int(c["trend"] if not math.isnan(c["trend"]) else 0) != d:
                continue
            if not (c["atr"] > 0):
                continue
            if p["momentum_filter"]:
                if sym not in moms:
                    continue
                if d == 1 and sym not in ordered[:k]:
                    continue
                if d == -1 and sym not in ordered[-k:]:
                    continue
            strength = moms.get(sym, 0.0) * d
            cands.append((strength, sym, d, c))
        cands.sort(key=lambda x: x[0], reverse=True)

        for strength, sym, d, c in cands:
            if len(open_trades) >= p["max_positions"]:
                break
            entry = c["close"] * (1 + slip_pct * d)
            stop = entry - d * p["stop_atr_mult"] * c["atr"]
            if stop <= 0:
                continue
            notional_open = sum(t["qty"] * t["entry"] for t in open_trades.values())
            qty, lev = sizer.position_size(equity, entry, stop, existing_notional=notional_open)
            if qty <= 0 or qty * entry < cfg.min_notional_usdt:
                continue
            open_trades[sym] = {
                "dir": d, "entry": entry, "stop": stop, "initial_stop": stop, "best": entry,
                "trail": p["trail_atr_mult"] * c["atr"], "qty": qty, "lev": lev,
                "risk_usd": qty * abs(entry - stop), "entry_fee": qty * entry * fee_pct,
                "entry_time": ts + H4,
            }

    # close anything still open at the last price, marked as such
    for sym, t in open_trades.items():
        last = data[sym].iloc[-1]
        exit_px = last["close"]
        gross = (exit_px - t["entry"]) * t["qty"] * t["dir"]
        fee = t["qty"] * exit_px * fee_pct
        held_h = (data[sym].index[-1] + H4 - t["entry_time"]).total_seconds() / 3600
        funding = t["qty"] * t["entry"] * funding_pct_8h * held_h / 8
        pnl = gross - fee - t["entry_fee"] - funding
        equity += pnl
        trades.append({
            "symbol": sym, "side": "LONG" if t["dir"] == 1 else "SHORT",
            "entry_time_utc": t["entry_time"].isoformat(), "exit_time_utc": "",
            "entry": t["entry"], "exit": exit_px, "initial_stop": t["initial_stop"],
            "qty": t["qty"], "leverage": t["lev"], "risk_usd": t["risk_usd"],
            "pnl": pnl, "r_multiple": pnl / t["risk_usd"] if t["risk_usd"] else 0.0,
            "fees": fee + t["entry_fee"], "funding": funding, "hours_held": held_h,
            "exit_kind": "still open (marked at last close)",
        })
    curve.append((curve[-1][0] if curve else start_ts, equity))
    return trades, curve


def stats(trades, curve, start_equity, mid_ts):
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq = np.array([e for _, e in curve]) if curve else np.array([start_equity])
    peak = np.maximum.accumulate(np.r_[start_equity, eq])
    dd = float(((peak - np.r_[start_equity, eq]) / peak).max()) if len(eq) else 0.0
    total = sum(pnl)
    top = max(pnl) if pnl else 0.0
    first = sum(t["pnl"] for t in trades if t["entry_time_utc"] < mid_ts.isoformat())
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "net": total,
        "ret": total / start_equity,
        "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf") if wins else 0.0,
        "avg_r": float(np.mean([t["r_multiple"] for t in trades])) if n else 0.0,
        "max_dd": dd,
        "fees": sum(t["fees"] for t in trades),
        "funding": sum(t["funding"] for t in trades),
        "first": first,
        "second": total - first,
        "top_share": (top / total) if total > 0 else float("nan"),
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
    }


def row(name, s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"| {name} | {s['trades']} | {s['win_rate']:.1%} | {s['net']:+.2f} | {s['ret']:+.1%} | {pf} | "
            f"{s['avg_r']:+.2f} | {s['max_dd']:.1%} | {s['fees']:.2f} | {s['funding']:.2f} | "
            f"{s['first']:+.2f} / {s['second']:+.2f} |")


HEADER = ("| variant | trades | win rate | net P/L $ | return | profit factor | avg R | max drawdown | fees $ | "
          "funding $ | P/L 1st half / 2nd half |\n|---|---|---|---|---|---|---|---|---|---|---|")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--end", default=None, help="last day (exclusive), YYYY-MM-DD UTC; default today")
    ap.add_argument("--start-equity", type=float, default=None, help="default: 250 USDT")
    ap.add_argument("--fee-pct", type=float, default=0.05, help="taker fee per side, %%")
    ap.add_argument("--slippage-pct", type=float, default=0.02, help="per fill, %% of price")
    ap.add_argument("--funding-pct-8h", type=float, default=0.01, help="charged on every position, %% per 8h")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--out", default="backtest_results")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--synthetic", type=int, default=None, metavar="SEED", help="random data, to test the code")
    args = ap.parse_args()

    cfg = load_config()
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else cfg.symbols
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    start = end - pd.Timedelta(days=args.days)
    warm = start - pd.Timedelta(days=max(cfg.trend_ema_period, cfg.momentum_lookback_days) * 2 + 10)
    start_equity = args.start_equity or 250.0
    fee, slip, fund = args.fee_pct / 100, args.slippage_pct / 100, args.funding_pct_8h / 100

    raw = {}
    if args.synthetic is None:
        import ccxt
        ex = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        ex.load_markets()
        for s in symbols:
            if s not in ex.markets:
                print(f"[backtest] {s}: not listed on BingX, skipped.")
                continue
            h4 = fetch_all(ex, s, "4h", int(warm.value // 10**6), int(end.value // 10**6), args.cache_dir)
            d1 = fetch_all(ex, s, "1d", int(warm.value // 10**6), int(end.value // 10**6), args.cache_dir)
            print(f"[backtest] {s}: {len(h4)} 4h and {len(d1)} daily candles from {h4['ts'].min() if len(h4) else '-'}")
            if len(h4) > 100 and len(d1) > cfg.trend_ema_period + 5:
                raw[s] = (h4, d1)
    else:
        for s in symbols:
            raw[s] = (synthetic(s, "4h", warm, end, args.synthetic), synthetic(s, "1d", warm, end, args.synthetic))
    if not raw:
        sys.exit("[backtest] no data")

    base = {
        "breakout_lookback": cfg.breakout_lookback, "atr_period": cfg.atr_period,
        "stop_atr_mult": cfg.stop_atr_mult, "trail_atr_mult": cfg.trail_atr_mult,
        "trend_ema_period": cfg.trend_ema_period, "momentum_filter": cfg.momentum_filter,
        "momentum_lookback_days": cfg.momentum_lookback_days,
        "momentum_top_fraction": cfg.momentum_top_fraction,
        "max_positions": cfg.max_concurrent_positions,
    }
    mid = start + (end - start) / 2
    cache = {}

    def run(**over):
        p = {**base, **over}
        key = (p["breakout_lookback"], p["atr_period"], p["trend_ema_period"], p["momentum_lookback_days"])
        if key not in cache:
            cache[key] = {s: prepare(h4, d1, p) for s, (h4, d1) in raw.items()}
        t, c = simulate(cache[key], cfg, p, start, start_equity, fee, slip, fund)
        return t, c, stats(t, c, start_equity, mid)

    base_trades, base_curve, base_stats = run()

    sections = []
    sections.append(("Positions allowed at once (MAX_CONCURRENT_POSITIONS)",
                     [(f"{m}{' (current)' if m == base['max_positions'] else ''}", run(max_positions=m)[2])
                      for m in sorted({1, 3, 5, base["max_positions"]})]))
    sections.append(("Momentum filter (MOMENTUM_FILTER)",
                     [(f"{'on' if v else 'off'}{' (current)' if v == base['momentum_filter'] else ''}",
                       run(momentum_filter=v)[2]) for v in (False, True)]))
    sections.append(("Breakout lookback in 4h candles (BREAKOUT_LOOKBACK)",
                     [(f"{n}{' (current)' if n == base['breakout_lookback'] else ''}", run(breakout_lookback=n)[2])
                      for n in sorted({10, 20, 40, 60, base["breakout_lookback"]})]))
    combos = sorted({(2.0, 3.0), (2.5, 3.0), (3.0, 4.0), (2.0, 2.0), (4.0, 5.0),
                     (base["stop_atr_mult"], base["trail_atr_mult"])})
    sections.append(("Stop / trail width in ATRs (STOP_ATR_MULT / TRAIL_ATR_MULT)",
                     [(f"{a:g} / {b:g}{' (current)' if (a, b) == (base['stop_atr_mult'], base['trail_atr_mult']) else ''}",
                       run(stop_atr_mult=a, trail_atr_mult=b)[2]) for a, b in combos]))

    # buy-and-hold BTC for reference
    bh = ""
    btc = next((s for s in raw if s.startswith("BTC/")), None)
    if btc:
        h4 = raw[btc][0]
        h4p = h4[h4["ts"] >= start]
        if len(h4p):
            bh = f"BTC buy-and-hold over the same period: {h4p['close'].iloc[-1] / h4p['open'].iloc[0] - 1:+.1%} (unlevered)."

    by_sym = {}
    for t in base_trades:
        a = by_sym.setdefault(t["symbol"], [0, 0, 0.0])
        a[0] += 1
        a[1] += t["pnl"] > 0
        a[2] += t["pnl"]
    by_exit = {}
    for t in base_trades:
        a = by_exit.setdefault(t["exit_kind"], [0, 0.0])
        a[0] += 1
        a[1] += t["pnl"]
    by_side = {}
    for t in base_trades:
        a = by_side.setdefault(t["side"], [0, 0, 0.0])
        a[0] += 1
        a[1] += t["pnl"] > 0
        a[2] += t["pnl"]

    s = base_stats
    held = np.median([t["hours_held"] for t in base_trades]) if base_trades else 0
    lines = [
        f"# Trend-following backtest: {start.date()} to {end.date()} ({args.days} days)",
        "",
        f"Data: {'SYNTHETIC random candles (code test only)' if args.synthetic is not None else 'BingX USDT-M perpetual 4h and daily candles'}. "
        f"{len(raw)} symbols: {', '.join(x.split('/')[0] for x in raw)}.",
        "",
        f"Settings: daily EMA{base['trend_ema_period']} trend filter, {base['breakout_lookback']}-candle 4h breakout, "
        f"stop {base['stop_atr_mult']:g} ATR{base['atr_period']}, trail {base['trail_atr_mult']:g} ATR, no take-profit, "
        f"momentum filter {'on' if base['momentum_filter'] else 'off'}, max {base['max_positions']} positions, "
        f"risk {cfg.risk_pct_per_trade:.1%} per trade, ${cfg.margin_per_trade_usdt:.0f} margin, leverage cap {cfg.max_leverage_hard_cap}x, "
        f"daily loss limit {cfg.max_daily_loss_pct:.0%}.",
        f"Costs: taker fee {args.fee_pct}% per side, slippage {args.slippage_pct}% per fill, funding {args.funding_pct_8h}% per 8h on every position. "
        f"Start equity ${start_equity:.0f}.",
        "",
        "## Result with the current settings",
        "",
        HEADER,
        row("current settings", s),
        "",
        f"Average win ${s['avg_win']:.2f}, average loss ${s['avg_loss']:.2f}, median holding time {held:.0f} hours. "
        + (f"The single best trade is {s['top_share']:.0%} of total profit. " if s["net"] > 0 else "")
        + bh,
        "",
    ]
    for title, rows_ in sections:
        lines += [f"## {title}", "", HEADER] + [row(n, st) for n, st in rows_] + [""]
    lines += ["## Current settings by side", "", "| side | trades | win rate | net P/L $ |", "|---|---|---|---|"]
    lines += [f"| {k} | {v[0]} | {v[1] / v[0]:.0%} | {v[2]:+.2f} |" for k, v in sorted(by_side.items())] + [""]
    lines += ["## Current settings by symbol", "", "| symbol | trades | win rate | net P/L $ |", "|---|---|---|---|"]
    lines += [f"| {k.split('/')[0]} | {v[0]} | {v[1] / v[0]:.0%} | {v[2]:+.2f} |"
              for k, v in sorted(by_sym.items(), key=lambda kv: -kv[1][2])] + [""]
    lines += ["## How trades ended", "", "| exit | trades | net P/L $ |", "|---|---|---|"]
    lines += [f"| {k} | {v[0]} | {v[1]:+.2f} |" for k, v in by_exit.items()] + [""]
    lines += [
        "## Reading this",
        "",
        "- Simulated fills on historical candles. Real results will differ (slippage in fast moves, funding, outages).",
        "- Trend following wins on a few big trades and loses small on many; a win rate of 30-45% is normal.",
        "- Only the current-settings row is a fair test. Picking the best variant from these tables is fitting to this period; prefer settings that do well in BOTH halves.",
        "",
    ]
    report = "\n".join(lines)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "summary.md"), "w") as f:
        f.write(report)
    with open(os.path.join(args.out, "trades.csv"), "w", newline="") as f:
        if base_trades:
            w = csv.DictWriter(f, fieldnames=list(base_trades[0].keys()))
            w.writeheader()
            w.writerows(base_trades)
    with open(os.path.join(args.out, "equity.csv"), "w") as f:
        f.write("time_utc,equity\n" + "\n".join(f"{t.isoformat()},{e:.4f}" for t, e in base_curve))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    print(report)


if __name__ == "__main__":
    main()
