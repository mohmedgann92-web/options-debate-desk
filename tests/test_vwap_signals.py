"""
Offline tests for the VWAP scanner rules. Standard library only:

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vwap_signals import Bar, Params, detect, replay, vwap_series  # noqa: E402
import vwap_scanner  # noqa: E402

T0 = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)  # 09:30 ET
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "spy_2026-11-02_tv.json")


def mk(closes, vols=None, wick=0.05, opens=None, lows=None, highs=None):
    """Build 1-minute bars from a close path. o = previous close unless given."""
    vols = vols or [1000] * len(closes)
    bars = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        h = highs[i] if highs else max(o, c) + wick
        l = lows[i] if lows else min(o, c) - wick
        bars.append(Bar(t=T0 + timedelta(minutes=i), o=o, h=h, l=l, c=c, v=vols[i]))
    return bars


def kinds(signals):
    return [s.kind for s in signals]


class TestVwap(unittest.TestCase):
    def test_vwap_matches_hand_calc(self):
        bars = [Bar(T0, 10, 10, 10, 10, 100), Bar(T0, 12, 12, 12, 12, 300)]
        vwap, sd = vwap_series(bars)[-1]
        self.assertAlmostEqual(vwap, 11.5)
        self.assertAlmostEqual(sd, (0.25 * 2.25 + 0.75 * 0.25) ** 0.5)

    def test_uses_feed_vw_when_present(self):
        b = Bar(T0, 10, 11, 9, 10, 100, vw=10.8)
        self.assertEqual(b.price, 10.8)


class TestBreaks(unittest.TestCase):
    def _below_then_reclaim(self, vol_on_break=3000):
        # Drift down well below the opening VWAP, then reclaim it hard.
        closes = [100.0] * 5 + [99.8, 99.6, 99.5, 99.4, 99.4, 99.45, 99.4, 99.5, 99.45, 99.5] + [100.3, 100.4]
        vols = [1000] * (len(closes) - 2) + [vol_on_break, vol_on_break]
        return mk(closes, vols)

    def test_break_up_fires_on_volume(self):
        sigs = detect("X", self._below_then_reclaim())
        self.assertEqual(kinds(sigs), ["break_up"])
        s = sigs[0]
        self.assertEqual(s.direction, "long")
        self.assertLess(s.stop, s.price)
        self.assertGreater(s.targets[0], s.price)
        self.assertAlmostEqual(s.targets[1] - s.price, 2 * s.risk_per_share, places=1)

    def test_break_up_needs_volume(self):
        self.assertEqual(detect("X", self._below_then_reclaim(vol_on_break=1000)), [])

    def test_break_fires_once_not_every_bar_after(self):
        bars = self._below_then_reclaim()
        bars += mk([100.5, 100.6, 100.7], [3000] * 3)
        for i, b in enumerate(bars[-3:]):
            b.t = bars[-4].t + timedelta(minutes=i + 1)
        sigs = replay("X", bars)
        self.assertEqual(kinds(sigs).count("break_up"), 1)

    def test_break_down_is_mirror(self):
        closes = [100.0] * 5 + [100.2, 100.4, 100.5, 100.6, 100.6, 100.55, 100.6, 100.5, 100.55, 100.5] + [99.7, 99.6]
        vols = [1000] * (len(closes) - 2) + [3000, 3000]
        sigs = detect("X", mk(closes, vols))
        self.assertEqual(kinds(sigs), ["break_down"])
        self.assertGreater(sigs[0].stop, sigs[0].price)

    def test_crossing_bar_inside_zone_does_not_block_break(self):
        # Regression: the bar that crosses VWAP closes a hair through it but
        # inside the noise buffer; that must not disqualify the break.
        closes = [100.0] * 5 + [100.2, 100.4, 100.5, 100.5, 100.5, 100.45, 100.5, 100.45, 100.4, 100.25] + [99.6, 99.5]
        vols = [1000] * (len(closes) - 2) + [3000, 3000]
        self.assertIn("break_down", kinds(detect("X", mk(closes, vols))))

    def test_quiet_until_min_session_bars(self):
        self.assertEqual(detect("X", mk([100, 99, 101], [1, 1, 50])), [])


class TestBounces(unittest.TestCase):
    def test_bounce_up(self):
        # Trend up and away from VWAP, pull back to it, hold, rip higher.
        up = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.1, 101.2, 101.3, 101.4, 101.4, 101.4, 101.3, 101.3, 101.2]
        bars = mk(up)
        vwap_now = vwap_series(bars)[-1][0]
        # Three pullback bars whose lows tag VWAP but close above it.
        pull = [vwap_now + 0.15, vwap_now + 0.1, vwap_now + 0.12]
        bars += mk(pull, lows=[p - 0.2 for p in pull], highs=[p + 0.05 for p in pull], opens=pull)
        bars.append(Bar(t=T0, o=vwap_now + 0.12, h=vwap_now + 0.6, l=vwap_now + 0.1, c=vwap_now + 0.55, v=1500))
        for i, b in enumerate(bars):
            b.t = T0 + timedelta(minutes=i)
        sigs = detect("X", bars)
        self.assertEqual(kinds(sigs), ["bounce_up"])
        self.assertLess(sigs[0].stop, min(b.l for b in bars[-4:-1]))

    def test_no_bounce_when_pullback_closes_through_vwap(self):
        up = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.1, 101.2, 101.3, 101.4, 101.4, 101.4, 101.3, 101.3, 101.2]
        bars = mk(up)
        v = vwap_series(bars)[-1][0]
        bars += mk([v - 0.4, v - 0.3, v + 0.1], opens=[v, v - 0.4, v - 0.3])
        bars.append(Bar(t=T0, o=v + 0.1, h=v + 0.7, l=v, c=v + 0.6, v=1500))
        for i, b in enumerate(bars):
            b.t = T0 + timedelta(minutes=i)
        self.assertNotIn("bounce_up", kinds(detect("X", bars)))


class TestChop(unittest.TestCase):
    def test_chop_suppresses(self):
        # Whipsaw across VWAP, then the same clean reclaim as TestBreaks.
        closes = [100.0] * 5
        for i in range(12):
            closes.append(100.3 if i % 2 else 99.7)
        closes += [99.5, 99.4, 99.45, 99.4, 99.5, 100.3, 100.4]
        vols = [1000] * (len(closes) - 2) + [3000, 3000]
        bars = mk(closes, vols)
        self.assertEqual(detect("X", bars), [])
        # Same bars with the chop filter disabled DO fire — proves the filter did it.
        loose = Params(chop_max_crosses=10_000)
        self.assertEqual(kinds(detect("X", bars, loose)), ["break_up"])


class TestRealSession(unittest.TestCase):
    """SPY 2026-11-02, 1-minute bars captured from TradingView (no per-bar
    VWAP in that feed, so typical price is used)."""

    def setUp(self):
        with open(FIXTURE) as f:
            self.bars = sorted((vwap_scanner.parse_bar(r) for r in json.load(f)["SPY"]), key=lambda b: b.t)

    def test_session_vwap(self):
        vwap, _ = vwap_series(self.bars)[-1]
        self.assertAlmostEqual(vwap, 758.98, places=1)

    def test_catches_morning_bounce_and_vwap_loss(self):
        sigs = replay("SPY", self.bars)
        got = [(s.kind, datetime.fromisoformat(s.bar_time).astimezone(vwap_scanner.ET).strftime("%H:%M"))
               for s in sigs]
        self.assertIn(("bounce_up", "10:11"), got)
        self.assertIn(("break_down", "10:58"), got)
        self.assertLessEqual(len(sigs), 4, f"too noisy: {got}")


class TestScannerPlumbing(unittest.TestCase):
    def test_incomplete_bar_dropped(self):
        raw = [{"t": "2026-11-02T14:40:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
               {"t": "2026-11-02T14:41:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]
        now = datetime(2026, 11, 2, 14, 42, 1, tzinfo=timezone.utc)  # 14:41 bar not yet final (grace 3s)
        open_, close = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc), datetime(2026, 11, 2, 21, tzinfo=timezone.utc)
        bars = vwap_scanner.completed_session_bars(raw, now, open_, close)
        self.assertEqual([b.t.minute for b in bars], [40])

    def test_pagination_extends_not_overwrites(self):
        pages = [
            {"bars": {"AAA": [{"t": "2026-11-02T14:30:00Z"}]}, "next_page_token": "p2"},
            {"bars": {"AAA": [{"t": "2026-11-02T14:31:00Z"}], "BBB": [{"t": "2026-11-02T14:30:00Z"}]}},
        ]
        orig = vwap_scanner._get_json
        vwap_scanner._get_json = lambda url, headers, timeout=15: pages.pop(0)
        try:
            now = datetime(2026, 11, 2, 10, 0, tzinfo=vwap_scanner.ET)
            out = vwap_scanner.fetch_session_bars(["AAA", "BBB"], {}, "iex", now)
        finally:
            vwap_scanner._get_json = orig
        self.assertEqual(len(out["AAA"]), 2)
        self.assertEqual(len(out["BBB"]), 1)

    def test_premarket_makes_no_request(self):
        orig = vwap_scanner._get_json
        vwap_scanner._get_json = lambda *a, **k: self.fail("should not call API pre-market")
        try:
            now = datetime(2026, 11, 2, 9, 0, tzinfo=vwap_scanner.ET)
            self.assertEqual(vwap_scanner.fetch_session_bars(["AAA"], {}, "iex", now), {"AAA": []})
        finally:
            vwap_scanner._get_json = orig


if __name__ == "__main__":
    unittest.main()
