"""
Exit-plan comparison for the daily 55-day Donchian entries.

    python3 research_exits.py --since 2021-01-01

Same entries for both plans (research.py's donchian 55/20 on daily candles,
both directions, entry at the next daily open, initial stop 2 x ATR(20)),
price walked on 1h candles, one account (1% risk per trade, max 5 positions,
one per symbol), taker fee + slippage on every fill, funding on longs.

A  current : whole position, stop trails after each daily close to the
             tighter of the 20-day channel and close -/+ 2 ATR.
B  requested (2026-11-09): R = initial stop distance.
             TP1 at 3R closes 1/3, TP2 at 5R closes 1/3, the last 1/3 runs.
             Structure on 1h (fractal swings, 2 candles each side, confirmed
             2 candles later; only swings formed after the entry count).
             First 1h BOS in the trade's direction (a close beyond the last
             swing high for a long): stop to the entry. Every later BOS:
             stop to the swing low the breaking move started from (the
             lowest low since the broken swing high), never below the
             entry. The runner ends at the first CHoCH against the trade,
             which is that trailed stop being hit.
Within one 1h candle the stop is assumed hit before a target, unless the
candle opens beyond the target.
"""
import argparse
import os

import numpy as np
import pandas as pd

import research as R

FEE = R.FEE + R.SLIP           # per side, fraction of notional
FUND = R.FUNDING_LONG_PER_DAY
FRACTAL_N = 2


def swings(df1h):
    h, l = df1h["high"].values, df1h["low"].values
    n = FRACTAL_N
    sh = np.zeros(len(h), bool)
    sl = np.zeros(len(h), bool)
    if len(h) > 2 * n:
        w = np.lib.stride_tricks.sliding_window_view
        sh[n:len(h) - n] = w(h, 2 * n + 1).argmax(axis=1) == n
        sl[n:len(h) - n] = w(l, 2 * n + 1).argmin(axis=1) == n
    return sh, sl


def structure(h1, tf):
    """Candles the BOS/CHoCH are read on, built from the 1h candles, plus
    for every 1h candle: the index of its structure candle and whether it
    is that candle's last hour (when the structure candle closes)."""
    if tf == "1h":
        st = h1
    else:
        st = h1.resample(tf).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    sh, sl = swings(st)
    kidx = st.index.searchsorted(h1.index.floor(tf), side="left")
    nxt = np.r_[kidx[1:], -1]
    last = kidx != nxt
    return {"h": st["high"].values, "l": st["low"].values, "c": st["close"].values,
            "sh": sh, "sl": sl, "k": kidx, "last": last}


def run_trade(plan, side, entry_i, stop0, h1, S, daily_trail):
    """Walk one trade on 1h candles from bar entry_i. Returns
    (exit_i, parts) with parts = [(fraction, exit_price)], or None."""
    o, hi, lo, c = (h1[k].values for k in ("open", "high", "low", "close"))
    ts = h1.index
    entry = o[entry_i] * (1 + R.SLIP * side)
    risk = (entry - stop0) * side
    if risk <= 0:
        return None
    stop = stop0
    parts, remaining = [], 1.0
    tp = [(entry + 3 * risk * side, 1 / 3), (entry + 5 * risk * side, 1 / 3)] if plan != "A" else []
    ref = None          # (index, price) of the latest confirmed swing in the trade's direction
    ref_broken = True
    bos_count = 0
    for i in range(entry_i, len(o)):
        # --- fills on this candle: stop first (conservative), then targets
        stop_hit = (lo[i] <= stop) if side > 0 else (hi[i] >= stop)
        gap_tp = tp and ((o[i] >= tp[0][0]) if side > 0 else (o[i] <= tp[0][0]))
        if stop_hit and not gap_tp:
            px = (min(o[i], stop) if side > 0 else max(o[i], stop))
            parts.append((remaining, px))
            return i, parts, entry, risk
        while tp:
            lvl, frac = tp[0]
            if (hi[i] >= lvl) if side > 0 else (lo[i] <= lvl):
                px = max(o[i], lvl) if side > 0 else min(o[i], lvl)
                parts.append((frac, px))
                remaining -= frac
                tp.pop(0)
            else:
                break
        if stop_hit:   # gapped over a target, then the stop on the same candle
            parts.append((remaining, stop))
            return i, parts, entry, risk

        # --- after the close: move the stop for the next candle
        if plan == "A":
            if ts[i].hour == 23 and ts[i].normalize() in daily_trail.index:
                lvl = daily_trail.at[ts[i].normalize()]
                if not np.isnan(lvl):
                    stop = max(stop, lvl) if side > 0 else min(stop, lvl)
        elif S["last"][i]:
            k = S["k"][i]                # structure candle k just closed
            k0 = S["k"][entry_i]
            sh_, sl_, sc, shh, sll = S["sh"], S["sl"], S["c"], S["h"], S["l"]
            j = k - FRACTAL_N            # a swing at j is confirmed once k closes
            if j > k0:
                if side > 0 and sh_[j]:
                    ref, ref_broken = (j, shh[j]), False
                if side < 0 and sl_[j]:
                    ref, ref_broken = (j, sll[j]), False
            if ref is not None and not ref_broken and ((sc[k] > ref[1]) if side > 0 else (sc[k] < ref[1])):
                ref_broken = True
                bos_count += 1
                if bos_count == 1:
                    new = entry
                else:
                    seg = sll[ref[0]:k + 1] if side > 0 else shh[ref[0]:k + 1]
                    origin = seg.min() if side > 0 else seg.max()
                    new = max(origin, entry) if side > 0 else min(origin, entry)
                stop = max(stop, new) if side > 0 else min(stop, new)
    parts.append((remaining, c[-1]))   # still open at the end: mark at the last close
    return len(o) - 1, parts, entry, risk


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
    d1, h1 = {}, {}
    notes = []
    for sym in R.SYMBOLS:
        try:
            a = R.fetch(ex, sym, "1d", since_ms - 250 * R.TF_MS["1d"], args.cache)
            b = R.fetch(ex, sym, "1h", since_ms, args.cache)
        except Exception as e:
            print(f"{sym}: {e}")
            continue
        if len(a) > 300 and len(b) > 24 * 120:
            d1[sym], h1[sym] = a, b
            notes.append(f"{sym}: 1h from {b.index[0].date()}")
        print(f"{sym}: 1d {len(a)} from {a.index[0].date() if len(a) else '-'}, 1h {len(b)} from {b.index[0].date() if len(b) else '-'}", flush=True)

    start = max(min(b.index[0] for b in h1.values()).normalize() + pd.Timedelta(days=1), pd.Timestamp(args.since))
    end = min(b.index[-1] for b in h1.values())
    split = max(pd.Timestamp("2025-04-03"), start + (end - start) / 2) if start < pd.Timestamp("2025-04-03") else start + (end - start) / 2

    # candidate trades per plan
    PLANS = ("A", "B 1h", "B 4h")
    cands = {p: [] for p in PLANS}
    for sym in d1:
        df = d1[sym]
        sig = R.donchian(df, 55, 20)
        hh = h1[sym]
        structs = {"B 1h": structure(hh, "1h"), "B 4h": structure(hh, "4h")}
        idx = hh.index
        for day, row in sig.iterrows():
            nxt = day + pd.Timedelta(days=1)
            if nxt < start or nxt > end:
                continue
            for side, col, trail_col in ((1, "long_entry", "trail_long"), (-1, "short_entry", "trail_short")):
                stop0 = row[col]
                if np.isnan(stop0):
                    continue
                k = idx.searchsorted(nxt)
                if k >= len(idx) or idx[k] != nxt:
                    continue
                for plan in PLANS:
                    r = run_trade(plan, side, k, stop0, hh, structs.get(plan), sig[trail_col])
                    if r is None:
                        continue
                    exit_i, parts, entry, risk = r
                    cands[plan].append({"sym": sym, "side": side, "t_in": nxt, "t_out": idx[exit_i],
                                        "entry": entry, "risk": risk, "parts": parts})
                break

    lines = [f"# Exit plans on daily 55-day Donchian entries, {start.date()} to {end.date()}", "",
             "Prices walked on 1h candles. " + "; ".join(notes) + ".",
             f"Split for the two halves: {split.date()}. 1% risk per trade, max 5 positions, one per symbol, "
             f"fee+slippage {FEE*100:g}% per side, funding {FUND*100:g}%/day on longs. "
             "A = current exit (20-day channel / 2 ATR, whole position). "
             "B = TP1 3R (1/3), TP2 5R (1/3), stop to entry at the first BOS, then to the last swing at every BOS, runner until the CHoCH; structure read on 1h or 4h candles (built from the 1h data).", "",
             "| plan | period | trades | win rate | PF | avg R | reached 3R | reached 5R | avg days held | return | CAGR | max DD |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for plan in PLANS:
        for label, a, b in (("1st part", start, split), ("2nd part", split, end), ("whole", start, end)):
            cs = sorted([c for c in cands[plan] if a <= c["t_in"] < b], key=lambda c: c["t_in"])
            eq, peak, dd = 1000.0, 1000.0, 0.0
            open_ = []
            res = []
            events = []
            for c in cs:
                open_ = [o for o in open_ if o["t_out"] > c["t_in"]]
                if len(open_) >= R.MAX_POS or any(o["sym"] == c["sym"] for o in open_):
                    continue
                # realize exits that happened before this entry
                for e in sorted([e for e in events if e[0] <= c["t_in"]]):
                    eq += e[1]
                    peak = max(peak, eq)
                    dd = max(dd, 1 - eq / peak)
                events = [e for e in events if e[0] > c["t_in"]]
                risk_usd = eq * R.RISK
                qty = risk_usd / c["risk"]
                pnl = 0.0
                for frac, px in c["parts"]:
                    pnl += (px - c["entry"]) * c["side"] * qty * frac - qty * frac * (c["entry"] + px) * FEE
                days = (c["t_out"] - c["t_in"]) / pd.Timedelta(days=1)
                if c["side"] > 0:
                    pnl -= qty * c["entry"] * FUND * days
                rmult = pnl / risk_usd
                best = max(((px - c["entry"]) * c["side"] / c["risk"]) for _, px in c["parts"])
                res.append((rmult, days, best))
                events.append((c["t_out"], pnl))
                open_.append(c)
            for e in sorted(events):
                eq += e[1]
                peak = max(peak, eq)
                dd = max(dd, 1 - eq / peak)
            n = len(res)
            if not n:
                continue
            rs = np.array([r[0] for r in res])
            gp, gl = rs[rs > 0].sum(), -rs[rs <= 0].sum()
            yrs = (b - a).days / 365
            cagr = (eq / 1000) ** (1 / yrs) - 1 if eq > 0 and yrs > 0 else -1
            hit3 = np.mean([r[2] >= 3 for r in res]) * 100
            hit5 = np.mean([r[2] >= 5 for r in res]) * 100
            lines.append(f"| {plan} | {label} | {n} | {100*np.mean(rs>0):.1f}% | {gp/gl if gl else float('nan'):.2f} | "
                         f"{rs.mean():+.3f} | {hit3:.0f}% | {hit5:.0f}% | {np.mean([r[1] for r in res]):.1f} | "
                         f"{100*(eq/1000-1):+.1f}% | {100*cagr:+.1f}% | {100*dd:.1f}% |")
            print(lines[-1], flush=True)
    lines += ["", "\"reached 3R/5R\" for A counts trades whose exit price was at least that far in profit; for B, "
              "trades that took TP1/TP2. Max drawdown is measured on realized equity at exits (open losses not marked)."]
    open(os.path.join(args.out, "exits_summary.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
