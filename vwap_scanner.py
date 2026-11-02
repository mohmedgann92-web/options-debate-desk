"""
VWAP bounce / break-through scanner with phone notifications.

Every minute during regular trading hours it pulls today's 1-minute bars
for your tickers from Alpaca's market-data API (one batched request), rebuilds
each ticker's session VWAP, and runs the setups in vwap_signals.py on every
newly COMPLETED bar. When one fires you get a notification with entry, stop,
targets and the reason it fired.

Setups (both directions — see vwap_signals.py for the exact rules):
  break_up / break_down     VWAP reclaim / VWAP loss on volume
  bounce_up / bounce_down   pullback to VWAP that holds and reverses

Notifications (use any combination; console is always on):
  ntfy      free push to your phone, no account needed. Install the ntfy app
            (iOS/Android), subscribe to a hard-to-guess topic name, and set
            NTFY_TOPIC=that-name in .env.  Anyone who knows the topic name can
            read it — treat the name like a password.
  telegram  set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

DATA FEED: Alpaca's free plan gives real-time bars from the IEX exchange only
(feed=iex) — a few percent of total US volume. Price levels track the market
closely, but the VWAP is built from IEX volume, so it can sit a few cents away
from the consolidated VWAP your charting platform shows, and the relative-
volume filter sees IEX volume only. With a paid Alpaca data plan use
--feed sip for the full consolidated tape.

Usage:
    python vwap_scanner.py                                    # default day-trade list, every minute
    python vwap_scanner.py --symbols SPY,QQQ,NVDA,TSLA --notify ntfy
    python vwap_scanner.py --notify ntfy,telegram --cooldown 20
    python vwap_scanner.py --once                             # one pass, then exit (good for testing)
    python vwap_scanner.py --save-bars today.json --once      # also dump today's bars for replay
    python vwap_scanner.py --replay today.json                # backtest the rules on saved bars, no network
    python vwap_scanner.py --test-notify --notify ntfy        # send one test notification and exit

Alerts are appended to vwap_signals.jsonl so you can review them later.
Signals are reference levels for YOUR decision, not trade instructions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vwap_signals import Bar, Params, Signal, detect, replay

ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
LOG_FILE = "vwap_signals.jsonl"

DEFAULT_SYMBOLS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL"]

EMOJI = {"break_up": "🟢⬆️", "break_down": "🔴⬇️", "bounce_up": "🟢↩️", "bounce_down": "🔴↩️"}
LABEL = {
    "break_up": "VWAP RECLAIM (break up)",
    "break_down": "VWAP LOSS (break down)",
    "bounce_up": "VWAP BOUNCE (support held)",
    "bounce_down": "VWAP REJECT (resistance held)",
}


# ---------------------------------------------------------------------------
# Credentials / config
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load .env if python-dotenv is installed (it is, via requirements.txt).
    Deliberately does not import config.py, so this script stays usable on
    its own without the LLM provider setup."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def alpaca_headers() -> dict:
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (put them in .env).")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}


def _get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def session_bounds(now_et: datetime) -> tuple[datetime, datetime]:
    open_ = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_, close


def market_is_open(headers: dict) -> bool:
    """Ask Alpaca's clock (knows holidays and half-days); fall back to a
    plain weekday 9:30–16:00 ET check if the call fails."""
    base = "https://paper-api.alpaca.markets" if os.getenv("ALPACA_PAPER", "true").lower() == "true" \
        else "https://api.alpaca.markets"
    try:
        return bool(_get_json(f"{base}/v2/clock", headers).get("is_open"))
    except Exception as exc:
        print(f"  (clock check failed: {exc} — falling back to local hours check)")
        now = datetime.now(ET)
        o, c = session_bounds(now)
        return now.weekday() < 5 and o <= now < c


def parse_bar(raw: dict) -> Bar:
    t = datetime.fromisoformat(raw["t"].replace("Z", "+00:00"))
    return Bar(t=t, o=float(raw["o"]), h=float(raw["h"]), l=float(raw["l"]),
               c=float(raw["c"]), v=float(raw["v"]),
               vw=float(raw["vw"]) if raw.get("vw") is not None else None)


def fetch_session_bars(symbols: list[str], headers: dict, feed: str, now_et: datetime) -> dict[str, list[dict]]:
    """All regular-session 1-minute bars so far today, raw Alpaca dicts,
    keyed by symbol. Follows pagination — and EXTENDS each symbol's list
    across pages, because one symbol can span a page boundary."""
    open_et, close_et = session_bounds(now_et)
    if now_et <= open_et:
        return {s: [] for s in symbols}   # pre-market: no session bars yet
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Min",
        "start": open_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": min(now_et, close_et).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000,
        "adjustment": "raw",
        "feed": feed,
        "sort": "asc",
    }
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    token = None
    for _ in range(50):  # hard stop against a pagination loop
        if token:
            params["page_token"] = token
        data = _get_json(f"{DATA_URL}?{urllib.parse.urlencode(params)}", headers)
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
    return out


def completed_session_bars(raw: list[dict], now_utc: datetime, open_utc: datetime, close_utc: datetime,
                           grace_s: int = 3) -> list[Bar]:
    """Regular-session bars whose minute has fully closed. The bar stamped
    10:31 covers 10:31:00–10:31:59, so it is complete only after 10:32:00
    (+ a few seconds of grace for late prints)."""
    bars = sorted((parse_bar(r) for r in raw), key=lambda b: b.t)
    return [b for b in bars
            if open_utc <= b.t < close_utc
            and b.t + timedelta(seconds=60 + grace_s) <= now_utc]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def format_signal(s: Signal) -> tuple[str, str]:
    t = datetime.fromisoformat(s.bar_time).astimezone(ET).strftime("%H:%M")
    title = f"{EMOJI.get(s.kind, '')} {s.symbol} {LABEL.get(s.kind, s.kind)} @ {s.price:.2f}"
    tgt = " / ".join(f"{x:.2f}" for x in s.targets)
    band = f"  ·  next σ band {s.band_target:.2f}" if s.band_target else ""
    body = (
        f"{s.direction.upper()} idea · bar {t} ET\n"
        f"Price {s.price:.2f} vs VWAP {s.vwap:.2f} ({s.distance_pct:+.2f}%)\n"
        f"Stop {s.stop:.2f} (risk {s.risk_per_share:.2f}/sh) · Targets 1R/2R {tgt}{band}\n"
        f"Rel vol {s.rel_volume:.1f}x · {s.reason}"
    )
    return title, body


def notify_console(title: str, body: str) -> None:
    print("\a" + "=" * 72)          # \a = terminal bell
    print(title)
    print(body)
    print("=" * 72, flush=True)


def notify_ntfy(title: str, body: str, kind: str) -> None:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        print("  (ntfy skipped: NTFY_TOPIC not set)")
        return
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    # HTTP headers must be latin-1, so the emoji title goes in via ntfy's
    # tags (rendered as emoji by the app) and the title stays ASCII.
    ascii_title = title.encode("ascii", "ignore").decode().strip()
    tags = "chart_with_upwards_trend" if kind.endswith("_up") else "chart_with_downwards_trend"
    req = urllib.request.Request(
        f"{server}/{urllib.parse.quote(topic)}", data=body.encode("utf-8"), method="POST",
        headers={"Title": ascii_title, "Priority": "high", "Tags": tags},
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def notify_telegram(title: str, body: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("  (telegram skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return
    payload = json.dumps({"chat_id": chat, "text": f"{title}\n{body}"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        pass


def send(signal: Signal, channels: set[str], log: bool = True) -> None:
    title, body = format_signal(signal)
    notify_console(title, body)
    for name, fn in (("ntfy", lambda: notify_ntfy(title, body, signal.kind)),
                     ("telegram", lambda: notify_telegram(title, body))):
        if name in channels:
            try:
                fn()
            except Exception as exc:  # a dead notifier must never kill the scanner
                print(f"  ({name} notification failed: {exc})")
    if not log:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"logged_at": datetime.now(timezone.utc).isoformat(), **signal.to_dict()}) + "\n")
    except OSError as exc:
        print(f"  (could not write {LOG_FILE}: {exc})")


# ---------------------------------------------------------------------------
# Live loop
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, symbols, feed, channels, cooldown_min, params: Params):
        self.symbols = symbols
        self.feed = feed
        self.channels = channels
        self.cooldown = timedelta(minutes=cooldown_min)
        self.params = params
        self.last_eval: dict[str, datetime] = {}     # last bar time evaluated, per symbol
        self.last_fired: dict[tuple, datetime] = {}  # (symbol, kind) -> bar time
        self.session_date = None

    def _reset_if_new_day(self, now_et: datetime) -> None:
        if self.session_date != now_et.date():
            self.session_date = now_et.date()
            self.last_eval.clear()
            self.last_fired.clear()

    def cycle(self, headers: dict, save_path: str | None = None) -> int:
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)
        self._reset_if_new_day(now_et)
        open_et, close_et = session_bounds(now_et)
        raw = fetch_session_bars(self.symbols, headers, self.feed, now_et)
        if save_path:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            print(f"  saved bars to {save_path}")

        fired = 0
        for sym in self.symbols:
            bars = completed_session_bars(raw.get(sym, []), now_utc,
                                          open_et.astimezone(timezone.utc), close_et.astimezone(timezone.utc))
            if not bars:
                continue
            # First time we see a symbol today: evaluate only the latest bar,
            # don't spam alerts for the whole morning on startup.
            prev = self.last_eval.get(sym, bars[-1].t - timedelta(seconds=1))
            for i, b in enumerate(bars):
                if b.t <= prev:
                    continue
                for s in detect(sym, bars[:i + 1], self.params):
                    key = (sym, s.kind)
                    last = self.last_fired.get(key)
                    if last and b.t - last < self.cooldown:
                        continue
                    self.last_fired[key] = b.t
                    send(s, self.channels)
                    fired += 1
            self.last_eval[sym] = bars[-1].t
        return fired

    def status_line(self) -> str:
        return f"{datetime.now(ET):%H:%M:%S} ET · watching {len(self.symbols)} · feed={self.feed}"


def run_live(args, params: Params) -> None:
    headers = alpaca_headers()
    channels = set(filter(None, args.notify.split(",")))
    scanner = Scanner(args.symbols, args.feed, channels, args.cooldown, params)
    print(f"VWAP scanner · {', '.join(args.symbols)} · feed={args.feed} · notify=console"
          f"{'+' + '+'.join(sorted(channels)) if channels else ''} · cooldown {args.cooldown}m")
    if args.feed == "iex":
        print("Note: free IEX feed — VWAP uses IEX volume only and can differ slightly from your chart's.")

    while True:
        try:
            if args.ignore_market_hours or market_is_open(headers):
                n = scanner.cycle(headers, args.save_bars)
                print(f"{scanner.status_line()} · {n} new signal(s)", flush=True)
            else:
                print(f"{datetime.now(ET):%H:%M:%S} ET · market closed — waiting", flush=True)
                if args.once:
                    return
                time.sleep(300)
                continue
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            print(f"HTTP {exc.code} from Alpaca: {detail}")
            if exc.code in (401, 403):
                if "sip" in detail.lower() or args.feed == "sip":
                    print("Your plan may not include SIP data — try --feed iex.")
                sys.exit(1)
        except Exception as exc:
            print(f"cycle error (will retry): {exc!r}")

        if args.once:
            return
        # Wake ~5 s after each minute boundary, when the previous minute's bar is final.
        now = time.time()
        time.sleep(max(1.0, 60 - (now % 60) + 5))


def run_replay(path: str, symbols: list[str] | None, params: Params) -> None:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    total = 0
    for sym, rows in raw.items():
        if symbols and sym not in symbols:
            continue
        bars = sorted((parse_bar(r) for r in rows), key=lambda b: b.t)
        if bars:  # keep the regular session only
            d = bars[0].t.astimezone(ET)
            o, c = session_bounds(d)
            bars = [b for b in bars if o <= b.t.astimezone(ET) < c]
        sigs = replay(sym, bars, params)
        total += len(sigs)
        print(f"\n{sym}: {len(bars)} bars, {len(sigs)} signal(s)")
        for s in sigs:
            title, body = format_signal(s)
            print(f"  {title}\n    " + body.replace("\n", "\n    "))
    print(f"\n{total} signal(s) total (cooldown not applied in replay).")


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="VWAP bounce / break-through scanner with notifications")
    ap.add_argument("--symbols", default=None,
                    help=f"comma-separated tickers (default: {','.join(DEFAULT_SYMBOLS)}; "
                         "with --replay, default = every ticker in the file)")
    ap.add_argument("--feed", default=os.getenv("ALPACA_DATA_FEED", "iex"), choices=["iex", "sip"],
                    help="iex = free real-time single exchange; sip = full tape (paid plan)")
    ap.add_argument("--notify", default="", help="extra channels: ntfy, telegram (comma-separated)")
    ap.add_argument("--cooldown", type=int, default=15, help="minutes before the same ticker+setup can alert again")
    ap.add_argument("--rel-vol", type=float, default=None, help="override min relative volume for breaks (default 1.5)")
    ap.add_argument("--confirm-bars", type=int, default=None, help="closes beyond VWAP needed to confirm a break (default 2)")
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    ap.add_argument("--ignore-market-hours", action="store_true")
    ap.add_argument("--save-bars", metavar="FILE", help="write today's raw bars to FILE (for --replay)")
    ap.add_argument("--replay", metavar="FILE", help="backtest the rules on saved bars, no live data")
    ap.add_argument("--test-notify", action="store_true", help="send one fake alert through the chosen channels")
    args = ap.parse_args()
    explicit = args.symbols is not None
    args.symbols = [s.strip().upper() for s in (args.symbols or ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]

    params = Params()
    if args.rel_vol is not None:
        params.break_rel_vol = args.rel_vol
    if args.confirm_bars is not None:
        params.confirm_bars = max(1, args.confirm_bars)

    if args.test_notify:
        demo = Signal(symbol="TEST", kind="break_up", direction="long",
                      bar_time=datetime.now(timezone.utc).isoformat(), price=100.0, vwap=99.8, sd=0.4,
                      distance_pct=0.2, rel_volume=2.0, buffer=0.05, stop=99.7, risk_per_share=0.3,
                      targets=[100.3, 100.6], band_target=100.2, reason="test notification — not a real signal")
        send(demo, set(filter(None, args.notify.split(","))), log=False)
        return
    if args.replay:
        run_replay(args.replay, args.symbols if explicit else None, params)
        return
    try:
        run_live(args, params)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
