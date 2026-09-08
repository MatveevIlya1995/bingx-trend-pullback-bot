"""
Trend + pullback strategy, analyzed on exactly three timeframes: weekly,
daily and 4h. There is no 15m/1h "noise" level in this bot -- the entry
decision is made entirely from the weekly trend, the daily pullback, and
the 4h trigger.

How a trade gets taken
-----------------------
1. Weekly (primary trend) -- an EMA-slope trend read (see detect_trend())
   on weekly closes. The weekly trend must be clearly bullish or bearish
   for this strategy to look for a trade at all: a pullback needs a trend
   to pull back from, so a neutral weekly reading skips the symbol this
   cycle (see config.require_weekly_trend).

2. Daily (primary trend + pullback zone) -- the same EMA-slope trend read
   is computed on daily closes. If it directly contradicts the weekly
   trend (one bullish, one bearish -- see combine_htf_bias()), the symbol
   is skipped: this strategy only ever trades WITH the weekly trend, and
   only when the daily chart isn't actively fighting it. Unlike the
   order-block bot, there is no weekly/daily override here -- keeping the
   conflict rule simple and unconditional makes this strategy's behavior
   easy to reason about.

   Separately, the daily chart is also where the "pullback" itself is
   identified: has price on the daily chart pulled back far enough, but
   not too far, to be a tradeable retracement inside the weekly trend?
   See daily_pullback_zone() -- it checks that the most recent daily
   candle has touched a daily EMA "value zone" (pullback_ema_period,
   default 20) without closing beyond the daily chart's own last
   confirmed swing low/high (which would mean the pullback has already
   gone deep enough to look like a possible reversal, not a pullback).

3. 4h (entry trigger) -- once the daily pullback zone is active, the 4h
   chart is watched for the actual entry trigger: a 4h close crossing
   back to the trend side of a fast 4h EMA (trigger_ema_period, default
   9) that it was on the wrong side of the previous 4h candle -- see
   entry_trigger(). This is the timing mechanism: the daily pullback zone
   says "a pullback is happening", the 4h trigger says "and it just
   turned back in our favor".

Stop-loss & take-profit -- the stop-loss sits just beyond the 4h chart's
own most recently confirmed swing low (LONG) or swing high (SHORT) -- see
nearest_confirmed_swing_level(), reused from the same N-bar fractal
definition as the order-block bot. If no confirmed 4h swing exists yet in
the fetched window, falls back to the lowest low / highest high of the
last few 4h candles. Take-profit is a fixed reward:risk multiple
(config.py hard-enforces 2:1 regardless of what REWARD_RISK_RATIO is set
to); the position sizer in risk_manager.py separately hard-caps the
stop-loss risk itself at 3% of equity, and risk_manager.py's partial
take-profit + trailing stop manage the runner beyond that fixed target
exactly as they do for the order-block bot.
"""
import pandas as pd
import numpy as np


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


def drop_incomplete_last_candle(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """ccxt's fetch_ohlcv typically includes the currently-forming candle
    as the last row. Every trend/pullback/trigger function in this
    strategy should only ever see CLOSED candles -- an in-progress
    candle's close is still moving and can cross an EMA back and forth
    before it actually closes, which would make entry_trigger()'s
    edge-detection fire on noise instead of a real, settled close.

    Drops the last row if its close time (its open time + one
    timeframe) is still in the future. A no-op for unrecognized
    timeframe strings or an empty df, so callers don't need to special-
    case those."""
    minutes = _TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None or df.empty:
        return df
    last_open = df.iloc[-1]["ts"]
    close_time = last_open + pd.Timedelta(minutes=minutes)
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    if close_time > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def detect_trend(df: pd.DataFrame, ema_period: int = 50) -> str:
    """Trend filter based on an EMA of closes. Returns "bullish" if the
    last close is above a rising EMA(ema_period), "bearish" if it's below
    a falling EMA, and "neutral" if there isn't enough data yet or the
    EMA is flat/ambiguous."""
    if len(df) < ema_period + 2:
        return "neutral"
    ema = df["close"].ewm(span=ema_period, adjust=False).mean()
    last_close = df["close"].iloc[-1]
    last_ema = ema.iloc[-1]
    prev_ema = ema.iloc[-2]
    if last_close > last_ema and last_ema > prev_ema:
        return "bullish"
    if last_close < last_ema and last_ema < prev_ema:
        return "bearish"
    return "neutral"


def combine_htf_bias(trends: list) -> str | None:
    """Fold a list of "bullish"/"bearish"/"neutral" readings into one
    overall bias: neutral readings are skipped; if every non-neutral
    reading agrees, that direction is returned; if any two disagree,
    returns "conflict"; if every level is neutral, returns None (no
    opinion). Used here to combine exactly [weekly_trend, daily_trend]."""
    bias = None
    for t in trends:
        if t == "neutral":
            continue
        if bias is None:
            bias = t
        elif t != bias:
            return "conflict"
    return bias


def find_fractals(df: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    highs = df["high"].values
    lows = df["low"].values
    swing_high = np.zeros(len(df), dtype=bool)
    swing_low = np.zeros(len(df), dtype=bool)

    for i in range(n, len(df) - n):
        window_h = highs[i - n : i + n + 1]
        window_l = lows[i - n : i + n + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == n:
            swing_high[i] = True
        if lows[i] == window_l.min() and np.argmin(window_l) == n:
            swing_low[i] = True

    out = df.copy()
    out["swing_high"] = swing_high
    out["swing_low"] = swing_low
    return out


def nearest_confirmed_swing_level(df: pd.DataFrame, direction: str, n: int = 2):
    """The most recently CONFIRMED swing high (direction="bullish", i.e.
    "give me the resistance above") or swing low (direction="bearish",
    "give me the support below") in df. A swing point needs n bars to
    close on both sides before it's confirmed, so this always lags the
    most extreme price seen so far by up to n bars. Returns None if no
    such point has been confirmed yet in the fetched window."""
    out = find_fractals(df, n=n)
    if direction == "bullish":
        matches = out[out["swing_high"]]
        price_col = "high"
    elif direction == "bearish":
        matches = out[out["swing_low"]]
        price_col = "low"
    else:
        return None
    if matches.empty:
        return None
    return float(matches.iloc[-1][price_col])


def daily_pullback_zone(
    daily_df: pd.DataFrame,
    direction: str,
    pullback_ema_period: int = 20,
    touch_tolerance_pct: float = 0.5,
    fractal_n: int = 2,
) -> dict:
    """Is the most recent daily candle sitting in a tradeable pullback
    zone for `direction` ("bullish" or "bearish")?

    A pullback is considered active when BOTH:
    1. the daily candle has come within touch_tolerance_pct of the daily
       pullback EMA (or traded through it) -- direction="bullish" checks
       the candle's LOW against the EMA (price dipped down to the zone);
       direction="bearish" checks the HIGH (price rallied up to it).
    2. price has NOT closed beyond the daily chart's own last confirmed
       swing low (bullish) / swing high (bearish) -- if it has, this
       looks like a potential trend reversal rather than a pullback
       inside an intact trend, and is deliberately not traded here.

    Returns {"active": bool, "ema": float, "detail": str}. `active` is
    False (with a `detail` explaining why) if there isn't enough daily
    history yet to compute the EMA, or either condition above fails.
    """
    if len(daily_df) < pullback_ema_period + 2:
        return {"active": False, "ema": None, "detail": "not enough daily history for pullback EMA"}

    ema_series = daily_df["close"].ewm(span=pullback_ema_period, adjust=False).mean()
    ema = float(ema_series.iloc[-1])
    last = daily_df.iloc[-1]
    tolerance = ema * (touch_tolerance_pct / 100.0)

    if direction == "bullish":
        touched = float(last["low"]) <= ema + tolerance
        swing_low = nearest_confirmed_swing_level(daily_df, "bearish", n=fractal_n)
        broke_structure = swing_low is not None and float(last["close"]) < swing_low
        detail = (
            f"daily EMA{pullback_ema_period}={ema:.4f}, last low={float(last['low']):.4f} "
            f"({'touched' if touched else 'not touched'}), last confirmed daily swing low="
            f"{swing_low if swing_low is not None else 'none yet'} "
            f"({'BROKEN -- looks like reversal, not pullback' if broke_structure else 'holding'})"
        )
        active = touched and not broke_structure
        return {"active": active, "ema": ema, "detail": detail}

    if direction == "bearish":
        touched = float(last["high"]) >= ema - tolerance
        swing_high = nearest_confirmed_swing_level(daily_df, "bullish", n=fractal_n)
        broke_structure = swing_high is not None and float(last["close"]) > swing_high
        detail = (
            f"daily EMA{pullback_ema_period}={ema:.4f}, last high={float(last['high']):.4f} "
            f"({'touched' if touched else 'not touched'}), last confirmed daily swing high="
            f"{swing_high if swing_high is not None else 'none yet'} "
            f"({'BROKEN -- looks like reversal, not pullback' if broke_structure else 'holding'})"
        )
        active = touched and not broke_structure
        return {"active": active, "ema": ema, "detail": detail}

    return {"active": False, "ema": None, "detail": f"unknown direction {direction!r}"}


def entry_trigger(entry_df: pd.DataFrame, direction: str, trigger_ema_period: int = 9) -> bool:
    """Has the 4h chart just triggered an entry in `direction`? Fires the
    first candle a 4h close crosses back to the trend side of a fast EMA
    (trigger_ema_period) after being on the wrong side the previous 4h
    candle -- i.e. a fresh reclaim/rejection of the fast EMA, timed to
    fire once per pullback rather than on every candle that happens to
    already be on the right side of it."""
    if len(entry_df) < trigger_ema_period + 2:
        return False
    ema = entry_df["close"].ewm(span=trigger_ema_period, adjust=False).mean()
    last_close = float(entry_df["close"].iloc[-1])
    prev_close = float(entry_df["close"].iloc[-2])
    last_ema = float(ema.iloc[-1])
    prev_ema = float(ema.iloc[-2])

    if direction == "bullish":
        return prev_close <= prev_ema and last_close > last_ema
    if direction == "bearish":
        return prev_close >= prev_ema and last_close < last_ema
    return False


def _fallback_extreme(df: pd.DataFrame, direction: str, lookback: int = 20):
    """Used only when no confirmed fractal swing exists yet in the
    fetched window (e.g. right after the bot starts and the entry
    timeframe's history is short) -- the lowest low / highest high of
    the last `lookback` candles stands in for "nearest structure" so a
    stop can still always be placed."""
    window = df.tail(min(lookback, len(df)))
    if window.empty:
        return None
    if direction == "bearish":  # support, for a LONG stop
        return float(window["low"].min())
    if direction == "bullish":  # resistance, for a SHORT stop
        return float(window["high"].max())
    return None


def build_entry(
    entry_df: pd.DataFrame,
    direction: str,
    fractal_n: int = 2,
    stop_buffer_pct: float = 0.15,
    reward_risk_ratio: float = 2.0,
):
    """Builds the actual entry/stop/target once analyze-side functions
    (daily_pullback_zone + entry_trigger) have already confirmed a trade
    is wanted in `direction`. Entry is the 4h chart's last close; the
    stop sits just beyond the 4h chart's nearest confirmed swing
    low/high (or a lookback-window fallback -- see _fallback_extreme).
    Returns None if a valid (positive) risk distance can't be computed.
    """
    entry = float(entry_df["close"].iloc[-1])

    if direction == "bullish":
        swing = nearest_confirmed_swing_level(entry_df, "bearish", n=fractal_n)
        if swing is None:
            swing = _fallback_extreme(entry_df, "bearish")
        if swing is None:
            return None
        stop = swing * (1 - stop_buffer_pct / 100.0)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + risk * reward_risk_ratio
        return {
            "side": "buy",
            "position_side": "LONG",
            "entry": entry,
            "stop_loss": stop,
            "take_profit": target,
            "swing_reference": swing,
        }

    if direction == "bearish":
        swing = nearest_confirmed_swing_level(entry_df, "bullish", n=fractal_n)
        if swing is None:
            swing = _fallback_extreme(entry_df, "bullish")
        if swing is None:
            return None
        stop = swing * (1 + stop_buffer_pct / 100.0)
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - risk * reward_risk_ratio
        return {
            "side": "sell",
            "position_side": "SHORT",
            "entry": entry,
            "stop_loss": stop,
            "take_profit": target,
            "swing_reference": swing,
        }

    return None
