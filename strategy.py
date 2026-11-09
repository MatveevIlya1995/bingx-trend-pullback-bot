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
