"""
Pure signal logic for the VWAP scanner (see vwap_scanner.py for the live
loop, data fetching and notifications).

No network, no config import — everything here takes plain bars in and
returns plain results out, so it can be unit-tested offline and replayed
against saved data.

Two setups are detected, each in both directions:

  break_up    – price was BELOW session VWAP, then closes ABOVE it for
                `confirm_bars` straight bars, beyond a noise buffer, on
                elevated volume.  (VWAP reclaim → long idea)
  break_down  – the mirror image.                (VWAP loss → short idea)

  bounce_up   – price has been trending ABOVE VWAP, pulls back INTO the
                VWAP zone without closing through it, then a green bar
                closes back up out of the zone.  (VWAP support hold → long)
  bounce_down – the mirror image.  (VWAP resistance rejection → short)

A "chop" filter suppresses everything when price has crossed VWAP too many
times recently — in that regime VWAP is a magnet, not a level, and both
setups fail far more often.

Each signal comes with a mechanical stop (the other side of the VWAP zone /
the pullback extreme) and R-multiple targets so you can size the trade
before you click. These are reference levels, not advice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Bar:
    t: datetime          # bar START time, timezone-aware
    o: float
    h: float
    l: float
    c: float
    v: float
    vw: float | None = None   # the bar's own VWAP if the feed supplies it

    @property
    def price(self) -> float:
        """Representative traded price for the bar: the feed's per-bar VWAP
        when available (exact), otherwise the typical price (H+L+C)/3."""
        if self.vw is not None and self.vw > 0:
            return self.vw
        return (self.h + self.l + self.c) / 3


@dataclass
class Params:
    # Ignore the first N bars of the session: VWAP is built from only a
    # handful of prints and whipsaws meaninglessly.
    min_session_bars: int = 10

    # Noise buffer around VWAP = max(atr_mult * ATR(atr_len), min_buffer_bps).
    atr_len: int = 14
    atr_mult: float = 0.25
    min_buffer_bps: float = 3.0          # 3 bps = 0.03% of price

    # --- break-through ---
    side_lookback: int = 5               # bars that must have CLOSED on the old side
    confirm_bars: int = 2                # consecutive closes on the new side, beyond the buffer
    vol_lookback: int = 20
    break_rel_vol: float = 1.5           # max confirm-bar volume vs. avg of prior bars

    # --- bounce ---
    trend_lookback: int = 10             # bars before the pullback used to judge trend side
    trend_min_frac: float = 0.8          # share of those closes required on the trend side
    touch_window: int = 3                # pullback must touch the zone within the N bars before the trigger bar
    bounce_rel_vol: float = 1.0          # trigger-bar volume vs. average

    # --- regime filter ---
    chop_lookback: int = 30
    chop_max_crosses: int = 4            # more VWAP crosses than this in the lookback = chop, stay silent

    # --- targets ---
    target_r: tuple = (1.0, 2.0)


@dataclass
class Signal:
    symbol: str
    kind: str                 # break_up | break_down | bounce_up | bounce_down
    direction: str            # long | short
    bar_time: str             # ISO time of the bar that triggered
    price: float              # trigger-bar close (reference entry)
    vwap: float
    sd: float                 # volume-weighted std-dev of price around VWAP
    distance_pct: float       # (price - vwap) / vwap * 100
    rel_volume: float
    buffer: float             # size of the VWAP noise zone used
    stop: float
    risk_per_share: float
    targets: list = field(default_factory=list)
    band_target: float | None = None   # next VWAP σ band in trade direction
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def vwap_series(bars: list[Bar]) -> list[tuple[float, float]]:
    """Cumulative session VWAP and volume-weighted standard deviation at
    each bar. Bars with zero volume carry the previous values forward."""
    out = []
    pv = vol = pv2 = 0.0
    last = (bars[0].price if bars else 0.0, 0.0)
    for b in bars:
        if b.v > 0:
            p = b.price
            pv += p * b.v
            pv2 += p * p * b.v
            vol += b.v
            vwap = pv / vol
            var = max(pv2 / vol - vwap * vwap, 0.0)
            last = (vwap, math.sqrt(var))
        out.append(last)
    return out


def atr(bars: list[Bar], n: int) -> float:
    """Simple-average true range over the last n bars (session only)."""
    if not bars:
        return 0.0
    trs = []
    for i in range(max(1, len(bars) - n), len(bars)):
        prev_c = bars[i - 1].c
        b = bars[i]
        trs.append(max(b.h - b.l, abs(b.h - prev_c), abs(b.l - prev_c)))
    if not trs:  # single bar
        return bars[0].h - bars[0].l
    return sum(trs) / len(trs)


def count_crosses(bars: list[Bar], vw: list[tuple[float, float]], lookback: int) -> int:
    start = max(0, len(bars) - lookback)
    crosses = 0
    prev_side = None
    for i in range(start, len(bars)):
        side = bars[i].c > vw[i][0]
        if prev_side is not None and side != prev_side:
            crosses += 1
        prev_side = side
    return crosses


def _avg_volume(bars: list[Bar], end: int, n: int) -> float:
    window = [b.v for b in bars[max(0, end - n):end] if b.v > 0]
    return sum(window) / len(window) if window else 0.0


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect(symbol: str, bars: list[Bar], p: Params | None = None) -> list[Signal]:
    """Evaluate ONLY the last bar in `bars` (which must be a completed bar)
    and return any signals it triggers. Call it once per new completed bar;
    pass the whole session so far so VWAP is correct."""
    p = p or Params()
    n = len(bars)
    need = max(p.min_session_bars, p.side_lookback + p.confirm_bars,
               p.trend_lookback + p.touch_window + 1)
    if n < need:
        return []

    vw = vwap_series(bars)
    last = bars[-1]
    vwap, sd = vw[-1]
    if vwap <= 0:
        return []

    if count_crosses(bars, vw, p.chop_lookback) > p.chop_max_crosses:
        return []

    a = atr(bars, p.atr_len)
    buf = max(p.atr_mult * a, last.c * p.min_buffer_bps / 10_000)
    avg_vol = _avg_volume(bars, n - 1, p.vol_lookback)

    signals: list[Signal] = []
    for direction in ("long", "short"):
        s = _check_break(symbol, bars, vw, buf, avg_vol, p, direction)
        if s:
            signals.append(s)
            continue
        s = _check_bounce(symbol, bars, vw, buf, avg_vol, p, direction)
        if s:
            signals.append(s)
    return signals


def _above(c: float, vwap: float, buf: float, direction: str) -> bool:
    """True if close is beyond VWAP+buffer on the trade side."""
    return c > vwap + buf if direction == "long" else c < vwap - buf


def _on_side(c: float, vwap: float, direction: str) -> bool:
    return c > vwap if direction == "long" else c < vwap


def _check_break(symbol, bars, vw, buf, avg_vol, p: Params, direction) -> Signal | None:
    n = len(bars)
    k = p.confirm_bars
    confirm = range(n - k, n)
    prior = range(n - k - p.side_lookback, n - k)

    # Every confirm bar closes beyond the zone on the new side...
    if not all(_above(bars[i].c, vw[i][0], buf, direction) for i in confirm):
        return None
    # ...and before that, price was genuinely on the OLD side (a fresh cross,
    # not a continuation): no prior close was already beyond the zone on the
    # new side, and all but one closed on the old side. The one allowed
    # exception is the crossing bar, which often closes a hair through VWAP
    # but still inside the noise zone.
    opposite = "short" if direction == "long" else "long"
    if any(_above(bars[i].c, vw[i][0], buf, direction) for i in prior):
        return None
    if sum(1 for i in prior if _on_side(bars[i].c, vw[i][0], opposite)) < p.side_lookback - 1:
        return None

    peak_vol = max(bars[i].v for i in confirm)
    rel = peak_vol / avg_vol if avg_vol else 0.0
    if rel < p.break_rel_vol:
        return None

    vwap, sd = vw[-1]
    entry = bars[-1].c
    if direction == "long":
        stop = min(min(bars[i].l for i in confirm), vwap - buf)
    else:
        stop = max(max(bars[i].h for i in confirm), vwap + buf)
    kind = "break_up" if direction == "long" else "break_down"
    reason = (f"{p.side_lookback} closes {'below' if direction == 'long' else 'above'} VWAP, "
              f"then {k} closes {'above' if direction == 'long' else 'below'} it beyond the "
              f"{buf:.2f} noise buffer on {rel:.1f}x volume")
    return _build(symbol, kind, direction, bars[-1], entry, vwap, sd, rel, buf, stop, p, reason)


def _check_bounce(symbol, bars, vw, buf, avg_vol, p: Params, direction) -> Signal | None:
    n = len(bars)
    trig = n - 1
    touch = range(n - 1 - p.touch_window, n - 1)
    trend = range(n - 1 - p.touch_window - p.trend_lookback, n - 1 - p.touch_window)

    # 1) Trend context: most closes before the pullback were on the trade side.
    on_side = sum(1 for i in trend if _on_side(bars[i].c, vw[i][0], direction))
    if on_side / p.trend_lookback < p.trend_min_frac:
        return None

    # 2) The pullback reached the VWAP zone...
    if direction == "long":
        touched = [i for i in touch if bars[i].l <= vw[i][0] + buf]
    else:
        touched = [i for i in touch if bars[i].h >= vw[i][0] - buf]
    if not touched:
        return None
    # ...but never CLOSED through it (that would be a break, not a bounce).
    if direction == "long":
        if any(bars[i].c < vw[i][0] - buf for i in touch):
            return None
    else:
        if any(bars[i].c > vw[i][0] + buf for i in touch):
            return None

    # 3) Trigger bar: closes back out of the zone, in the trade direction,
    #    beyond every close in the pullback, with at least normal volume.
    t = bars[trig]
    vwap, sd = vw[trig]
    if not _above(t.c, vwap, buf, direction):
        return None
    if direction == "long":
        if not (t.c > t.o and t.c > max(bars[i].c for i in touch)):
            return None
    else:
        if not (t.c < t.o and t.c < min(bars[i].c for i in touch)):
            return None
    rel = t.v / avg_vol if avg_vol else 0.0
    if rel < p.bounce_rel_vol:
        return None

    if direction == "long":
        stop = min(bars[i].l for i in touch) - buf * 0.5
    else:
        stop = max(bars[i].h for i in touch) + buf * 0.5
    kind = "bounce_up" if direction == "long" else "bounce_down"
    reason = (f"trend {'above' if direction == 'long' else 'below'} VWAP "
              f"({on_side}/{p.trend_lookback} closes), pulled back into the VWAP zone, "
              f"held, and reversed on {rel:.1f}x volume")
    return _build(symbol, kind, direction, t, t.c, vwap, sd, rel, buf, stop, p, reason)


def _build(symbol, kind, direction, bar, entry, vwap, sd, rel, buf, stop, p: Params, reason) -> Signal | None:
    risk = entry - stop if direction == "long" else stop - entry
    if risk <= 0:
        return None
    sign = 1 if direction == "long" else -1
    targets = [round(entry + sign * r * risk, 2) for r in p.target_r]
    # Next σ band (1σ, 2σ, 3σ) at least 0.5R beyond the entry in the trade
    # direction — a band a few cents away is not a meaningful target.
    band = None
    if sd > 0:
        for m in (1, 2, 3):
            lvl = vwap + sign * m * sd
            if (lvl - entry) * sign >= 0.5 * risk:
                band = round(lvl, 2)
                break
    return Signal(
        symbol=symbol, kind=kind, direction=direction,
        bar_time=bar.t.isoformat(), price=round(entry, 2),
        vwap=round(vwap, 2), sd=round(sd, 3),
        distance_pct=round((entry - vwap) / vwap * 100, 3),
        rel_volume=round(rel, 2), buffer=round(buf, 3),
        stop=round(stop, 2), risk_per_share=round(risk, 2),
        targets=targets, band_target=band, reason=reason,
    )


def replay(symbol: str, bars: list[Bar], p: Params | None = None) -> list[Signal]:
    """Run detect() bar-by-bar across a whole session, exactly as the live
    loop would see it. Used for backtesting saved data and in tests."""
    out = []
    for i in range(1, len(bars) + 1):
        out.extend(detect(symbol, bars[:i], p))
    return out
