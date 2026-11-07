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
BLOCKED_FILE = os.path.join(STATE_DIR, "blocked.jsonl")
# Index filter: longs only while SPY and QQQ are both above their VWAP, shorts
# only while both are below; mixed = no alerts. Inverse ETFs flip direction.
INDEX_SYMBOLS = ("SPY", "QQQ")
INVERSE = {"SQQQ"}
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


# NYSE calendar, from nyse.com/markets/hours-calendars and ICE's 2024-11-08
# holiday press release. Extend when NYSE publishes later years.
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
    "2028-09-04", "2028-11-23", "2028-12-25",
}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24"}   # 1:00 PM ET
CALENDAR_LAST_YEAR = 2028


def session_bounds(day_et: datetime) -> tuple[datetime, datetime]:
    close_h = 13 if day_et.date().isoformat() in NYSE_EARLY_CLOSE else 16
    return (day_et.replace(hour=9, minute=30, second=0, microsecond=0),
            day_et.replace(hour=close_h, minute=0, second=0, microsecond=0))


def is_trading_day(day_et: datetime) -> tuple[bool, str]:
    d = day_et.date()
    if d.weekday() >= 5:
        return False, "weekend"
    if d.isoformat() in NYSE_HOLIDAYS:
        return False, "NYSE holiday"
    if d.year > CALENDAR_LAST_YEAR:
        return True, f"weekday (holiday calendar only covers through {CALENDAR_LAST_YEAR})"
    return True, "early close 1:00 PM ET" if d.isoformat() in NYSE_EARLY_CLOSE else "regular session"


def in_session(now_et: datetime) -> bool:
    o, c = session_bounds(now_et)
    return is_trading_day(now_et)[0] and o <= now_et < c


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

    state["ticks"] = state.get("ticks", 0) + 1
    state["interval"] = interval
    alerts = 0
    candidates: list[tuple[str, dict, datetime, Signal]] = []
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
                candidates.append((sym, st, b.t, s))
        if len(bars) > MAX_BARS:         # fold the oldest bars into the seed
            for old in bars[:-MAX_BARS]:
                p = old.price
                seed = (seed[0] + p * old.v, seed[1] + old.v, seed[2] + p * p * old.v)
            bars = bars[-MAX_BARS:]
            st["seed"] = list(seed)
        st["bars"] = [bar_to_dict(b) for b in bars]

    side = market_side(state)
    blocked = 0
    for sym, st, bar_t, s in candidates:
        ok, why = index_allows(sym, s, side)
        if not ok:
            blocked += 1
            lines.append(f"BLOCKED {short(sym)} {LABEL[s.kind]} ({s.direction}): {why}")
            _log(sym, s, BLOCKED_FILE, why)
            continue
        st["fired"][s.kind] = bar_t.isoformat()
        lines.append("ALERT " + format_alert(sym, s, interval))
        alerts += 1
        _log(sym, s)

    save_state(state)
    n_bars = min((len(s["bars"]) for s in state["symbols"].values()), default=0)
    warm = "" if n_bars >= params.min_session_bars + 2 else f" · warming up ({n_bars} bars)"
    blk = f" · {blocked} blocked by index filter" if blocked else ""
    lines.append(f"STATUS {now_et:%H:%M} ET · {len(data)} tickers · {alerts} alert(s){blk}{warm}")
    return lines


def _log(sym: str, s: Signal, path: str | None = None, why: str | None = None) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    rec = {"symbol_full": sym, **s.to_dict()}
    if why:
        rec["blocked_reason"] = why
    with open(path or LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def market_side(state: dict) -> dict:
    """Each index ETF's last price vs its session VWAP: {"SPY": +1|-1, ...}.
    An index with no data yet is left out."""
    from vwap_signals import vwap_series
    out = {}
    for sym, st in state.get("symbols", {}).items():
        if short(sym) not in INDEX_SYMBOLS:
            continue
        bars = [bar_from_dict(b) for b in st["bars"]]
        seed = tuple(st["seed"])
        if not bars and not seed[1]:
            continue                      # no volume yet: unknown side
        vwap = vwap_series(bars, seed)[-1][0] if bars else seed[0] / seed[1]
        out[short(sym)] = 1 if st["last_c"] > vwap else -1
    return out


def index_allows(sym: str, s: Signal, side: dict) -> tuple[bool, str]:
    """Longs need SPY and QQQ above VWAP, shorts need both below. Inverse
    ETFs (SQQQ) are judged by the market direction they bet on. If either
    index has no data yet the filter stays off."""
    if any(i not in side for i in INDEX_SYMBOLS):
        return True, "index data missing"
    want = 1 if s.direction == "long" else -1
    if short(sym) in INVERSE:
        want = -want
    if all(side[i] == want for i in INDEX_SYMBOLS):
        return True, ""
    desc = ", ".join(f"{i} {'above' if side[i] > 0 else 'below'}" for i in INDEX_SYMBOLS)
    return False, f"{desc} VWAP"


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


def tick_times_et(now: datetime, interval: int) -> list[datetime]:
    """All of today's tick times (ET): 1 min after the open, then every
    `interval` minutes, last one before the close."""
    open_et, close_et = session_bounds(now.astimezone(ET))
    times, t = [], open_et + timedelta(minutes=1)
    while t < close_et:
        times.append(t)
        t += timedelta(minutes=interval)
    return times


CLAIM_FILE = os.path.join(STATE_DIR, "claim.json")


def cmd_claim(now: datetime, interval: int) -> str:
    """Called at the START of every tick. Durable one-shot reminders drive
    the tick chain (in-session cron jobs do not survive the container being
    recycled). Each tick claims its slot here, then schedules the next tick.

    Prints one of:
      CLOSED                 not a trading session right now - do nothing
      DUPLICATE <age>s       another tick ran moments ago - skip entirely
                             (stops a restarted chain from doubling up)
      NEXT <utc iso>         run this tick, schedule the next at that time
      END <utc iso>          run this tick, it is the last; schedule the
                             end-of-day step at that time instead
    """
    now_et = now.astimezone(ET)
    ok, _ = is_trading_day(now_et)
    open_et, close_et = session_bounds(now_et)
    if not ok or not (open_et <= now_et < close_et + timedelta(minutes=1)):
        return "CLOSED"
    try:
        with open(CLAIM_FILE, encoding="utf-8") as f:
            last = datetime.fromisoformat(json.load(f)["t"])
    except (OSError, ValueError, KeyError):
        last = None
    if last and timedelta(0) <= now - last < timedelta(seconds=interval * 60 * 0.5):
        return f"DUPLICATE {int((now - last).total_seconds())}s"
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CLAIM_FILE, "w", encoding="utf-8") as f:
        json.dump({"t": now.isoformat()}, f)
    upcoming = [t for t in tick_times_et(now, interval) if t > now_et + timedelta(seconds=60)]
    fmt = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if upcoming:
        return f"NEXT {fmt(upcoming[0])}"
    return f"END {fmt(close_et + timedelta(minutes=5))}"


def cmd_health(now: datetime, interval: int) -> str:
    """For the hourly watchdog: is the tick chain alive?"""
    now_et = now.astimezone(ET)
    ok, _ = is_trading_day(now_et)
    open_et, close_et = session_bounds(now_et)
    if not ok or not (open_et <= now_et < close_et):
        return "CLOSED"
    try:
        with open(CLAIM_FILE, encoding="utf-8") as f:
            last = datetime.fromisoformat(json.load(f)["t"])
    except (OSError, ValueError, KeyError):
        return "STALE never"
    age = (now - last).total_seconds() / 60
    return f"STALE {age:.0f}m" if age > interval * 2 + 2 else f"OK {age:.0f}m"


def cmd_plan(now: datetime, interval: int) -> dict:
    """Today's tick times as cron expressions in THIS MACHINE'S local time
    (what CronCreate uses), pinned to today's date so a job can never fire
    on a later day. Handles DST, holidays and early closes, because it is
    computed fresh each morning from the America/New_York calendar."""
    now_et = now.astimezone(ET)
    ok, reason = is_trading_day(now_et)
    plan = {"date_et": now_et.date().isoformat(), "trading_day": ok, "reason": reason}
    if not ok:
        return plan
    open_et, close_et = session_bounds(now_et)
    local = datetime.now().astimezone().tzinfo
    # First tick 1 min after the open, then every `interval` minutes; the
    # last one lands before the close.
    times, t = [], open_et + timedelta(minutes=1)
    while t < close_et:
        times.append(t.astimezone(local))
        t += timedelta(minutes=interval)
    by_hour: dict[tuple, list[int]] = {}
    for lt in times:
        by_hour.setdefault((lt.day, lt.month, lt.hour), []).append(lt.minute)
    # Merge consecutive hours that share the same minute list into one job.
    groups: list[tuple] = []
    for (d, m, h), mins in by_hour.items():
        key = ",".join(map(str, mins))
        if groups and groups[-1][0] == key and groups[-1][1] == (d, m) and groups[-1][3] == h - 1:
            groups[-1] = (key, (d, m), groups[-1][2], h)
        else:
            groups.append((key, (d, m), h, h))
    plan["tick_crons"] = [f"{mins} {h0 if h0 == h1 else f'{h0}-{h1}'} {d} {m} *"
                          for mins, (d, m), h0, h1 in groups]
    end = (close_et + timedelta(minutes=5)).astimezone(local)
    plan["end_cron"] = f"{end.minute} {end.hour} {end.day} {end.month} *"
    plan["session_et"] = f"{open_et:%H:%M}-{close_et:%H:%M}"
    plan["ticks_expected"] = len(times)
    plan["first_tick_utc"] = (open_et + timedelta(minutes=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return plan


def grade_alert(alert: dict, bars: list[Bar]) -> dict:
    """What price did after an alert, from the bars collected that day.
    Walks forward bar by bar from the alert: whichever of stop / 1R / 2R is
    hit first decides the outcome. If one bar spans both the stop and a
    target, the stop is assumed first (conservative). Snapshot bars only
    carry intra-bar extremes when they set a new day high/low, so this is an
    approximation, good for spotting patterns across many alerts."""
    long = alert["direction"] == "long"
    entry, stop, risk = alert["price"], alert["stop"], alert["risk_per_share"]
    t1, t2 = alert["targets"][0], alert["targets"][1]
    start = datetime.fromisoformat(alert["bar_time"])
    after = [b for b in bars if b.t > start]
    outcome, mfe, mae = "open", 0.0, 0.0
    hit1 = False
    for b in after:
        fav = (b.h - entry) if long else (entry - b.l)
        adv = (entry - b.l) if long else (b.h - entry)
        mfe, mae = max(mfe, fav), max(mae, adv)
        stopped = b.l <= stop if long else b.h >= stop
        if stopped:
            outcome = "1R then stop" if hit1 else "stop"
            break
        if (b.h >= t2) if long else (b.l <= t2):
            outcome = "2R"
            break
        if (b.h >= t1) if long else (b.l <= t1):
            hit1 = True
            outcome = "1R"
    close_r = None
    if after and risk > 0:
        last = after[-1].c
        close_r = round(((last - entry) if long else (entry - last)) / risk, 2)
    return {"outcome": outcome, "mfe_r": round(mfe / risk, 2) if risk else None,
            "mae_r": round(mae / risk, 2) if risk else None, "close_r": close_r}


def cmd_journal(now: datetime, out_dir: str, ticks_expected: int | None = None) -> list[str]:
    """Write today's review record into the repo: a Markdown page per day,
    plus one CSV row per alert (appended) for reviewing many days at once.
    Re-running on the same day rewrites that day's page and its CSV rows."""
    from vwap_signals import vwap_series
    state = load_state()
    day = state.get("day") or now.astimezone(ET).date().isoformat()
    bars_by_sym = {short(k): [bar_from_dict(b) for b in v["bars"]] for k, v in state.get("symbols", {}).items()}

    alerts = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if datetime.fromisoformat(r["bar_time"]).astimezone(ET).date().isoformat() == day:
                    alerts.append(r)
    except OSError:
        pass
    for a in alerts:
        a["grade"] = grade_alert(a, bars_by_sym.get(a["symbol"], []))

    ticks = state.get("ticks", 0)
    bar_len = timedelta(minutes=state.get("interval", 1))
    exp = f" of {ticks_expected} planned" if ticks_expected else ""
    md = [f"# VWAP scanner — {day}", "",
          f"- Ticks run: **{ticks}**{exp}",
          f"- Alerts: **{len(alerts)}**", ""]
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a["grade"]["outcome"]] = counts.get(a["grade"]["outcome"], 0) + 1
    if counts:
        md += ["Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]

    md += ["## Alerts", ""]
    if alerts:
        md += ["| Alert ET | Ticker | Setup | Dir | Entry | VWAP | Stop | 1R | 2R | RelVol | Outcome | MFE R | MAE R | Close R |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for a in alerts:
            g = a["grade"]
            t = datetime.fromisoformat(a["bar_time"]).astimezone(ET) + bar_len
            md.append(f"| {t:%H:%M} | {a['symbol']} | {LABEL[a['kind']]} | {a['direction']} | {a['price']:.2f} | "
                      f"{a['vwap']:.2f} | {a['stop']:.2f} | {a['targets'][0]:.2f} | {a['targets'][1]:.2f} | "
                      f"{a['rel_volume']:.1f}x | **{g['outcome']}** | {g['mfe_r']} | {g['mae_r']} | {g['close_r']} |")
        md += ["", "Alert ET = when the alert fired (close of the triggering bar). MFE/MAE = best/worst move after the alert in R "
                   "(R = entry-to-stop distance). Grading uses the scanner's snapshot bars, so treat it as approximate."]
    else:
        md.append("No alerts today.")

    blocked = []
    try:
        with open(BLOCKED_FILE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if datetime.fromisoformat(r["bar_time"]).astimezone(ET).date().isoformat() == day:
                    blocked.append(r)
    except OSError:
        pass
    if blocked:
        md += ["", "## Blocked by index filter (not pushed)", "",
               "Graded the same way, to check whether the SPY/QQQ filter is helping.", "",
               "| Alert ET | Ticker | Setup | Dir | Entry | Why blocked | Outcome | MFE R | MAE R |",
               "|---|---|---|---|---|---|---|---|---|"]
        for a in blocked:
            g = grade_alert(a, bars_by_sym.get(a["symbol"], []))
            t = datetime.fromisoformat(a["bar_time"]).astimezone(ET) + bar_len
            md.append(f"| {t:%H:%M} | {a['symbol']} | {LABEL[a['kind']]} | {a['direction']} | {a['price']:.2f} | "
                      f"{a['blocked_reason']} | {g['outcome']} | {g['mfe_r']} | {g['mae_r']} |")

    md += ["", "## End-of-day position vs VWAP", "",
           "| Ticker | Last | VWAP | vs VWAP | Bars |", "|---|---|---|---|---|"]
    for sym, st in sorted(state.get("symbols", {}).items()):
        bars = bars_by_sym[short(sym)]
        seed = tuple(st["seed"])
        vwap = vwap_series(bars, seed)[-1][0] if bars else seed[0] / seed[1]
        px = st["last_c"]
        md.append(f"| {short(sym)} | {px:.2f} | {vwap:.2f} | {(px - vwap) / vwap * 100:+.2f}% | {len(bars)} |")

    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{day}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    csv_path = os.path.join(out_dir, "alerts.csv")
    header = "date,alert_time_et,ticker,setup,direction,entry,vwap,stop,target_1r,target_2r,rel_volume,outcome,mfe_r,mae_r,close_r"
    keep = []
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as f:
            keep = [l.rstrip("\n") for l in f if l.strip() and not l.startswith("date,") and not l.startswith(day + ",")]
    rows = []
    for a in alerts:
        g = a["grade"]
        t = datetime.fromisoformat(a["bar_time"]).astimezone(ET) + bar_len
        rows.append(",".join(str(x) for x in (
            day, f"{t:%H:%M}", a["symbol"], a["kind"], a["direction"], a["price"], a["vwap"], a["stop"],
            a["targets"][0], a["targets"][1], a["rel_volume"], g["outcome"], g["mfe_r"], g["mae_r"], g["close_r"])))
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("\n".join([header] + keep + rows) + "\n")
    return [md_path, csv_path]


def cmd_tick_claimed(payload: dict, now: datetime, params: Params, interval: int) -> list[str]:
    """tick with the slot claim built in: one script call instead of two.
    CLOSED / DUPLICATE come back as a single line and nothing is processed."""
    res = cmd_claim(now, interval)
    if res.startswith(("CLOSED", "DUPLICATE")):
        return [res]
    return cmd_tick(payload, now, params, interval)


def cmd_block(now: datetime, out_dir: str, ticks_expected: int | None = None) -> str:
    """Write the journal files, then return the whole record as one text block
    (journal page + today's CSV rows) so a reader never has to retype it."""
    md_path, csv_path = cmd_journal(now, out_dir, ticks_expected)
    day = os.path.basename(md_path)[:-3]
    with open(md_path, encoding="utf-8") as f:
        md = f.read().rstrip("\n")
    with open(csv_path, encoding="utf-8") as f:
        rows = [l.rstrip("\n") for l in f if l.startswith(day + ",")]
    return "\n".join([f"=== VWAP JOURNAL {day} PUSHED=no ===", md, "=== VWAP CSV ROWS ===", *rows,
                      "=== VWAP END ==="])


def cmd_eod(now: datetime, interval: int, out_dir: str) -> str:
    """End of day in one call: NO_SESSION, or `PUSH <summary line>` followed
    by the full journal block."""
    plan = cmd_plan(now, interval)
    if not plan["trading_day"]:
        return f"NO_SESSION {plan['reason']}"
    summary = cmd_summary(now)
    return f"PUSH {summary}\n" + cmd_block(now, out_dir, plan["ticks_expected"])


def cmd_summary(now: datetime) -> str:
    state = load_state()
    day = state.get("day") or now.astimezone(ET).date().isoformat()
    alerts = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if datetime.fromisoformat(r["bar_time"]).astimezone(ET).date().isoformat() == day:
                    alerts.append(f"{r['symbol']} {LABEL[r['kind']]}")
    except OSError:
        pass
    shown = "; ".join(alerts[:8]) + (" …" if len(alerts) > 8 else "")
    return (f"VWAP scanner {day} done: {len(alerts)} alert(s), {state.get('ticks', 0)} ticks run"
            + (f" — {shown}" if alerts else ""))


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
    t.add_argument("--claim", action="store_true", help="also claim the slot first; prints CLOSED/DUPLICATE and stops")
    s = sub.add_parser("seed", help="seed one ticker's VWAP from today's OHLCV JSON on stdin")
    s.add_argument("symbol")
    s.add_argument("--now", help="ISO timestamp override (testing)")
    sub.add_parser("status")
    pl = sub.add_parser("plan", help="today's tick schedule as cron expressions in this machine's local time")
    pl.add_argument("--interval", type=int, default=5)
    pl.add_argument("--now", help="ISO timestamp override (testing)")
    sub.add_parser("summary", help="one-line end-of-day summary")
    jr = sub.add_parser("journal", help="write today's review page + alerts.csv rows into the repo")
    jr.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "vwap_journal"))
    jr.add_argument("--ticks-expected", type=int, default=None)
    jr.add_argument("--block", action="store_true", help="print the whole journal as one text block")
    eo = sub.add_parser("eod", help="end of day in one call: summary + journal files + full block")
    eo.add_argument("--interval", type=int, default=5)
    eo.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "vwap_journal"))
    eo.add_argument("--now", help="ISO timestamp override (testing)")
    sub.add_parser("reset")
    for name, helptext in (("claim", "start of every tick: claim the slot, print NEXT/END/DUPLICATE/CLOSED"),
                           ("health", "watchdog: is the tick chain alive?")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--interval", type=int, default=5)
        sp.add_argument("--now", help="ISO timestamp override (testing)")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now) if getattr(args, "now", None) else datetime.now(timezone.utc)
    if args.cmd == "tick":
        run = cmd_tick_claimed if args.claim else cmd_tick
        print("\n".join(run(json.load(sys.stdin), now, params_for(args.interval), args.interval)))
    elif args.cmd == "seed":
        print(cmd_seed(args.symbol, json.load(sys.stdin), now))
    elif args.cmd == "claim":
        print(cmd_claim(now, args.interval))
    elif args.cmd == "health":
        print(cmd_health(now, args.interval))
    elif args.cmd == "plan":
        print(json.dumps(cmd_plan(now, args.interval), indent=2))
    elif args.cmd == "journal":
        if args.block:
            print(cmd_block(now, args.out, args.ticks_expected))
        else:
            print("\n".join(cmd_journal(now, args.out, args.ticks_expected)))
    elif args.cmd == "eod":
        print(cmd_eod(now, args.interval, args.out))
    elif args.cmd == "summary":
        print(cmd_summary(now))
    elif args.cmd == "status":
        print("\n".join(cmd_status(now)))
    elif args.cmd == "reset":
        for f in (STATE_FILE, CLAIM_FILE):
            if os.path.exists(f):
                os.remove(f)
        print("state cleared")


if __name__ == "__main__":
    main()
