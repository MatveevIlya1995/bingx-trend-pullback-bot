"""Daily Donchian trend-following rule: entry, trailing exit, sizing, and a
DRY_RUN run of the real bot.run_once() against a fake exchange."""
import csv
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import config  # noqa: E402
import exchange_client  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from strategy import donchian_breakout, trail_levels  # noqa: E402

ENV_KEYS = ["SYMBOLS", "DRY_RUN", "RISK_PCT_PER_TRADE", "MAX_CONCURRENT_POSITIONS", "MARGIN_PER_TRADE_USDT",
            "MAX_MARGIN_UTILIZATION_PCT", "ENTRY_TIMEFRAME", "BREAKOUT_LOOKBACK", "EXIT_LOOKBACK", "ATR_PERIOD",
            "STOP_ATR_MULT", "TRAIL_ATR_MULT", "TREND_FILTER", "MOMENTUM_FILTER", "BREAKOUT_EDGE_ONLY",
            "MAX_TOTAL_NOTIONAL_X", "MAX_DAILY_LOSS_PCT", "STATE_PATH", "TRADES_CSV_PATH", "LOG_PATH",
            "BINGX_API_KEY", "BINGX_API_SECRET", "EXIT_PLAN", "TP1_R", "TP2_R", "TP1_FRACTION",
            "TP2_FRACTION", "FRACTAL_N", "STRUCTURE_TIMEFRAME"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("TRADES_CSV_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "trades.log"))
    monkeypatch.setenv("RISK_PCT_PER_TRADE", "2")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "5")
    monkeypatch.setattr(exchange_client, "CANDLE_MIN_INTERVAL_S", 0.0)
    return tmp_path


def _df(closes, start="2025-01-01"):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=len(closes), freq="1D"),
        "open": opens, "high": np.maximum(opens, closes) * 1.005,
        "low": np.minimum(opens, closes) * 0.995, "close": closes, "volume": 1.0,
    })


def test_defaults_are_the_tested_rule(env):
    c = config.load_config()
    assert (c.entry_timeframe, c.breakout_lookback, c.exit_lookback, c.atr_period) == ("1d", 55, 20, 20)
    assert (c.stop_atr_mult, c.trail_atr_mult, c.trend_filter, c.momentum_filter) == (2.0, 2.0, False, False)
    assert c.breakout_edge_only is False and c.max_total_notional_x == 3.0


def test_breakout_level_and_edge():
    flat = [100.0] * 60
    assert donchian_breakout(_df(flat + [101.0]), 55) == "bullish"
    assert donchian_breakout(_df(flat + [99.0]), 55) == "bearish"
    assert donchian_breakout(_df(flat + [100.2]), 55) is None   # inside the channel (highs are 100.5)
    # Still beyond the channel the next day: level mode re-fires, edge mode doesn't.
    two = _df(flat + [101.0, 102.0])
    assert donchian_breakout(two, 55) == "bullish"
    assert donchian_breakout(two, 55, edge_only=True) is None
    assert donchian_breakout(_df(flat + [101.0]), 55, edge_only=True) == "bullish"


def test_trail_levels_tighter_of_channel_and_atr():
    up = list(np.linspace(100, 200, 80))
    df = _df(up)
    long_level, short_level = trail_levels(df, 20, 20, 2.0)
    channel_low = float(df["low"].iloc[-21:-1].min())
    assert long_level >= channel_low
    assert long_level < float(df["close"].iloc[-1])
    channel_high = float(df["high"].iloc[-21:-1].max())
    assert short_level <= channel_high


def test_position_size_risks_two_percent_and_fills_five_slots(env):
    cfg = config.load_config()
    risk = RiskManager(cfg)
    equity, entry, stop = 157.0, 100.0, 90.0      # a typical ~10% daily 2-ATR stop
    qty, lev = risk.position_size(equity, entry, stop)
    assert qty * (entry - stop) == pytest.approx(equity * 0.02)
    assert lev >= 1 and qty * entry / lev <= equity * cfg.max_margin_utilization_pct / 5 + 1e-9
    used = 0.0
    for _ in range(5):
        q, _ = risk.position_size(equity, entry, stop, existing_notional=used)
        assert q * entry >= cfg.min_notional_usdt
        used += q * entry
    assert used <= equity * cfg.max_total_notional_x


def test_channel_trail_never_moves_back():
    trade = {"position_side": "LONG", "stop_loss": 95.0, "best_price": 100.0, "trail_distance": 5.0,
             "exit_mode": "channel"}
    assert RiskManager._ratchet(trade, 101, 99, (97.0, 120.0)) and trade["stop_loss"] == 97.0
    assert not RiskManager._ratchet(trade, 101, 99, (96.0, 120.0)) and trade["stop_loss"] == 97.0
    short = {"position_side": "SHORT", "stop_loss": 105.0, "best_price": 100.0, "trail_distance": 5.0,
             "exit_mode": "channel"}
    assert RiskManager._ratchet(short, 101, 99, (80.0, 103.0)) and short["stop_loss"] == 103.0
    old = {"position_side": "LONG", "stop_loss": 95.0, "best_price": 100.0, "trail_distance": 5.0}
    RiskManager._ratchet(old, 104, 99, (99.0, 120.0))
    assert old["stop_loss"] == 99.0                 # chandelier: 104 - 5


class _FakeEx:
    """Just enough of ccxt.bingx for a DRY_RUN cycle."""

    def __init__(self, frames):
        self.frames, self.upto = frames, 0

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        df = self.frames[symbol].iloc[: self.upto]
        rows = [[int(t.value // 1_000_000), o, h, l, c, v] for t, o, h, l, c, v in df.itertuples(index=False)]
        return rows[-(limit or 300):]

    def fetch_balance(self, params=None):
        return {"info": {"data": {"balance": {"asset": "USDT", "equity": "157"}}}}

    def fetch_positions(self, symbols=None, params=None):
        return []


def test_dry_run_trades_a_trend_end_to_end(env, monkeypatch):
    # A coin that ranges, trends up hard, then rolls over; candles end yesterday.
    rng = np.random.default_rng(3)
    n = 220
    path = np.r_[100 + rng.normal(0, 1, 90), np.linspace(100, 180, 70), np.linspace(180, 140, 60)]
    start = (pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=n + 1))
    frames = {"SOL/USDT:USDT": _df(path, start=start)}
    ex = _FakeEx(frames)
    monkeypatch.setenv("SYMBOLS", "SOL/USDT:USDT")
    monkeypatch.setenv("BINGX_API_KEY", "k")
    monkeypatch.setenv("BINGX_API_SECRET", "s")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EXIT_PLAN", "channel")   # this test covers exit plan A

    def client(cfg):
        c = exchange_client.ExchangeClient.__new__(exchange_client.ExchangeClient)
        c.cfg, c.exchange = cfg, ex
        return c

    monkeypatch.setattr(bot, "ExchangeClient", client)
    for k in range(80, n + 1):
        ex.upto = k
        bot.run_once()
        bot.run_once()      # a second run on the same candle must change nothing
    rows = list(csv.DictReader(open(env / "trades.csv")))
    entries = [r for r in rows if r["event"] == "ENTRY"]
    exits = [r for r in rows if r["event"] == "EXIT"]
    assert entries and entries[0]["side"] == "BUY"
    assert len(exits) >= len(entries) - 1
    # The uptrend's first long made money, exited by the trailing stop.
    first_exit = next(r for r in exits if r["trade_id"] == entries[0]["trade_id"])
    assert float(first_exit["pnl"]) > 0 and "trailing" in first_exit["trigger"]
    # One position per symbol at any time.
    state = json.load(open(env / "state.json"))
    assert len(state.get("open_dry_run_trades", [])) <= 1
