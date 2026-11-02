"""
VWAP bounce / break scanner driven by TradingView connector snapshots.

Why this exists alongside vwap_scanner.py: that script pulls Alpaca bars,
which are IEX-only and partly delayed on the free plan. This one runs on
TradingView's real-time ("streaming") quote snapshots instead, which Claude
fetches through the TradingView connector and pipes in here. Only Claude can
call the connector, so this script can't fetch data itself. A
recurring Claude task runs one tick per minute:

    1. Claude calls mcp-tv-get-symbol-data-batch for every ticker
       (columns: close, volume, high, low, open) — ONE call for up to 50 tickers.
    2. Claude pipes that JSON into:   python3 tv_vwap_scanner.py tick
    3. For every line starting with ALERT, Claude sends a phone push.

How bars are built from snapshots: the quote's `volume` is the cumulative
CONSOLIDATED session volume, so the change between two snapshots is exactly
the volume that traded in between — this gives a full-tape VWAP (the 1-min
bar feed available through the connector only carries part of the volume).
Each interval becomes one 1-minute bar: open = previous price, close = current
price, high/low = the move between them, widened to the day's new high/low
when one was set. Intra-minute wicks that don't set a day extreme are not
visible, so bounce "touches" are judged on closes/extremes, slightly stricter
than on a full bar chart. Late ticks are split into per-minute bars so
relative volume stays comparable.

Starting mid-session: run `seed` first for each ticker with that day's 5-minute
OHLCV bars (from mcp-tv-get-ohlcv) so the VWAP includes everything that
traded before the scanner started:

    python3 tv_vwap_scanner.py seed SPY < spy_5m.json

Other commands:
    python3 tv_vwap_scanner.py status     # each ticker: price vs VWAP, bars collected
    python3 tv_vwap_scanner.py reset      # clear today's state

State lives in .vwap_state/state.json (git-ignored). Signal rules are in
vwap_signals.py, shared with vwap_scanner.py and covered by the tests.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vwap_signals import Bar, Params, Signal, detect

ET = ZoneInfo("America/New_York")
STATE_DIR = os.getenv("VWAP_STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vwap_state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "signals.jsonl")
COOLDOWN = timedelta(minutes=15)
MAX_SPLIT = 10          # a gap longer than this many intervals is one bar, not ten


def params_for(interval_min: int) -> Params:
    """Rule set per tick interval. The 1-minute defaults live in Params; at
    5 minutes each bar carries 5x the information, so lookbacks shrink and a
    single 5-minute close beyond the zone is enough to confirm a break."""
    if interval_min == 1:
        return Params()
    if interval_min == 5:
        return Params(
            min_session_bars=3,      # 15 min after the open
            atr_len=6,               # 30 min
            side_lookback=3,         # 15 min on the old side
            confirm_bars=1,          # one 5-min close beyond the zone
            vol_lookback=12,         # vs. the last hour
            # Averaging over 5 bars shrinks volume spikes ~sqrt(5)x, so the
            # 1-min rule's 1.5x (= +0.5 above normal) becomes 1 + 0.5/sqrt(5).
            break_rel_vol=1.25,
            trend_lookback=4,        # 20 min of trend before the pullback
            trend_min_frac=0.75,
            touch_window=2,          # pullback within the last 10 min
            chop_lookback=12,        # crosses in the last hour
            chop_max_crosses=3,
        )
    raise SystemExit(f"unsupported --interval {interval_min}; use 1 or 5")
MAX_BARS = 420          # > one full session of minute bars

LABEL = {
    "break_up": "VWAP RECLAIM",
    "break_down": "VWAP LOSS",
    "bounce_up": "VWAP BOUNCE",
    "bounce_down": "VWAP REJECT",
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"symbols": {}, "seeds": {}}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)          # atomic: a crash never leaves half a file


def bar_to_dict(b: Bar) -> dict:
    return {"t": b.t.isoformat(), "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}


def bar_from_dict(d: dict) -> Bar:
    return Bar(t=datetime.fromisoformat(d["t"]), o=d["o"], h=d["h"], l=d["l"], c=d["c"], v=d["v"])


def short(sym: str) -> str:
    return sym.split(":")[-1]


def session_bounds(day_et: datetime) -> tuple[datetime, datetime]:
    return (day_et.replace(hour=9, minute=30, second=0, microsecond=0),
            day_et.replace(hour=16, minute=0, second=0, microsecond=0))


def in_session(now_et: datetime) -> bool:
    o, c = session_bounds(now_et)
    return now_et.weekday() < 5 and o <= now_et < c


# ---------------------------------------------------------------------------
# Snapshot -> bars
# ---------------------------------------------------------------------------

def seed_tuple(vwap: float, sd: float, volume: float) -> list[float]:
    """(sum pv, sum v, sum p²v) reproducing a known VWAP/σ at a given volume."""
    return [vwap * volume, volume, (sd * sd + vwap * vwap) * volume]


def init_symbol(snap: dict, now: datetime, pending_seed: dict | None) -> dict:
    V = float(snap["volume"])
    if pending_seed:
        seed = seed_tuple(pending_seed["vwap"], pending_seed["sd"], V)
        seeded = "5m-bars"
    else:
        # No history: assume everything so far traded at the day's typical
        # price. Accurate in the first minute or two after the open; a rough
        # approximation later in the day, so run `seed` for mid-day starts.
        tp = (float(snap["high"]) + float(snap["low"]) + float(snap["close"])) / 3
        seed = seed_tuple(tp, 0.0, V)
        seeded = "typical-price"
    return {
        "seed": seed, "seeded_from": seeded, "bars": [],
        "last_t": now.isoformat(), "last_c": float(snap["close"]), "last_v": V,
        "last_h": float(snap["high"]), "last_l": float(snap["low"]),
        "fired": {},
    }


def snapshot_to_bars(st: dict, snap: dict, now: datetime, interval: int = 1) -> list[Bar]:
    """New `interval`-minute bars covering the time since the previous snapshot."""
    c, V = float(snap["close"]), float(snap["volume"])
    hi, lo = float(snap["high"]), float(snap["low"])
    dv = V - st["last_v"]
    if dv <= 0:
        return []                        # no trades (or a stale/duplicate quote)
    prev_t = datetime.fromisoformat(st["last_t"])
    secs = max((now - prev_t).total_seconds(), 1.0)
    k = max(1, min(MAX_SPLIT, round(secs / (60 * interval))))
    o0 = st["last_c"]
    bars = []
    for i in range(k):
        o = o0 + (c - o0) * i / k
        cl = o0 + (c - o0) * (i + 1) / k
        h, l = max(o, cl), min(o, cl)
        if i == k - 1:                   # attribute a new day extreme to the latest bar
            if hi > st["last_h"]:
                h = max(h, hi)
            if lo < st["last_l"]:
                l = min(l, lo)
        t = prev_t + timedelta(seconds=secs * (i + 1) / k) - timedelta(minutes=interval)
        bars.append(Bar(t=t, o=o, h=h, l=l, c=cl, v=dv / k))
    st.update(last_t=now.isoformat(), last_c=c, last_v=V, last_h=hi, last_l=lo)
    return bars


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def format_alert(sym: str, s: Signal, interval: int = 1) -> str:
    t = datetime.fromisoformat(s.bar_time).astimezone(ET) + timedelta(minutes=interval)
    arrow = "▲" if s.direction == "long" else "▼"
    return (f"{arrow} {short(sym)} {LABEL[s.kind]} {s.price:.2f} ({s.direction}) | "
            f"VWAP {s.vwap:.2f} | stop {s.stop:.2f} | 1R {s.targets[0]:.2f} 2R {s.targets[1]:.2f} | "
            f"relvol {s.rel_volume:.1f}x | {t:%H:%M} ET")


def cmd_tick(payload: dict, now: datetime, params: Params, interval: int = 1) -> list[str]:
    data = payload.get("data", payload)
    now_et = now.astimezone(ET)
    lines: list[str] = []
    if not in_session(now_et):
        return [f"STATUS market closed ({now_et:%a %H:%M} ET) — nothing to do"]

    state = load_state()
    today = now_et.date().isoformat()
    if state.get("day") != today:
        state = {"day": today, "symbols": {}, "seeds": {
            k: v for k, v in state.get("seeds", {}).items() if v.get("day") == today}}

    alerts = 0
    for sym, snap in sorted(data.items()):
        if not isinstance(snap, dict) or snap.get("close") is None or snap.get("volume") is None:
            lines.append(f"WARN {short(sym)}: incomplete quote, skipped")
            continue
        st = state["symbols"].get(sym)
        if st is None:
            st = init_symbol(snap, now, state["seeds"].get(short(sym)))
            state["symbols"][sym] = st
            continue
        new = snapshot_to_bars(st, snap, now, interval)
        if not new:
            continue
        bars = [bar_from_dict(b) for b in st["bars"]]
        seed = tuple(st["seed"])
        for b in new:
            bars.append(b)
            for s in detect(short(sym), bars, params, seed):
                last = st["fired"].get(s.kind)
                if last and b.t - datetime.fromisoformat(last) < COOLDOWN:
                    continue
                st["fired"][s.kind] = b.t.isoformat()
                lines.append("ALERT " + format_alert(sym, s, interval))
                alerts += 1
                _log(sym, s)
        if len(bars) > MAX_BARS:         # fold the oldest bars into the seed
            for old in bars[:-MAX_BARS]:
                p = old.price
                seed = (seed[0] + p * old.v, seed[1] + old.v, seed[2] + p * p * old.v)
            bars = bars[-MAX_BARS:]
            st["seed"] = list(seed)
        st["bars"] = [bar_to_dict(b) for b in bars]

    save_state(state)
    n_bars = min((len(s["bars"]) for s in state["symbols"].values()), default=0)
    warm = "" if n_bars >= params.min_session_bars + 2 else f" · warming up ({n_bars} bars)"
    lines.append(f"STATUS {now_et:%H:%M} ET · {len(data)} tickers · {alerts} alert(s){warm}")
    return lines


def _log(sym: str, s: Signal) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"symbol_full": sym, **s.to_dict()}) + "\n")


def cmd_seed(symbol: str, payload: dict, now: datetime) -> str:
    """payload = mcp-tv-get-ohlcv output (rows or columns) for TODAY's
    regular session, any intraday interval (5m recommended)."""
    bars = payload.get("bars", payload)
    if isinstance(bars, dict):           # columns format
        rows = [dict(zip(("t", "o", "h", "l", "c", "v"), vals))
                for vals in zip(bars["t"], bars["o"], bars["h"], bars["l"], bars["c"], bars["v"])]
    else:
        rows = bars
    now_et = now.astimezone(ET)
    o, c = session_bounds(now_et)
    pv = v = pv2 = 0.0
    used = 0
    for r in rows:
        t = datetime.fromtimestamp(int(r["t"]), timezone.utc)
        if not (o <= t.astimezone(ET) < c) or r["v"] <= 0:
            continue
        p = (r["h"] + r["l"] + r["c"]) / 3
        pv += p * r["v"]; v += r["v"]; pv2 += p * p * r["v"]; used += 1
    if not v:
        return f"ERROR {symbol}: no regular-session bars for today in the payload"
    vwap = pv / v
    sd = math.sqrt(max(pv2 / v - vwap * vwap, 0.0))
    state = load_state()
    state.setdefault("seeds", {})[symbol.upper()] = {"day": now_et.date().isoformat(), "vwap": vwap, "sd": sd}
    # If the symbol is already being tracked today, re-seed it in place.
    for full, st in state.get("symbols", {}).items():
        if short(full) == symbol.upper() and state.get("day") == now_et.date().isoformat():
            V_before = st["last_v"] - sum(b["v"] for b in st["bars"])
            st["seed"] = seed_tuple(vwap, sd, max(V_before, 1.0))
            st["seeded_from"] = "5m-bars"
    save_state(state)
    return f"SEEDED {symbol.upper()} from {used} bars: VWAP {vwap:.2f} σ {sd:.2f}"


def cmd_status(now: datetime) -> list[str]:
    from vwap_signals import vwap_series
    state = load_state()
    if not state.get("symbols"):
        return ["STATUS no symbols tracked yet today"]
    out = [f"day {state.get('day')}"]
    for sym, st in sorted(state["symbols"].items()):
        bars = [bar_from_dict(b) for b in st["bars"]]
        seed = tuple(st["seed"])
        vwap = vwap_series(bars, seed)[-1][0] if bars else seed[0] / seed[1]
        px = st["last_c"]
        out.append(f"{short(sym):6} {px:>9.2f}  VWAP {vwap:>9.2f}  {(px - vwap) / vwap * 100:+6.2f}%  "
                   f"{'above' if px > vwap else 'below'}  bars={len(bars)}  seed={st['seeded_from']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="feed one get_symbol_data_batch JSON on stdin")
    t.add_argument("--now", help="ISO timestamp override (testing)")
    t.add_argument("--interval", type=int, default=1, help="minutes between ticks: 1 or 5 (default 1)")
    s = sub.add_parser("seed", help="seed one ticker's VWAP from today's OHLCV JSON on stdin")
    s.add_argument("symbol")
    s.add_argument("--now", help="ISO timestamp override (testing)")
    sub.add_parser("status")
    sub.add_parser("reset")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now) if getattr(args, "now", None) else datetime.now(timezone.utc)
    if args.cmd == "tick":
        print("\n".join(cmd_tick(json.load(sys.stdin), now, params_for(args.interval), args.interval)))
    elif args.cmd == "seed":
        print(cmd_seed(args.symbol, json.load(sys.stdin), now))
    elif args.cmd == "status":
        print("\n".join(cmd_status(now)))
    elif args.cmd == "reset":
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print("state cleared")


if __name__ == "__main__":
    main()
