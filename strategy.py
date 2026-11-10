"""
Trend-following strategy: DAILY Donchian breakout with a channel + ATR
trailing stop and no fixed target.

Why this design
---------------
Tested on 15 large-cap BingX perpetuals since 2022 (research.py in the
order-block bot repo: one account, fees, slippage, funding on longs):
every daily Donchian breakout from 20 to 55 days made money both in
2022 - Apr 2025 and in the held-out stretch after it; the 4h version
of the same rule had 40-60% drawdowns, and intraday smart-money entries
lost on every timeframe. Trend following makes money from a few large
winners and loses small amounts on many false breakouts, so it needs
(1) no take-profit capping winners and (2) a fixed slice of equity
risked per trade.

How a trade gets taken (defaults = the tested rule)
-------------------------------------------------
1. Breakout -- donchian_breakout(): the last CLOSED daily candle closes
   above the highest high of the previous BREAKOUT_LOOKBACK (55) daily
   candles for a long, or below the lowest low for a short. With
   BREAKOUT_EDGE_ONLY=false (default, as tested) a flat symbol whose
   close is still beyond the channel is entered again after a stop-out;
   true = only the first close outside the channel.
2. Optional daily EMA trend filter (TREND_FILTER, off by default --
   the tested rule has none; the EMA-filtered versions did slightly worse).
3. Optional momentum filter (MOMENTUM_FILTER, off by default, see bot.py).

Stops and exits
---------------
Since 2026-11-10 new trades use exit plan B (EXIT_PLAN=structure, see
structure_step at the bottom of this file). The description below is
exit plan A (EXIT_PLAN=channel), kept for trades opened before that.
- Initial stop: STOP_ATR_MULT (2) x ATR(ATR_PERIOD=20) beyond the entry.
- Trailing stop, moved after every closed daily candle and never
  backwards: the tighter of the EXIT_LOOKBACK (20)-day channel (lowest
  low for a long, highest high for a short, of the 20 candles before the
  latest) and the latest close minus TRAIL_ATR_MULT (2) x the current ATR.
- No take-profit order: the trade ends only when the stop is hit.
- Size: risk_manager.position_size() keeps a stop-out at
  RISK_PCT_PER_TRADE of equity.
"""
import numpy as np
import pandas as pd


def ohlcv_to_df(raw) -> pd.DataFrame:
    """raw: list of [timestamp_ms, open, high, low, close, volume] as returned by ccxt."""
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


_TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080, "1M": 43200,
}


def drop_incomplete_last_candle(df: pd.DataFrame, timeframe: str, now=None) -> pd.DataFrame:
    """ccxt's fetch_ohlcv usually includes the still-forming candle as the
    last row. Every signal here must see CLOSED candles only, so drop the
    last row if its close time is still in the future. `now` can be
    passed in (naive UTC Timestamp) by the backtest."""
    minutes = _TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None or df.empty:
        return df
    close_time = df.iloc[-1]["ts"] + pd.Timedelta(minutes=minutes)
    if now is None:
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    if close_time > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def detect_trend(df: pd.DataFrame, ema_period: int = 50) -> str:
    """"bullish" if the last close is above a rising EMA(ema_period),
    "bearish" if below a falling one, else "neutral" (also when there
    isn't enough history)."""
    if len(df) < ema_period + 2:
        return "neutral"
    ema = df["close"].ewm(span=ema_period, adjust=False).mean()
    last_close = df["close"].iloc[-1]
    if last_close > ema.iloc[-1] and ema.iloc[-1] > ema.iloc[-2]:
        return "bullish"
    if last_close < ema.iloc[-1] and ema.iloc[-1] < ema.iloc[-2]:
        return "bearish"
    return "neutral"


def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder's Average True Range of the last `period` candles."""
    if len(df) < period + 1:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    value = tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    return float(value) if np.isfinite(value) and value > 0 else None


def donchian_breakout(df: pd.DataFrame, lookback: int = 55, edge_only: bool = False) -> str | None:
    """"bullish" if the last closed candle closes at or above the highest
    high of the `lookback` candles before it, "bearish" at or below the
    lowest low, else None. edge_only=True: only the FIRST close beyond the
    channel counts (the previous close was still inside)."""
    if len(df) < lookback + 2:
        return None
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    i = len(df) - 1
    upper_now, lower_now = highs[i - lookback:i].max(), lows[i - lookback:i].min()
    upper_prev, lower_prev = highs[i - 1 - lookback:i - 1].max(), lows[i - 1 - lookback:i - 1].min()
    if closes[i] >= upper_now and not (edge_only and closes[i - 1] >= upper_prev):
        return "bullish"
    if closes[i] <= lower_now and not (edge_only and closes[i - 1] <= lower_prev):
        return "bearish"
    return None


def trail_levels(df: pd.DataFrame, exit_lookback: int = 20, atr_period: int = 20,
                 trail_atr_mult: float = 2.0):
    """Where the trailing stop may be moved to after the last closed
    candle: (long_level, short_level). Long: the higher of the lowest low
    of the `exit_lookback` candles BEFORE the last one and last close -
    trail_atr_mult x ATR. Short: the mirror (the lower of the two).
    Returns (None, None) when there isn't enough history."""
    if len(df) < max(exit_lookback, atr_period) + 2:
        return None, None
    a = atr(df, atr_period)
    if a is None:
        return None, None
    close = float(df["close"].iloc[-1])
    lo = float(df["low"].iloc[-1 - exit_lookback:-1].min())
    hi = float(df["high"].iloc[-1 - exit_lookback:-1].max())
    return max(lo, close - trail_atr_mult * a), min(hi, close + trail_atr_mult * a)


def momentum_return(daily_df: pd.DataFrame, lookback_days: int = 30) -> float | None:
    """Return over the last `lookback_days` closed daily candles."""
    if len(daily_df) < lookback_days + 1:
        return None
    start = float(daily_df["close"].iloc[-1 - lookback_days])
    end = float(daily_df["close"].iloc[-1])
    return end / start - 1.0 if start > 0 else None


def build_trend_entry(
    entry_df: pd.DataFrame,
    direction: str,
    atr_period: int = 20,
    stop_atr_mult: float = 2.0,
    trail_atr_mult: float = 2.0,
):
    """Entry at the last closed candle's close, stop STOP_ATR_MULT x ATR
    away. trail_distance (TRAIL_ATR_MULT x ATR at entry) is kept for the
    old chandelier trail of trades opened before the channel exit.
    Returns None if ATR can't be computed."""
    a = atr(entry_df, atr_period)
    if a is None:
        return None
    entry = float(entry_df["close"].iloc[-1])
    if direction == "bullish":
        stop = entry - stop_atr_mult * a
        side, position_side = "buy", "LONG"
    elif direction == "bearish":
        stop = entry + stop_atr_mult * a
        side, position_side = "sell", "SHORT"
    else:
        return None
    if stop <= 0:
        return None
    return {
        "side": side,
        "position_side": position_side,
        "entry": entry,
        "stop_loss": stop,
        "take_profit": None,
        "atr": a,
        "trail_distance": trail_atr_mult * a,
        "entry_candle_ts": entry_df["ts"].iloc[-1].isoformat(),
    }


# --------------------------------------------------------------------------
# Exit plan B ("structure"): chosen 2026-11-10 after research_mm.py
# (RESEARCH_NOTES.md section 4). R = the initial stop distance.
#   TP1 at 3R closes 1/3, TP2 at 5R closes 1/3, the last 1/3 runs.
#   Structure is read on closed 1h candles (fractal swings, FRACTAL_N
#   candles each side, confirmed FRACTAL_N candles later; only swings formed
#   after the entry hour count). First 1h BOS in the trade's direction (a
#   close beyond the last swing high for a long): stop to the entry. Every
#   later BOS: stop to the low the breaking move started from (lowest low
#   since the broken swing high), never behind the entry. The runner ends at
#   the first CHoCH against the trade = that trailed stop being hit.
#   Within one candle the stop counts before a target, unless the candle
#   opens beyond the target (same as the backtest).
# --------------------------------------------------------------------------
def structure_targets(entry: float, stop: float, qty: float, tp1_r: float = 3.0, tp2_r: float = 5.0,
                      tp1_fraction: float = 1 / 3, tp2_fraction: float = 1 / 3) -> list:
    """The two partial take-profits of a new trade: [{label, price, qty}]."""
    risk = abs(entry - stop)
    d = 1 if entry > stop else -1
    return [
        {"label": f"TP1 {tp1_r:g}R", "price": entry + d * tp1_r * risk, "qty": qty * tp1_fraction},
        {"label": f"TP2 {tp2_r:g}R", "price": entry + d * tp2_r * risk, "qty": qty * tp2_fraction},
    ]


def _fractals(high, low, n):
    sh = np.zeros(len(high), bool)
    sl = np.zeros(len(high), bool)
    if len(high) > 2 * n:
        w = np.lib.stride_tricks.sliding_window_view
        sh[n:len(high) - n] = w(high, 2 * n + 1).argmax(axis=1) == n
        sl[n:len(low) - n] = w(low, 2 * n + 1).argmin(axis=1) == n
    return sh, sl


def structure_step(trade: dict, h1: pd.DataFrame, fractal_n: int = 2, simulate_fills: bool = True):
    """Walks the trade through every CLOSED 1h candle it hasn't seen yet
    (h1: ts/open/high/low/close, oldest first). Updates trade in place:
    stop_loss, tps (still open targets), qty, ref_swing, bos_count,
    last_structure_ts. Returns (events, done) where events is a list of
    (reason, qty, price) fills -- only when simulate_fills (paper trades;
    live fills happen on the exchange)."""
    if h1 is None or h1.empty:
        return [], False
    is_long = trade["position_side"].upper() == "LONG"
    side = 1 if is_long else -1
    ts = pd.to_datetime(h1["ts"]).values
    o, hi, lo, c = (h1[k].values.astype(float) for k in ("open", "high", "low", "close"))
    sh, sl = _fractals(hi, lo, fractal_n)
    start = _naive(trade["structure_from_ts"])
    last_seen = _naive(trade["last_structure_ts"]) if trade.get("last_structure_ts") else None
    entry = float(trade["entry_price"])
    events, done = [], False
    for i in range(len(ts)):
        if ts[i] < start or (last_seen is not None and ts[i] <= last_seen):
            continue
        stop = float(trade["stop_loss"])
        tps = trade.get("tps") or []
        if simulate_fills:
            stop_hit = (lo[i] <= stop) if is_long else (hi[i] >= stop)
            gap_tp = bool(tps) and ((o[i] >= tps[0]["price"]) if is_long else (o[i] <= tps[0]["price"]))
            if stop_hit and not gap_tp:
                px = min(o[i], stop) if is_long else max(o[i], stop)
                events.append((_stop_reason(trade), trade["qty"], px))
                trade["qty"] = 0.0
                done = True
            else:
                while tps and ((hi[i] >= tps[0]["price"]) if is_long else (lo[i] <= tps[0]["price"])):
                    tp = tps.pop(0)
                    q = min(tp["qty"], trade["qty"])
                    px = max(o[i], tp["price"]) if is_long else min(o[i], tp["price"])
                    events.append((tp["label"], q, px))
                    trade["qty"] -= q
                trade["tps"] = tps
                if trade["qty"] <= 1e-12:
                    done = True
                elif stop_hit:
                    events.append((_stop_reason(trade), trade["qty"], stop))
                    trade["qty"] = 0.0
                    done = True
        trade["last_structure_ts"] = pd.Timestamp(ts[i]).isoformat()
        if done:
            break
        # --- after the close: structure and the stop for the next candle
        j = i - fractal_n
        if j >= fractal_n and ts[j] >= start:
            if is_long and sh[j]:
                trade["ref_swing"] = {"ts": pd.Timestamp(ts[j]).isoformat(), "price": float(hi[j]), "broken": False}
            if not is_long and sl[j]:
                trade["ref_swing"] = {"ts": pd.Timestamp(ts[j]).isoformat(), "price": float(lo[j]), "broken": False}
        ref = trade.get("ref_swing")
        if ref and not ref["broken"] and ((c[i] > ref["price"]) if is_long else (c[i] < ref["price"])):
            ref["broken"] = True
            trade["bos_count"] = trade.get("bos_count", 0) + 1
            if trade["bos_count"] == 1:
                new = entry
            else:
                k0 = int(np.searchsorted(ts, _naive(ref["ts"])))
                origin = lo[k0:i + 1].min() if is_long else hi[k0:i + 1].max()
                new = max(origin, entry) if is_long else min(origin, entry)
            trade["stop_loss"] = max(stop, new) if is_long else min(stop, new)
    return events, done


def _naive(t):
    """ISO string / Timestamp -> naive-UTC numpy datetime64 (candle ts are naive UTC)."""
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return np.datetime64(t, "ns")


def _stop_reason(trade):
    if trade.get("bos_count", 0) == 0:
        return "initial stop hit"
    if abs(float(trade["stop_loss"]) - float(trade["entry_price"])) < 1e-12:
        return "breakeven stop hit (after 1h BOS)"
    return "structure stop hit (1h CHoCH)"
