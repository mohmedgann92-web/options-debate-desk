# VWAP scanner: hourly watchdog

Do exactly this, nothing else. Work in /home/user/options-debate-desk.

1. Run `python3 tv_vwap_scanner.py health --interval 5`.
2. If it prints `CLOSED` or starts with `OK`, reply with that line and stop.
3. If it starts with `STALE`, the tick chain broke. Restart it: call send_later with
   `delay_minutes` = 1 and message
   `VWAP tick: follow /home/user/options-debate-desk/scanner_prompts/tick.md exactly.`
   (The tick's duplicate guard makes this safe even if the chain was only late.)
   Then call PushNotification with status "proactive" and the message
   "VWAP scanner chain had stalled (<health line>); restarted."
   Reply with one line saying what you did.
