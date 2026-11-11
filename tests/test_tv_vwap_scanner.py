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


class TestTickChain(Base):
    """claim/health drive the durable reminder chain that replaced in-session
    cron jobs (those vanished whenever the cloud container was recycled)."""

    def at(self, hhmmss, day="2026-11-03"):        # times in ET (EST = UTC-5)
        h, m, s = map(int, hhmmss.split(":"))
        return datetime.fromisoformat(f"{day}T{h + 5:02d}:{m:02d}:{s:02d}+00:00")

    def test_first_tick_schedules_next_slot(self):
        self.assertEqual(self.m.cmd_claim(self.at("09:31:20"), 5), "NEXT 2026-11-03T14:36:00Z")

    def test_late_tick_skips_to_following_slot(self):
        # Woken 40s before a slot: too close, schedule the one after.
        self.assertEqual(self.m.cmd_claim(self.at("09:35:20"), 5), "NEXT 2026-11-03T14:41:00Z")

    def test_duplicate_guard(self):
        self.m.cmd_claim(self.at("10:01:05"), 5)
        self.assertTrue(self.m.cmd_claim(self.at("10:02:00"), 5).startswith("DUPLICATE"))
        self.assertTrue(self.m.cmd_claim(self.at("10:06:05"), 5).startswith("NEXT"))

    def test_last_tick_schedules_end(self):
        self.assertEqual(self.m.cmd_claim(self.at("15:56:10"), 5), "END 2026-11-03T21:05:00Z")

    def test_early_close_ends_at_1pm(self):
        out = self.m.cmd_claim(self.at("12:56:10", day="2026-11-27"), 5)
        self.assertEqual(out, "END 2026-11-27T18:05:00Z")

    def test_closed_outside_session_and_holidays(self):
        self.assertEqual(self.m.cmd_claim(self.at("09:10:00"), 5), "CLOSED")
        self.assertEqual(self.m.cmd_claim(self.at("10:00:00", day="2026-11-26"), 5), "CLOSED")

    def test_health(self):
        self.assertEqual(self.m.cmd_health(self.at("10:00:00"), 5), "STALE never")
        self.m.cmd_claim(self.at("10:01:05"), 5)
        self.assertTrue(self.m.cmd_health(self.at("10:05:00"), 5).startswith("OK"))
        self.assertTrue(self.m.cmd_health(self.at("10:20:00"), 5).startswith("STALE"))
        self.assertEqual(self.m.cmd_health(self.at("17:00:00"), 5), "CLOSED")

    def test_prompt_files_exist_and_reference_each_other(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tick = read(os.path.join(root, "scanner_prompts", "tick.md"))
        self.assertIn("scanner_prompts/tick.md", tick)
        self.assertIn("scanner_prompts/end.md", tick)
        self.assertIn("claim --interval 5", tick)
        self.assertIn("health --interval 5", read(os.path.join(root, "scanner_prompts", "watchdog.md")))


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


class TestIndexFilter(Base):
    """Longs only with SPY and QQQ above VWAP, shorts only with both below."""

    def sig(self, direction):
        from vwap_signals import Signal
        return Signal(symbol="X", kind="break_up" if direction == "long" else "break_down",
                      direction=direction, bar_time="2026-11-05T15:00:00+00:00", price=10, vwap=10,
                      sd=0.1, distance_pct=0, rel_volume=2, buffer=0.1, stop=9, risk_per_share=1,
                      targets=[11, 12])

    def test_rules(self):
        up, down, mixed = {"SPY": 1, "QQQ": 1}, {"SPY": -1, "QQQ": -1}, {"SPY": 1, "QQQ": -1}
        allows = lambda sym, d, side: self.m.index_allows(sym, self.sig(d), side)[0]
        self.assertTrue(allows("NASDAQ:INTC", "long", up))
        self.assertFalse(allows("NASDAQ:INTC", "long", down))
        self.assertTrue(allows("NASDAQ:INTC", "short", down))
        self.assertFalse(allows("NASDAQ:INTC", "short", up))
        self.assertFalse(allows("NASDAQ:INTC", "long", mixed))
        self.assertFalse(allows("NASDAQ:INTC", "short", mixed))
        # SQQQ is inverse: a SQQQ long is a market short.
        self.assertTrue(allows("NASDAQ:SQQQ", "long", down))
        self.assertFalse(allows("NASDAQ:SQQQ", "long", up))
        # Filter stays off until both indexes have data.
        self.assertTrue(allows("NASDAQ:INTC", "long", {"SPY": -1}))
        why = self.m.index_allows("NASDAQ:INTC", self.sig("long"), down)[1]
        self.assertEqual(why, "SPY below, QQQ below VWAP")

    def test_tick_blocks_and_journal_lists_it(self):
        # Replay the SPY fixture as INTC alongside a SPY/QQQ that sit flat
        # BELOW their VWAP: INTC's long bounce must be blocked, its short kept.
        with open(FIXTURE) as f:
            rows = json.load(f)["SPY"]
        cum, hi, lo = 0.0, -1e9, 1e9
        p = self.m.params_for(5)
        out, k = [], 0
        for r in rows:
            cum += r["v"]; hi = max(hi, r["h"]); lo = min(lo, r["l"])
            now = datetime.fromisoformat(r["t"].replace("Z", "+00:00")) + timedelta(seconds=65)
            if now.minute % 5 == 1:
                k += 1
                idx = 100 if k == 1 else 99          # opens at 100 then sits below VWAP
                snap = {"NASDAQ:INTC": {"close": r["c"], "volume": cum, "high": hi, "low": lo},
                        "AMEX:SPY": {"close": idx, "volume": 1000 * k, "high": 100, "low": 99},
                        "NASDAQ:QQQ": {"close": idx, "volume": 1000 * k, "high": 100, "low": 99}}
                out += self.m.cmd_tick({"data": snap}, now, p, 5)
        text = "\n".join(out)
        self.assertIn("BLOCKED INTC VWAP BOUNCE (long): SPY below, QQQ below VWAP", text)
        self.assertIn("ALERT ▼ INTC VWAP LOSS", text)
        self.assertNotIn("ALERT ▲ INTC", text)
        md_path, csv_path = self.m.cmd_journal(now, os.path.join(self.tmp.name, "j"))
        md = read(md_path)
        self.assertIn("## Blocked by index filter", md)
        self.assertIn("| 10:16 | INTC | VWAP BOUNCE | long |", md)
        self.assertEqual(sum(l.startswith("2026-11-02,") for l in read(csv_path).splitlines()), 1)


class TestOneCallHelpers(Base):
    def at(self, hhmm, day="2026-11-03"):
        h, m = map(int, hhmm.split(":"))
        return datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:10-05:00")

    def spy(self):
        return {"AMEX:SPY": {"close": 100, "volume": 1000, "high": 101, "low": 99}}

    def test_tick_claimed_skips_when_closed_or_duplicate(self):
        p = self.m.params_for(5)
        self.assertEqual(self.m.cmd_tick_claimed({"data": self.spy()}, self.at("08:00"), p, 5), ["CLOSED"])
        out = self.m.cmd_tick_claimed({"data": self.spy()}, self.at("10:01"), p, 5)
        self.assertTrue(out[-1].startswith("STATUS 10:01"))
        out = self.m.cmd_tick_claimed({"data": self.spy()}, self.at("10:02"), p, 5)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("DUPLICATE"))

    def test_eod_no_session_on_holiday(self):
        out = self.m.cmd_eod(self.at("16:06", day="2026-11-26"), 5, os.path.join(self.tmp.name, "j"))
        self.assertTrue(out.startswith("NO_SESSION"))

    def test_block_matches_files_exactly(self):
        with open(FIXTURE) as f:
            rows = json.load(f)["SPY"]
        cum, hi, lo, now = 0.0, -1e9, 1e9, None
        p = self.m.params_for(5)
        for r in rows:
            cum += r["v"]; hi = max(hi, r["h"]); lo = min(lo, r["l"])
            now = datetime.fromisoformat(r["t"].replace("Z", "+00:00")) + timedelta(seconds=65)
            if now.minute % 5 == 1:
                self.m.cmd_tick({"data": {"AMEX:SPY": {"close": r["c"], "volume": cum, "high": hi, "low": lo}}}, now, p, 5)
        out_dir = os.path.join(self.tmp.name, "j")
        eod = self.m.cmd_eod(now, 5, out_dir)
        first, block = eod.split("\n", 1)
        self.assertTrue(first.startswith("PUSH VWAP scanner 2026-11-02 done: 2 alert(s)"))
        md = read(os.path.join(out_dir, "2026-11-02.md")).rstrip("\n")
        csv_rows = [l for l in read(os.path.join(out_dir, "alerts.csv")).splitlines() if l.startswith("2026-11-02,")]
        expect = "\n".join(["=== VWAP JOURNAL 2026-11-02 PUSHED=no ===", md, "=== VWAP CSV ROWS ===", *csv_rows, "=== VWAP END ==="])
        self.assertEqual(block, expect)
        self.assertEqual(len(csv_rows), 2)


class TestBadVolume(Base):
    def test_inflated_first_volume_self_heals(self):
        # First snapshot has an extra digit in volume (10x). Before the fix the
        # ticker never produced a bar again; now it re-anchors on the next tick.
        p = self.m.params_for(5)
        T = datetime(2026, 11, 3, 14, 31, 10, tzinfo=timezone.utc)
        snap = lambda c, v: {"data": {"AMEX:SPY": {"close": c, "volume": v, "high": c + 1, "low": c - 1}}}
        self.m.cmd_tick(snap(100, 10_000_000), T, p, 5)              # bad: should be 1,000,000
        out = self.m.cmd_tick(snap(100.5, 1_200_000), T + timedelta(minutes=5), p, 5)
        self.assertTrue(any(l.startswith("WARN SPY: volume went down") for l in out))
        self.m.cmd_tick(snap(101, 1_500_000), T + timedelta(minutes=10), p, 5)
        st = self.m.load_state()["symbols"]["AMEX:SPY"]
        self.assertEqual(len(st["bars"]), 1)
        self.assertLessEqual(st["seed"][1], 1_200_000)


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
