# VWAP scanner: end of day

Work in /home/user/options-debate-desk.

1. Run `python3 tv_vwap_scanner.py summary`.
2. Run `python3 tv_vwap_scanner.py journal --ticks-expected "$(python3 tv_vwap_scanner.py plan --interval 5 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ticks_expected", 0))')"`.
3. Try: `git add vwap_journal && git commit -m "VWAP journal <date>" && git pull --rebase origin claude/vwap-scanner-tradingview && git push origin claude/vwap-scanner-tradingview`.
   A push failure is expected and fine; do not retry more than once.
4. Delete today's watchdog: read its id from `.vwap_state/watchdog_id` and call delete_trigger with it (skip if the file is missing).
5. Call PushNotification with status "proactive" and the summary line as the message.
6. Your FINAL reply must be exactly this, with nothing before or after (another session parses it to save the journal):

```
=== VWAP JOURNAL <date> PUSHED=<yes|no> ===
(full contents of vwap_journal/<date>.md)
=== VWAP CSV ROWS ===
(output of: grep "^<date>," vwap_journal/alerts.csv, may be empty)
=== VWAP END ===
```
