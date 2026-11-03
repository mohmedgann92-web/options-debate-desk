# VWAP scanner: one tick

Do exactly this, nothing else (no code edits, no commits, no commentary).
Work in /home/user/options-debate-desk.

1. Run `python3 tv_vwap_scanner.py claim --interval 5` and read its single output line.
   - `CLOSED` or `DUPLICATE ...`: reply with that line and stop. Do nothing else.
   - `NEXT <time>`: call send_later with `at` = that time and message
     `VWAP tick: follow /home/user/options-debate-desk/scanner_prompts/tick.md exactly.`
   - `END <time>`: call send_later with `at` = that time and message
     `VWAP end of day: follow /home/user/options-debate-desk/scanner_prompts/end.md exactly.`
   Schedule this BEFORE step 2, so a failure below can never break the chain.
2. Call `mcp__Trading_View__mcp-tv-get-symbol-data-batch` with symbols
   `["AMEX:SPY","NASDAQ:QQQ","NASDAQ:INTC","NASDAQ:SNDK","NASDAQ:COIN","NASDAQ:NVDA","NYSE:ORCL","NASDAQ:SQQQ","NASDAQ:TQQQ","AMEX:SOXL"]`
   and columns `["close","volume","high","low"]`. If it fails, reply "TradingView fetch failed: <reason>" and stop.
3. Pipe the full JSON result into: `python3 tv_vwap_scanner.py tick --interval 5 <<'JSON'` ... `JSON`
4. For EACH output line starting with `ALERT `, call PushNotification with status "proactive"
   and the rest of the line as the message.
5. Reply with only the script's output lines.
