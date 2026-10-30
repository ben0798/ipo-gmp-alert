#!/usr/bin/env python3
"""
TEST runner for the IPO GMP alert. Uses the real code from ipo_alert.py,
but lets you override the threshold and the date rules.

Examples:
  python ipo_alert_test.py                      # 5% threshold, IPOs closing today
  python ipo_alert_test.py --min-pct 10
  python ipo_alert_test.py --date 2026-11-04    # pretend today is this date
  python ipo_alert_test.py --any-open           # any IPO not yet closed (not only today)
  python ipo_alert_test.py --send-to channel    # default is your private ADMIN chat
  python ipo_alert_test.py --dry-run            # print only, send nothing
"""
import argparse
import os
import sys
from datetime import date, datetime

import ipo_alert as m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pct", type=float,
                    default=float(os.getenv("TEST_MIN_PCT") or 5))
    ap.add_argument("--date", default=os.getenv("TEST_DATE") or "",
                    help="YYYY-MM-DD, pretend today is this date")
    ap.add_argument("--any-open", action="store_true",
                    default=(os.getenv("TEST_ANY_OPEN") or "").lower() == "true",
                    help="include every IPO that closes today or later")
    ap.add_argument("--send-to", choices=["admin", "channel"],
                    default=os.getenv("TEST_SEND_TO") or "admin")
    ap.add_argument("--dry-run", action="store_true",
                    default=(os.getenv("DRY_RUN") or "").lower() == "true")
    a = ap.parse_args()

    # Override the production settings
    m.MIN_GMP_PCT = a.min_pct
    m.DRY_RUN = a.dry_run
    target = (m.ADMIN_CHAT_ID if a.send_to == "admin" else m.CHAT_ID)
    if not target and not a.dry_run:
        sys.exit(f"No chat id available for --send-to {a.send_to}. "
                 "Set the ADMIN_CHAT_ID / TELEGRAM_CHAT_ID secret.")

    now = datetime.now(m.IST)
    today = date.fromisoformat(a.date) if a.date else now.date()
    m.log(f"[TEST] today={today} | min={a.min_pct:g}% | any_open={a.any_open} "
          f"| send_to={a.send_to} | SME={m.INCLUDE_SME}")

    ipos = m.get_ipos(today)
    if not ipos:
        sys.exit("Parsed 0 IPO rows. Layout may have changed.")
    m.log(f"[TEST] Parsed {len(ipos)} IPOs from the page:")
    for i in sorted(ipos, key=lambda x: x["close"]):
        m.log(f"   closes {i['close']} | {i['name'][:35]:35} | "
              f"GMP {i['gmp']:g} / {i['upper']:g} = {i['pct']:.1f}%")

    pool = [i for i in ipos if (i["close"] >= today if a.any_open
                                else i["close"] == today)]
    hits = [i for i in pool
            if i["pct"] >= a.min_pct and (m.INCLUDE_SME or not i["sme"])]
    m.log(f"[TEST] {len(pool)} in date range, {len(hits)} meet >= {a.min_pct:g}%")

    if not hits:
        m.log("[TEST] Nothing qualifies. Try a lower --min-pct, "
              "another --date, or --any-open.")
        return
    msg = "🧪 <b>TEST RUN</b> (not a real alert)\n\n" + m.build_message(hits, now)
    m.send_telegram(msg, target)
    m.log(f"[TEST] Sent {len(hits)} IPO(s) to {a.send_to}.")


if __name__ == "__main__":
    main()
