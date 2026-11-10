"""
Money-management research for the daily 55-day Donchian entries.

    python3 research_mm.py --since 2021-01-01

One event-driven account simulation on 1h candles (all symbols on one
hourly clock), so portfolio rules that depend on what else is open can be
tested: total open risk cap, max positions in one direction, a drawdown
brake, pyramiding, more (non-crypto) symbols, and a small real-size
account with BingX minimum order sizes.

Entries: research.py's donchian(55, 20) on daily candles, both directions,
entered at the first 1h open after the signal day, initial stop 2 x ATR(20).
Exits:
  A  20-day channel / close -/+ 2 ATR trail after every daily close (the bot now)
  B  TP1 3R (1/3), TP2 5R (1/3), stop to entry at the first 1h BOS, then to the
     last swing at every later BOS, runner until the 1h CHoCH (research_exits.py)
Costs: taker fee + slippage on every fill, funding on longs (shorts get none).
Within one 1h candle the stop is assumed hit before a target or an add.
Sizing: equity (marked to market) x risk% / stop distance, one trade's
notional <= 1x equity (bot: margin cap x 10x leverage), all trades <= 3x.
"""
import argparse
import os
import time

import numpy as np
import pandas as pd

import research as R
import research_exits as X

FEE = R.FEE
SLIP = R.SLIP
FUND_H = R.FUNDING_LONG_PER_DAY / 24
CRYPTO = list(R.SYMBOLS)
EXTRA_PREFIXES = ("NCCO", "NCSI", "NCFX")       # BingX non-crypto perps: commodities, indices, forex
NONCRYPTO_MAX = 20


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def discover_noncrypto(ex):
    """BingX non-crypto USDT perps (gold, oil, indices, forex...) by 24h volume."""
    mk = ex.load_markets()
    bases = sorted({m["base"] for m in mk.values()
                    if m.get("swap") and m.get("quote") == "USDT" and m["base"].startswith(EXTRA_PREFIXES)})
    print("non-crypto perps found:", ", ".join(bases), flush=True)
    vol = {}
    try:
        tk = ex.fetch_tickers()
        for s, t in tk.items():
            b = s.split("/")[0]
            if b in bases:
                vol[b] = float(t.get("quoteVolume") or 0)
    except Exception as e:
        print("tickers:", e)
    bases.sort(key=lambda b: -vol.get(b, 0))
    return bases[:NONCRYPTO_MAX], vol


def load(ex, syms, since_ms, cache):
    out = {}
    for sym in syms:
        try:
            d = R.fetch(ex, sym, "1d", since_ms - 250 * R.TF_MS["1d"], cache)
            h = R.fetch(ex, sym, "1h", since_ms, cache)
        except Exception as e:
            print(f"{sym}: {e}", flush=True)
            continue
        print(f"{sym}: 1d {len(d)} from {d.index[0].date() if len(d) else '-'}, "
              f"1h {len(h)} from {h.index[0].date() if len(h) else '-'}", flush=True)
        if len(d) >= 120 and len(h) >= 24 * 60:
            out[sym] = (d, h)
    return out


def prepare(sym, d, h, grid):
    sig = R.donchian(d, 55, 20)
    a = R.atr(d, 20)
    S = X.structure(h, "1h")
    day = h.index.normalize()
    last = np.r_[day[1:] != day[:-1], True]
    tl = sig["trail_long"].reindex(day).values
    tsh = sig["trail_short"].reindex(day).values
    g = grid.searchsorted(h.index)
    ok = (g < len(grid)) & (grid[np.minimum(g, len(grid) - 1)] == h.index)
    loc = np.full(len(grid), -1)
    loc[g[ok]] = np.arange(len(h))[ok]
    entries = {}
    for dd, row in sig.iterrows():
        for side, col in ((1, "long_entry"), (-1, "short_entry")):
            stop0 = row[col]
            if np.isnan(stop0):
                continue
            k = h.index.searchsorted(dd + pd.Timedelta(days=1))
            if k >= len(h) or h.index[k] - dd > pd.Timedelta(days=4):
                break
            gi = grid.searchsorted(h.index[k])
            if gi < len(grid) and grid[gi] == h.index[k]:
                entries.setdefault(gi, []).append((sym, side, stop0, a.at[dd]))
            break
    return {"o": h["open"].values, "h": h["high"].values, "l": h["low"].values, "c": h["close"].values,
            "last": last, "tl": tl, "ts": tsh, "S": S, "loc": loc, "entries": entries}


# --------------------------------------------------------------------------
# account simulation
# --------------------------------------------------------------------------
def simulate(P, grid, cfg, ex=None, equity0=1000.0):
    plan = cfg["plan"]
    risk_pct = cfg["risk"]
    max_pos = cfg.get("max_pos", 5)
    side_cap = cfg.get("side_cap")           # max positions in one direction
    risk_cap = cfg.get("risk_cap")           # total open risk / equity
    brake = cfg.get("brake")                 # (drawdown, factor)
    pyr = cfg.get("pyramid")                 # list of ATR multiples to add at
    syms = cfg["symbols"]
    small = cfg.get("min_orders", False)

    eq = equity0
    peak_d = equity0       # daily peak for the brake
    braked = False
    peak, maxdd = equity0, 0.0
    pos = {}
    trades = []
    curve = []
    last_close = {}
    skipped = {"slots": 0, "side": 0, "risk_cap": 0, "min_size": 0}

    def unreal():
        return sum((last_close[s] - p["entry"]) * p["side"] * p["qty"] for s, p in pos.items())

    def open_risk():
        return sum(max((p["entry"] - p["stop"]) * p["side"], 0) * p["qty"] for p in pos.values())

    def close_part(p, q, px, why):
        nonlocal eq
        px = px * (1 - SLIP * p["side"])
        pnl = (px - p["entry"]) * p["side"] * q - q * px * FEE
        eq += pnl
        p["pnl"] += pnl
        p["qty"] -= q

    for g in range(len(grid)):
        t = grid[g]
        # ---- 1) new entries at this hour's open
        cands = []
        for s in syms:
            e = P[s]["entries"].get(g)
            if e:
                cands += e
        for sym, side, stop0, N in cands:
            if sym in pos:
                continue
            if len(pos) >= max_pos:
                skipped["slots"] += 1
                continue
            if side_cap and sum(1 for p in pos.values() if p["side"] == side) >= side_cap:
                skipped["side"] += 1
                continue
            d = P[sym]
            i = d["loc"][g]
            px = d["o"][i] * (1 + SLIP * side)
            dist = (px - stop0) * side
            if dist <= 0:
                continue
            equity = eq + unreal()
            rp = risk_pct * (brake[1] if braked else 1.0)
            qty = equity * rp / dist
            qty = min(qty, equity * 1.0 / px)
            notional = sum(p["qty"] * last_close[s] for s, p in pos.items())
            qty = min(qty, max(equity * 3.0 - notional, 0) / px)
            if risk_cap:
                room = equity * risk_cap - open_risk()
                if room < qty * dist:
                    if room < 0.5 * qty * dist:
                        skipped["risk_cap"] += 1
                        continue
                    qty = room / dist
            if small and ex is not None:
                m = ex.market(f"{sym}/USDT:USDT")
                try:
                    qty = float(ex.amount_to_precision(m["symbol"], qty))
                except Exception:
                    qty = 0.0
                mn = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0
                mc = (m.get("limits", {}).get("cost", {}) or {}).get("min") or 2.0
                if qty <= 0 or qty < mn or qty * px < mc:
                    skipped["min_size"] += 1
                    continue
            if qty * px < 1:
                continue
            eq -= qty * px * FEE
            p = {"sym": sym, "side": side, "entry": px, "qty": qty, "qty0": qty, "stop": stop0,
                 "risk": dist, "risk_usd": qty * dist, "N": N, "t_in": t, "k0": d["S"]["k"][i],
                 "pnl": -qty * px * FEE, "ref": None, "ref_broken": True, "bos": 0,
                 "adds": list(pyr) if (pyr and plan == "A") else [], "unit": qty, "entry_first": px}
            if plan == "B":
                p["tp"] = [(px + 3 * dist * side, qty / 3), (px + 5 * dist * side, qty / 3)]
            else:
                p["tp"] = []
            pos[sym] = p

        # ---- 2) fills and stop updates on this hour's candle
        for sym in list(pos):
            p = pos[sym]
            d = P[sym]
            i = d["loc"][g]
            if i < 0:
                continue
            o, hi, lo, c = d["o"][i], d["h"][i], d["l"][i], d["c"][i]
            side = p["side"]
            stop_hit = (lo <= p["stop"]) if side > 0 else (hi >= p["stop"])
            tp = p["tp"]
            gap_tp = tp and ((o >= tp[0][0]) if side > 0 else (o <= tp[0][0]))
            done = False
            if stop_hit and not gap_tp:
                close_part(p, p["qty"], min(o, p["stop"]) if side > 0 else max(o, p["stop"]), "stop")
                done = True
            else:
                while tp:
                    lvl, q = tp[0]
                    if (hi >= lvl) if side > 0 else (lo <= lvl):
                        close_part(p, q, max(o, lvl) if side > 0 else min(o, lvl), "tp")
                        tp.pop(0)
                    else:
                        break
                if p["qty"] <= 1e-12 * p["qty0"]:
                    done = True
                elif stop_hit:
                    close_part(p, p["qty"], p["stop"], "stop")
                    done = True
            if not done and p["adds"]:
                k = p["adds"][0]
                add_lvl = p["entry_first"] + k * p["N"] * side
                if (hi >= add_lvl) if side > 0 else (lo <= add_lvl):
                    apx = (max(o, add_lvl) if side > 0 else min(o, add_lvl)) * (1 + SLIP * side)
                    new_stop = apx - 2 * p["N"] * side
                    new_stop = max(p["stop"], new_stop) if side > 0 else min(p["stop"], new_stop)
                    q = p["unit"]
                    equity = eq + unreal()
                    ok = True
                    if risk_cap:
                        cur = open_risk() - max((p["entry"] - p["stop"]) * side, 0) * p["qty"]
                        avg = (p["entry"] * p["qty"] + apx * q) / (p["qty"] + q)
                        after = cur + max((avg - new_stop) * side, 0) * (p["qty"] + q)
                        ok = after <= equity * risk_cap
                    notional = sum(pp["qty"] * last_close[s] for s, pp in pos.items())
                    ok = ok and (notional + q * apx <= equity * 3.0) and ((p["qty"] + q) * apx <= equity * 1.0)
                    p["adds"].pop(0)
                    if ok:
                        eq -= q * apx * FEE
                        p["pnl"] -= q * apx * FEE
                        p["entry"] = (p["entry"] * p["qty"] + apx * q) / (p["qty"] + q)
                        p["qty"] += q
                        p["stop"] = new_stop
            if done:
                trades.append({"sym": sym, "side": side, "t_in": p["t_in"], "t_out": t,
                               "pnl": p["pnl"], "r": p["pnl"] / p["risk_usd"]})
                del pos[sym]
                last_close[sym] = c
                continue
            if side > 0:
                f = p["qty"] * c * FUND_H
                eq -= f
                p["pnl"] -= f
            # stop moves after the close
            if plan == "A":
                if d["last"][i]:
                    lvl = d["tl"][i] if side > 0 else d["ts"][i]
                    if not np.isnan(lvl):
                        p["stop"] = max(p["stop"], lvl) if side > 0 else min(p["stop"], lvl)
            else:
                S = d["S"]
                if S["last"][i]:
                    k = S["k"][i]
                    j = k - X.FRACTAL_N
                    if j > p["k0"]:
                        if side > 0 and S["sh"][j]:
                            p["ref"], p["ref_broken"] = (j, S["h"][j]), False
                        if side < 0 and S["sl"][j]:
                            p["ref"], p["ref_broken"] = (j, S["l"][j]), False
                    ref = p["ref"]
                    if ref is not None and not p["ref_broken"] and ((S["c"][k] > ref[1]) if side > 0 else (S["c"][k] < ref[1])):
                        p["ref_broken"] = True
                        p["bos"] += 1
                        if p["bos"] == 1:
                            new = p["entry"]
                        else:
                            seg = S["l"][ref[0]:k + 1] if side > 0 else S["h"][ref[0]:k + 1]
                            org = seg.min() if side > 0 else seg.max()
                            new = max(org, p["entry"]) if side > 0 else min(org, p["entry"])
                        p["stop"] = max(p["stop"], new) if side > 0 else min(p["stop"], new)
            last_close[sym] = c
        for s in syms:
            i = P[s]["loc"][g]
            if i >= 0:
                last_close[s] = P[s]["c"][i]

        # ---- 3) equity curve, drawdown, brake
        m = eq + unreal()
        peak = max(peak, m)
        maxdd = max(maxdd, 1 - m / peak)
        if t.hour == 23:
            curve.append((t.normalize(), m))
            if brake:
                if m >= peak_d:
                    peak_d = m
                    braked = False
                elif m < peak_d * (1 - brake[0]):
                    braked = True

    for sym, p in pos.items():
        c = last_close[sym]
        close_part(p, p["qty"], c, "end")
        trades.append({"sym": sym, "side": p["side"], "t_in": p["t_in"], "t_out": grid[-1],
                       "pnl": p["pnl"], "r": p["pnl"] / p["risk_usd"]})
    curve = pd.Series(dict(curve))
    return eq, maxdd, trades, curve, skipped


def cagr(a, b, days):
    if a <= 0 or b <= 0 or days <= 0:
        return float("nan")
    return (b / a) ** (365 / days) - 1


def row(name, eq0, eq, dd, tr, curve, start, end, split, skipped):
    n = len(tr)
    rs = np.array([t["r"] for t in tr]) if n else np.zeros(1)
    pn = np.array([t["pnl"] for t in tr]) if n else np.zeros(1)
    gp, gl = pn[pn > 0].sum(), -pn[pn <= 0].sum()
    days = (end - start).days
    cg = cagr(eq0, eq, days)
    c1 = curve[curve.index <= split]
    h1 = cagr(eq0, c1.iloc[-1], (split - start).days) if len(c1) else float("nan")
    h2 = cagr(c1.iloc[-1], curve.iloc[-1], (end - split).days) if len(c1) else float("nan")
    # worst calendar year by return
    yr = curve.groupby(curve.index.year).last()
    prev = pd.concat([pd.Series([eq0]), yr.iloc[:-1]]).values
    worst_year = np.min(yr.values / prev - 1) if len(yr) else float("nan")
    sk = ", ".join(f"{k} {v}" for k, v in skipped.items() if v) or "-"
    return (f"| {name} | {n} | {100*np.mean(rs>0):.0f}% | {gp/gl if gl else float('nan'):.2f} | {rs.mean():+.2f} | "
            f"{100*(eq/eq0-1):+.1f}% | **{100*cg:+.1f}%** | {100*dd:.1f}% | {cg/dd if dd else float('nan'):.2f} | "
            f"{100*h1:+.1f}% / {100*h2:+.1f}% | {100*worst_year:+.1f}% | {sk} |")


HEAD = ["| scenario | trades | win | PF | avg R | return | per year | max DD | per-year / DD | per year 1st / 2nd half | worst year | entries skipped |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2021-01-01")
    ap.add_argument("--out", default="research_results")
    ap.add_argument("--cache", default="research_cache")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)
    R.TF_MS["1h"] = 3_600_000
    ex = R.ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    since_ms = int(pd.Timestamp(args.since).value // 1_000_000)
    extra, vol = discover_noncrypto(ex)
    data = load(ex, CRYPTO + extra, since_ms, args.cache)
    crypto = [s for s in CRYPTO if s in data]
    extra = [s for s in extra if s in data]

    start = max(min(data[s][1].index[0] for s in crypto).normalize() + pd.Timedelta(days=1), pd.Timestamp(args.since))
    end = min(data[s][1].index[-1] for s in crypto)
    grid = pd.date_range(start, end, freq="h")
    split = start + (end - start) / 2
    P = {s: prepare(s, d, h, grid) for s, (d, h) in data.items()}

    base = {"symbols": crypto}
    caps = {"risk_cap": 0.06, "side_cap": 3}
    brk = {"brake": (0.10, 0.5)}
    pyr = {"pyramid": [1.0, 2.0]}
    S = [
        ("1. A 1% (yesterday's baseline)", {"plan": "A", "risk": 0.01}),
        ("2. A 1.5%", {"plan": "A", "risk": 0.015}),
        ("3. A 2% (bot now)", {"plan": "A", "risk": 0.02}),
        ("4. B 1%", {"plan": "B", "risk": 0.01}),
        ("5. B 1.5%", {"plan": "B", "risk": 0.015}),
        ("6. B 2%", {"plan": "B", "risk": 0.02}),
        ("7. A 1.5% + caps", {"plan": "A", "risk": 0.015, **caps}),
        ("8. B 1.5% + caps", {"plan": "B", "risk": 0.015, **caps}),
        ("9. A 1.5% + caps + brake", {"plan": "A", "risk": 0.015, **caps, **brk}),
        ("10. B 1.5% + caps + brake", {"plan": "B", "risk": 0.015, **caps, **brk}),
        ("11. A 2% + caps + brake", {"plan": "A", "risk": 0.02, **caps, **brk}),
        ("12. A 1.5% + pyramiding", {"plan": "A", "risk": 0.015, **pyr}),
        ("13. A 1.5% + pyramiding + caps + brake", {"plan": "A", "risk": 0.015, **pyr, **caps, **brk}),
        ("14. A 1% + pyramiding + caps + brake", {"plan": "A", "risk": 0.01, **pyr, **caps, **brk}),
    ]
    lines = [f"# Money-management backtest, {start.date()} to {end.date()}", "",
             f"Crypto: {', '.join(crypto)}. Daily 55-day Donchian entries, price walked on 1h candles, "
             f"one account from $1000, max 5 positions, fee {FEE*100:g}% + slippage {SLIP*100:g}% per side, "
             f"funding {R.FUNDING_LONG_PER_DAY*100:g}%/day on longs. Halves split at {split.date()}. "
             "Max DD is marked to market every hour. caps = total open risk <= 6% of equity and <= 3 positions "
             "in one direction. brake = risk halved while equity is >10% below its peak, until a new peak. "
             "pyramiding = add one more unit (same size as the first) at +1 ATR and +2 ATR, whole stop raised "
             "to 2 ATR behind the newest unit.", "", "## Risk, exits, caps, brake, pyramiding", ""] + HEAD
    for name, cfg in S:
        t0 = time.time()
        eq, dd, tr, cv, sk = simulate(P, grid, {**base, **cfg})
        lines.append(row(name, 1000, eq, dd, tr, cv, start, end, split, sk))
        print(lines[-1], f"({time.time()-t0:.0f}s)", flush=True)

    # ---- non-crypto symbols
    lines += ["", "## Adding non-crypto perps (metals, commodities, indices, forex)", ""]
    if extra:
        x0 = max(min(data[s][0].index[0] for s in extra) + pd.Timedelta(days=60), start)
        lines.append("Symbols (1h history start): " + ", ".join(
            f"{s} ({data[s][1].index[0].date()})" for s in extra) + ".")
        lines.append(f"Each symbol trades once it has 55 daily candles. Same-window comparison from {x0.date()}:")
        lines += [""] + HEAD
        g0 = grid.searchsorted(x0)
        sub = grid[g0:]
        P2 = {s: prepare(s, d, h, sub) for s, (d, h) in data.items()}
        for name, cfg in [("A 1.5% + caps + brake, crypto only", {"plan": "A", "risk": 0.015, **caps, **brk}),
                          ("A 1.5% + caps + brake, + non-crypto", {"plan": "A", "risk": 0.015, **caps, **brk, "extra": 1}),
                          ("A 1.5% + caps + brake, + non-crypto, 8 slots", {"plan": "A", "risk": 0.015, **caps, **brk, "extra": 1, "max_pos": 8}),
                          ("B 1.5% + caps + brake, crypto only", {"plan": "B", "risk": 0.015, **caps, **brk}),
                          ("B 1.5% + caps + brake, + non-crypto", {"plan": "B", "risk": 0.015, **caps, **brk, "extra": 1}),
                          ("A 1.5% + pyr + caps + brake, + non-crypto, 8 slots", {"plan": "A", "risk": 0.015, **pyr, **caps, **brk, "extra": 1, "max_pos": 8})]:
            syms = crypto + (extra if cfg.pop("extra", 0) else [])
            eq, dd, tr, cv, sk = simulate(P2, sub, {**cfg, "symbols": syms})
            mid = x0 + (end - x0) / 2
            lines.append(row(name, 1000, eq, dd, tr, cv, x0, end, mid, sk))
            print(lines[-1], flush=True)
            if name == "A 1.5% + caps + brake, + non-crypto":
                by = {}
                for t in tr:
                    by.setdefault("non-crypto" if t["sym"] in extra else "crypto", []).append(t["pnl"])
                lines.append("|  ↳ P/L split | " + "; ".join(f"{k}: {len(v)} trades, ${sum(v):+.0f}" for k, v in by.items())
                             + " | | | | | | | | | | |")
    else:
        lines.append("No non-crypto perps with enough history were found.")

    # ---- small real account
    lines += ["", "## Real account size ($157) with BingX minimum order sizes", ""] + HEAD
    for name, cfg in [("A 2% (bot now), $157", {"plan": "A", "risk": 0.02}),
                      ("A 1.5% + caps + brake, $157", {"plan": "A", "risk": 0.015, **caps, **brk}),
                      ("B 1.5% + caps + brake, $157", {"plan": "B", "risk": 0.015, **caps, **brk}),
                      ("A 1.5% + pyr + caps + brake, $157", {"plan": "A", "risk": 0.015, **pyr, **caps, **brk})]:
        eq, dd, tr, cv, sk = simulate(P, grid, {**base, **cfg, "min_orders": True}, ex=ex, equity0=157.0)
        lines.append(row(name, 157, eq, dd, tr, cv, start, end, split, sk))
        print(lines[-1], flush=True)

    lines += ["", "per-year / DD = yearly return divided by max drawdown (higher = more return for the pain). "
              "Results are on today's surviving coins, so they flatter a little."]
    open(os.path.join(args.out, "mm_summary.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
