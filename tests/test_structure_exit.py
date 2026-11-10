"""Exit plan B ("structure"): TP1 3R / TP2 5R partials, stop to entry at the
first 1h BOS, trailed to the last swing at every BOS, runner to the CHoCH."""
import copy
import csv
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import config  # noqa: E402
import exchange_client  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from strategy import structure_step, structure_targets  # noqa: E402
from test_daily_donchian import _FakeEx, _df, env  # noqa: E402,F401


def _h1(points, start="2026-01-01 01:00", per_leg=4):
    """1h candles walking linearly between the given prices."""
    closes = [points[0]]
    for a, b in zip(points[:-1], points[1:]):
        closes += list(np.linspace(a, b, per_leg + 1)[1:])
    closes = np.asarray(closes, float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"ts": pd.date_range(start, periods=len(closes), freq="1h"), "open": opens,
                         "high": np.maximum(opens, closes) + 0.05, "low": np.minimum(opens, closes) - 0.05,
                         "close": closes, "volume": 1.0})


def _trade(side="LONG", entry=100.0, stop=90.0, qty=3.0):
    return {"trade_id": "t1", "symbol": "SOL/USDT:USDT", "side": "BUY" if side == "LONG" else "SELL",
            "position_side": side, "qty": qty, "initial_qty": qty, "entry_price": entry, "stop_loss": stop,
            "initial_stop": stop, "entry_time_utc": "2026-01-01T00:05:00+00:00", "exit_mode": "structure",
            "tps": structure_targets(entry, stop, qty), "structure_from_ts": "2026-01-01T01:00:00",
            "bos_count": 0, "ref_swing": None, "last_structure_ts": None, "trigger": "test"}


def test_targets_are_3r_and_5r_thirds():
    tps = structure_targets(100, 90, 3.0)
    assert [round(t["price"], 9) for t in tps] == [130, 150]
    assert [round(t["qty"], 9) for t in tps] == [1, 1]
    short = structure_targets(100, 110, 3.0)
    assert [round(t["price"], 9) for t in short] == [70, 50]


def test_long_takes_both_targets_then_runner_exits_at_choch():
    # zigzag up through 130 and 150, then a drop that breaks the last swing low
    h1 = _h1([100, 108, 104, 115, 110, 124, 118, 135, 128, 146, 140, 158, 150, 162, 120])
    t = _trade()
    events, done = structure_step(t, h1)
    labels = [e[0] for e in events]
    assert labels[:2] == ["TP1 3R", "TP2 5R"] and done
    assert abs(sum(e[1] for e in events) - 3.0) < 1e-9
    assert abs(events[0][2] - 130) < 1e-9 and abs(events[1][2] - 150) < 1e-9
    final = events[-1]
    assert "CHoCH" in final[0] and final[2] > 140        # trailed well above the entry
    assert t["bos_count"] >= 2


def test_first_bos_moves_stop_to_entry_and_breakeven_exit():
    h1 = _h1([100, 106, 103, 109, 95])
    t = _trade()
    events, done = structure_step(t, h1)
    assert done and len(events) == 1
    assert events[0][0] == "breakeven stop hit (after 1h BOS)" and abs(events[0][2] - 100) < 1e-9


def test_initial_stop_and_rerun_is_idempotent():
    h1 = _h1([100, 97, 99, 92, 99])
    t = _trade()
    s0 = copy.deepcopy(t)
    ev, done = structure_step(t, h1.iloc[:6])
    assert not ev and not done
    again, _ = structure_step(t, h1.iloc[:6])     # same candles: nothing new
    assert not again
    ev, done = structure_step(t, _h1([100, 97, 99, 92, 85]))
    assert done and ev[0][0] == "initial stop hit" and abs(ev[0][2] - 90) < 1e-9
    assert s0["stop_loss"] == 90


def test_short_mirror():
    h1 = _h1([100, 92, 96, 85, 90, 75, 80, 66, 72, 48, 55, 40, 75])
    t = _trade("SHORT", 100.0, 110.0)
    events, done = structure_step(t, h1)
    assert [e[0] for e in events][:2] == ["TP1 3R", "TP2 5R"] and done
    assert events[-1][2] < 60


def test_paper_journal_and_plan_a_trades_untouched(env):
    cfg = config.load_config()
    risk = RiskManager(cfg)
    risk.state["open_dry_run_trades"] = [
        _trade(),
        {**_trade(), "trade_id": "old", "exit_mode": "channel", "tps": None, "stop_loss": 90.0},
    ]
    risk._save_state()
    h1 = _h1([100, 108, 104, 115, 110, 124, 118, 135, 128, 146, 140, 158, 150, 162, 120])
    closed = risk.manage_structure_trades("SOL/USDT:USDT", h1)
    assert [c["trade_id"] for c in closed] == ["t1"]
    left = risk.state["open_dry_run_trades"]
    assert [t["trade_id"] for t in left] == ["old"] and left[0]["stop_loss"] == 90.0
    rows = list(csv.DictReader(open(env / "trades.csv")))
    assert [r["trigger"] for r in rows][:2] == ["TP1 3R", "TP2 5R"] and len(rows) == 3
    assert all(float(r["pnl"]) > 0 for r in rows)
    # the daily channel code leaves structure trades alone
    risk.state["open_dry_run_trades"] = [_trade()]
    assert risk.reconcile_dry_run_exits("SOL/USDT:USDT", high=200, low=1, candle_ts="2026-01-05T00:00:00") == []


def test_bot_opens_plan_b_trades_by_default(env, monkeypatch):
    rng = np.random.default_rng(3)
    n = 160
    path = np.r_[100 + rng.normal(0, 1, 90), np.linspace(100, 180, 70)]
    start = (pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=n + 1))
    ex = _FakeEx({"SOL/USDT:USDT": _df(path, start=start)})
    for k, v in (("SYMBOLS", "SOL/USDT:USDT"), ("BINGX_API_KEY", "k"), ("BINGX_API_SECRET", "s"), ("DRY_RUN", "true")):
        monkeypatch.setenv(k, v)

    def client(cfg):
        c = exchange_client.ExchangeClient.__new__(exchange_client.ExchangeClient)
        c.cfg, c.exchange = cfg, ex
        return c

    monkeypatch.setattr(bot, "ExchangeClient", client)
    ex.upto = n
    bot.run_once()
    bot.run_once()
    st = json.load(open(env / "state.json"))
    tr = st["open_dry_run_trades"]
    assert len(tr) == 1 and tr[0]["exit_mode"] == "structure"
    r = tr[0]["entry_price"] - tr[0]["initial_stop"]
    assert abs(tr[0]["tps"][0]["price"] - (tr[0]["entry_price"] + 3 * r)) < 1e-4
    assert abs(tr[0]["tps"][1]["qty"] - tr[0]["qty"] / 3) < 1e-6
    rows = list(csv.DictReader(open(env / "trades.csv")))
    assert rows[0]["event"] == "ENTRY" and rows[0]["take_profit"] and "exit B" in rows[0]["trigger"]
