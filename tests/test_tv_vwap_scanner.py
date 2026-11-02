"""
Offline tests for tv_vwap_scanner.py (snapshot-driven scanner).

    python -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "spy_2026-11-02_tv.json")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["VWAP_STATE_DIR"] = self.tmp.name
        import tv_vwap_scanner
        self.m = importlib.reload(tv_vwap_scanner)   # pick up the temp state dir
        from vwap_signals import Params
        self.P = Params

    def tearDown(self):
        os.environ.pop("VWAP_STATE_DIR", None)
        self.tmp.cleanup()

    def tick(self, snap, now):
        return self.m.cmd_tick({"data": snap}, now, self.P())


class TestSnapshotReplay(Base):
    def test_spy_session_via_snapshots(self):
        """Rebuild what the live snapshots would have shown on 2026-11-02 and
        check the scanner fires the 10:58 VWAP loss and ends at the right VWAP."""
        with open(FIXTURE) as f:
            rows = json.load(f)["SPY"]
        cum, hi, lo, alerts = 0.0, -1e9, 1e9, []
        for r in rows:
            cum += r["v"]; hi = max(hi, r["h"]); lo = min(lo, r["l"])
            now = datetime.fromisoformat(r["t"].replace("Z", "+00:00")) + timedelta(seconds=65)
            out = self.tick({"AMEX:SPY": {"close": r["c"], "volume": cum, "high": hi, "low": lo}}, now)
            alerts += [l for l in out if l.startswith("ALERT")]
        self.assertTrue(any("SPY VWAP LOSS" in a and "10:59 ET" in a for a in alerts), alerts)
        self.assertLessEqual(len(alerts), 3, alerts)
        status = "\n".join(self.m.cmd_status(now))
        self.assertIn("VWAP    758.98", status)


class TestTickMechanics(Base):
    T = datetime(2026, 11, 3, 15, 0, 5, tzinfo=timezone.utc)   # 10:00:05 ET, a Tuesday

    def snap(self, c, v, h=None, l=None):
        return {"NASDAQ:XYZ": {"close": c, "volume": v, "high": h or c + 1, "low": l or c - 1}}

    def test_market_closed_does_nothing(self):
        out = self.tick(self.snap(100, 1000), datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc))
        self.assertTrue(out[0].startswith("STATUS market closed"))
        self.assertFalse(os.path.exists(self.m.STATE_FILE))

    def test_weekend_closed(self):
        out = self.tick(self.snap(100, 1000), datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc))
        self.assertIn("market closed", out[0])

    def test_first_tick_initialises_then_builds_bar(self):
        self.tick(self.snap(100, 1000), self.T)
        self.tick(self.snap(101, 1600, h=101.5), self.T + timedelta(minutes=1))
        st = self.m.load_state()["symbols"]["NASDAQ:XYZ"]
        self.assertEqual(len(st["bars"]), 1)
        b = st["bars"][0]
        self.assertEqual((b["o"], b["c"], b["v"]), (100, 101, 600))
        self.assertEqual(b["h"], 101.5)   # new day high attributed to the bar

    def test_no_volume_change_makes_no_bar(self):
        self.tick(self.snap(100, 1000), self.T)
        self.tick(self.snap(100.5, 1000), self.T + timedelta(minutes=1))
        self.assertEqual(self.m.load_state()["symbols"]["NASDAQ:XYZ"]["bars"], [])

    def test_late_tick_is_split_per_minute(self):
        self.tick(self.snap(100, 1000), self.T)
        self.tick(self.snap(103, 1300), self.T + timedelta(minutes=3))
        bars = self.m.load_state()["symbols"]["NASDAQ:XYZ"]["bars"]
        self.assertEqual(len(bars), 3)
        self.assertEqual([round(b["v"]) for b in bars], [100, 100, 100])
        self.assertEqual([b["c"] for b in bars], [101, 102, 103])

    def test_new_day_resets(self):
        self.tick(self.snap(100, 1000), self.T)
        self.tick(self.snap(101, 2000), self.T + timedelta(minutes=1))
        self.tick(self.snap(50, 10), self.T + timedelta(days=1))
        st = self.m.load_state()
        self.assertEqual(st["day"], "2026-11-04")
        self.assertEqual(st["symbols"]["NASDAQ:XYZ"]["bars"], [])
        self.assertEqual(st["symbols"]["NASDAQ:XYZ"]["last_v"], 10)

    def test_incomplete_quote_warns(self):
        out = self.tick({"NASDAQ:BAD": {"close": None, "volume": 5}}, self.T)
        self.assertTrue(any(l.startswith("WARN BAD") for l in out))


class TestSeed(Base):
    def test_seed_sets_vwap_for_midday_start(self):
        now = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)        # 12:00 ET
        open_ts = int(datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc).timestamp())
        payload = {"bars": {"t": [open_ts, open_ts + 300], "o": [10, 12], "h": [10, 12],
                            "l": [10, 12], "c": [10, 12], "v": [100, 300]}}
        msg = self.m.cmd_seed("XYZ", payload, now)
        self.assertIn("VWAP 11.50", msg)
        # First live tick adopts the seed, scaled to the consolidated volume.
        self.tick({"NASDAQ:XYZ": {"close": 20, "volume": 40000, "high": 21, "low": 9}}, now)
        status = "\n".join(self.m.cmd_status(now))
        self.assertIn("VWAP     11.50", status)
        self.assertIn("seed=5m-bars", status)

    def test_seed_ignores_premarket_bars(self):
        now = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
        pre = int(datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc).timestamp())     # 08:00 ET
        rth = int(datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc).timestamp())
        payload = {"bars": [{"t": pre, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1e6},
                            {"t": rth, "o": 5, "h": 5, "l": 5, "c": 5, "v": 10}]}
        self.assertIn("VWAP 5.00", self.m.cmd_seed("XYZ", payload, now))


if __name__ == "__main__":
    unittest.main()
