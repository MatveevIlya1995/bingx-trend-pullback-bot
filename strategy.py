"""
Trend-following strategy: 4h Donchian breakout, filtered by the daily
trend, with ATR-based stops that trail the trend and no fixed target.

Why this design
---------------
Published research on crypto finds that trends persist on multi-day
horizons (weekly momentum, multi-day moving-average and breakout rules),
while intraday signals rarely beat fees. Trend following makes money from
a few large winners and loses small amounts on many false breakouts, so
the two things that matter most are (1) never capping a winner with a
fixed take-profit and (2) keeping each loss to a fixed slice of equity.

How a trade gets taken
----------------------
1. Daily trend filter -- detect_trend() on CLOSED daily candles: last
   close above a rising EMA(TREND_EMA_PERIOD, default 50) = bullish,
   below a falling one = bearish, anything else = neutral (no trade).
   Longs only in a bullish daily trend, shorts only in a bearish one.

2. 4h breakout trigger -- donchian_breakout(): the last CLOSED 4h candle
   closes above the highest high of the previous BREAKOUT_LOOKBACK 4h
   candles (default 20, about 3.3 days) for a long, or below the lowest
   low for a short. It is edge-triggered: it fires only on the FIRST
   candle that closes outside the channel, so the bot doesn't chase a
   move that broke out days ago.

3. Optional momentum filter (MOMENTUM_FILTER, see bot.py): rank every
   watched symbol by its MOMENTUM_LOOKBACK_DAYS return and only take
   longs in the strongest MOMENTUM_TOP_FRACTION of coins and shorts in the
   weakest -- the cross-sectional momentum factor.

Stops and exits
---------------
- Initial stop: STOP_ATR_MULT x ATR(ATR_PERIOD) beyond the entry
  (default 2.5 x ATR14 on 4h). Wide enough to survive normal noise.
- Trailing stop: from the first candle, the stop trails
  TRAIL_ATR_MULT x ATR (default 3, the ATR measured at entry) behind the
  best price reached -- a "chandelier" stop. It only ever moves in the
  trade's favour; the initial stop stays until the trail passes it.
- No take-profit order: the trade ends only when the trailing stop is hit.
- Size: risk_manager.position_size() keeps a stop-out at or below
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


def donchian_breakout(df: pd.DataFrame, lookback: int = 20) -> str | None:
    """"bullish" if the last closed candle is the FIRST to close above the
    highest high of the `lookback` candles before it, "bearish" for the
    mirror case, else None."""
    if len(df) < lookback + 2:
        return None
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    i = len(df) - 1
    upper_now, lower_now = highs[i - lookback:i].max(), lows[i - lookback:i].min()
    upper_prev, lower_prev = highs[i - 1 - lookback:i - 1].max(), lows[i - 1 - lookback:i - 1].min()
    if closes[i] > upper_now and closes[i - 1] <= upper_prev:
        return "bullish"
    if closes[i] < lower_now and closes[i - 1] >= lower_prev:
        return "bearish"
    return None


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
    atr_period: int = 14,
    stop_atr_mult: float = 2.5,
    trail_atr_mult: float = 3.0,
):
    """Entry at the last 4h close, stop STOP_ATR_MULT x ATR away, trailing
    distance TRAIL_ATR_MULT x ATR. Returns None if ATR can't be computed."""
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
