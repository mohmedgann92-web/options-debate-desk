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


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


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


class TestFiveMinuteMode(Base):
    def test_spy_session_at_5min_ticks(self):
        """Same session sampled every 5 minutes (as the scheduled task runs):
        catches the morning bounce and the VWAP loss, and nothing else."""
        with open(FIXTURE) as f:
            rows = json.load(f)["SPY"]
        cum, hi, lo, alerts = 0.0, -1e9, 1e9, []
        p = self.m.params_for(5)
        for r in rows:
            cum += r["v"]; hi = max(hi, r["h"]); lo = min(lo, r["l"])
            now = datetime.fromisoformat(r["t"].replace("Z", "+00:00")) + timedelta(seconds=65)
            if now.minute % 5 != 1:
                continue
            out = self.m.cmd_tick({"data": {"AMEX:SPY": {"close": r["c"], "volume": cum, "high": hi, "low": lo}}},
                                  now, p, 5)
            alerts += [l for l in out if l.startswith("ALERT")]
        self.assertEqual(len(alerts), 2, alerts)
        self.assertIn("VWAP BOUNCE", alerts[0]); self.assertIn("10:16 ET", alerts[0])
        self.assertIn("VWAP LOSS", alerts[1]); self.assertIn("11:01 ET", alerts[1])

    def test_5min_ticks_make_one_bar_each(self):
        T = datetime(2026, 11, 3, 15, 0, 5, tzinfo=timezone.utc)
        p = self.m.params_for(5)
        snap = lambda c, v: {"data": {"NASDAQ:XYZ": {"close": c, "volume": v, "high": 200, "low": 1}}}
        self.m.cmd_tick(snap(100, 1000), T, p, 5)
        self.m.cmd_tick(snap(101, 2000), T + timedelta(minutes=5), p, 5)
        self.m.cmd_tick(snap(103, 4000), T + timedelta(minutes=15), p, 5)   # one tick missed
        bars = self.m.load_state()["symbols"]["NASDAQ:XYZ"]["bars"]
        self.assertEqual([round(b["v"]) for b in bars], [1000, 1000, 1000])

    def test_unsupported_interval_rejected(self):
        with self.assertRaises(SystemExit):
            self.m.params_for(3)


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


class TestCalendarAndPlan(Base):
    def at(self, iso):
        return datetime.fromisoformat(iso)

    def test_holiday_and_weekend_not_trading(self):
        self.assertEqual(self.m.cmd_plan(self.at("2026-11-26T13:41:00+00:00"), 5),
                         {"date_et": "2026-11-26", "trading_day": False, "reason": "NYSE holiday"})
        self.assertFalse(self.m.cmd_plan(self.at("2026-11-07T13:41:00+00:00"), 5)["trading_day"])

    def test_tick_on_holiday_is_closed(self):
        out = self.tick({"NASDAQ:XYZ": {"close": 1, "volume": 1, "high": 1, "low": 1}},
                        self.at("2026-12-25T16:00:00+00:00"))
        self.assertIn("market closed", out[0])

    def test_regular_day_plan(self):
        p = self.m.cmd_plan(self.at("2026-11-03T13:41:00+00:00"), 5)
        self.assertEqual(p["ticks_expected"], 78)
        self.assertEqual(p["session_et"], "09:30-16:00")
        self.assertTrue(all(c.endswith(" 3 11 *") for c in p["tick_crons"]))   # pinned to the day

    def test_early_close_day(self):
        p = self.m.cmd_plan(self.at("2026-11-27T13:41:00+00:00"), 5)
        self.assertEqual(p["session_et"], "09:30-13:00")
        self.assertEqual(p["ticks_expected"], 42)
        # A tick at 1:30 PM ET is outside the early-close session.
        out = self.tick({"NASDAQ:XYZ": {"close": 1, "volume": 1, "high": 1, "low": 1}},
                        self.at("2026-11-27T18:30:00+00:00"))
        self.assertIn("market closed", out[0])

    def test_plan_tick_count_matches_expected(self):
        """Every cron minute generated must be one of the expected tick times."""
        p = self.m.cmd_plan(self.at("2026-11-03T13:41:00+00:00"), 5)
        n = 0
        for expr in p["tick_crons"]:
            mins, hours = expr.split()[:2]
            h0, h1 = (map(int, hours.split("-")) if "-" in hours else (int(hours), int(hours)))
            n += len(mins.split(",")) * (h1 - h0 + 1)
        self.assertEqual(n, p["ticks_expected"])

    def test_summary_counts_todays_alerts_and_ticks(self):
        T = self.at("2026-11-03T15:00:05+00:00")
        self.tick({"NASDAQ:XYZ": {"close": 1, "volume": 1, "high": 1, "low": 1}}, T)
        os.makedirs(self.m.STATE_DIR, exist_ok=True)
        with open(self.m.LOG_FILE, "w") as f:
            f.write(json.dumps({"symbol": "XYZ", "kind": "break_up", "bar_time": "2026-11-03T15:00:00+00:00"}) + "\n")
            f.write(json.dumps({"symbol": "OLD", "kind": "break_up", "bar_time": "2026-11-02T15:00:00+00:00"}) + "\n")
        s = self.m.cmd_summary(T)
        self.assertIn("1 alert(s), 1 ticks run", s)
        self.assertIn("XYZ VWAP RECLAIM", s)
        self.assertNotIn("OLD", s)


class TestJournal(Base):
    def _run_spy_5min(self):
        with open(FIXTURE) as f:
            rows = json.load(f)["SPY"]
        cum, hi, lo = 0.0, -1e9, 1e9
        p = self.m.params_for(5)
        for r in rows:
            cum += r["v"]; hi = max(hi, r["h"]); lo = min(lo, r["l"])
            now = datetime.fromisoformat(r["t"].replace("Z", "+00:00")) + timedelta(seconds=65)
            if now.minute % 5 == 1:
                self.m.cmd_tick({"data": {"AMEX:SPY": {"close": r["c"], "volume": cum, "high": hi, "low": lo}}},
                                now, p, 5)
        return now

    def test_journal_files_and_grading(self):
        now = self._run_spy_5min()
        out = os.path.join(self.tmp.name, "journal")
        md_path, csv_path = self.m.cmd_journal(now, out, ticks_expected=78)
        md = read(md_path)
        self.assertTrue(md_path.endswith("2026-11-02.md"))
        self.assertIn("of 78 planned", md)
        # Alert times match the phone push (bar close), not the bar start.
        self.assertIn("| 10:16 | SPY | VWAP BOUNCE | long |", md)
        self.assertIn("| 11:01 | SPY | VWAP LOSS | short |", md)
        self.assertIn("**2R**", md)                 # the VWAP loss ran to 2R
        self.assertIn("| SPY | 753.51 | 758.98 |", md)
        lines = read(csv_path).splitlines()
        self.assertEqual(lines[0].split(",")[1], "alert_time_et")
        self.assertEqual(len(lines), 3)

    def test_journal_rerun_same_day_does_not_duplicate_but_keeps_other_days(self):
        now = self._run_spy_5min()
        out = os.path.join(self.tmp.name, "journal")
        os.makedirs(out)
        with open(os.path.join(out, "alerts.csv"), "w") as f:
            f.write("date,alert_time_et\n2026-10-30,10:00,OLD,row\n")
        self.m.cmd_journal(now, out)
        self.m.cmd_journal(now, out)
        lines = read(os.path.join(out, "alerts.csv")).splitlines()
        self.assertEqual(sum(l.startswith("2026-11-02,") for l in lines), 2)
        self.assertIn("2026-10-30,10:00,OLD,row", lines)
        self.assertEqual(sum(l.startswith("date,") for l in lines), 1)

    def test_journal_with_no_alerts(self):
        T = datetime(2026, 11, 3, 15, 0, 5, tzinfo=timezone.utc)
        self.tick({"NASDAQ:XYZ": {"close": 10, "volume": 100, "high": 11, "low": 9}}, T)
        md_path, _ = self.m.cmd_journal(T, os.path.join(self.tmp.name, "j"))
        self.assertIn("No alerts today.", read(md_path))


class TestGrading(unittest.TestCase):
    def setUp(self):
        import tv_vwap_scanner
        from vwap_signals import Bar
        self.m, self.Bar = tv_vwap_scanner, Bar
        self.T = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
        self.alert = {"direction": "long", "price": 100.0, "stop": 99.0, "risk_per_share": 1.0,
                      "targets": [101.0, 102.0], "bar_time": self.T.isoformat()}

    def bars(self, hl):
        return [self.Bar(self.T + timedelta(minutes=5 * (i + 1)), 100, h, l, (h + l) / 2, 1) for i, (h, l) in enumerate(hl)]

    def test_two_r(self):
        g = self.m.grade_alert(self.alert, self.bars([(100.5, 99.5), (101.2, 100.2), (102.1, 101.0)]))
        self.assertEqual(g["outcome"], "2R")
        self.assertEqual(g["mfe_r"], 2.1)

    def test_stop_first(self):
        self.assertEqual(self.m.grade_alert(self.alert, self.bars([(100.4, 98.9)]))["outcome"], "stop")

    def test_one_r_then_stop(self):
        self.assertEqual(self.m.grade_alert(self.alert, self.bars([(101.3, 100.1), (100.5, 98.5)]))["outcome"],
                         "1R then stop")

    def test_same_bar_stop_and_target_counts_as_stop(self):
        self.assertEqual(self.m.grade_alert(self.alert, self.bars([(102.5, 98.5)]))["outcome"], "stop")

    def test_short_mirror_and_close_r(self):
        a = dict(self.alert, direction="short", stop=101.0, targets=[99.0, 98.0])
        g = self.m.grade_alert(a, self.bars([(100.2, 99.4), (100.0, 99.2)]))
        self.assertEqual(g["outcome"], "open")
        self.assertEqual(g["close_r"], 0.4)


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
