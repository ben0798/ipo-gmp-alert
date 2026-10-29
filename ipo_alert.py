#!/usr/bin/env python3
"""
IPO GMP last-day alert.

Sends a Telegram message when an IPO that closes TODAY (IST) has
GMP% >= MIN_GMP_PCT. GMP% = GMP / upper price band * 100.

Env vars:
  TELEGRAM_BOT_TOKEN   (required)  from @BotFather
  TELEGRAM_CHAT_ID     (required)  your chat id, or channel like @my_ipo_alerts
  ADMIN_CHAT_ID        (optional)  where error messages go (your personal chat id)
  MIN_GMP_PCT          (default 30)
  INCLUDE_SME          (default true)  set false to ignore SME IPOs
  GMP_URL              (default Investorgain live GMP dashboard)
  FORCE_DATE           (optional) YYYY-MM-DD, pretend today is this date (testing)
  TEST_MODE            (optional) "true" -> just send a test message and exit
  DRY_RUN              (optional) "true" -> print instead of sending
"""
import html
import io
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
GMP_URL = os.getenv("GMP_URL") or "https://www.investorgain.com/report/live-ipo-gmp/331/"
MIN_GMP_PCT = float(os.getenv("MIN_GMP_PCT") or 30)
INCLUDE_SME = (os.getenv("INCLUDE_SME") or "true").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
DRY_RUN = (os.getenv("DRY_RUN") or "").lower() == "true"
TEST_MODE = (os.getenv("TEST_MODE") or "").lower() == "true"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Telegram
def send_telegram(text, chat_id=None):
    chat_id = chat_id or CHAT_ID
    if DRY_RUN:
        log("[DRY RUN] would send to", chat_id, ":\n", text)
        return
    if not BOT_TOKEN or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


# ---------------------------------------------------------------- Fetching
def fetch_tables():
    """Try a plain HTTP fetch first; fall back to a headless browser
    (the dashboard may render its table with JavaScript)."""
    try:
        r = requests.get(GMP_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        if any(is_gmp_table(t) for t in tables):
            log("Fetched tables via plain HTTP")
            return tables
        log("Plain HTTP returned no GMP table, trying headless browser...")
    except Exception as e:  # noqa
        log("Plain HTTP fetch failed:", e, "- trying headless browser...")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(GMP_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_selector("table tbody tr", timeout=30000)
        content = page.content()
        browser.close()
    log("Fetched tables via headless browser")
    return pd.read_html(io.StringIO(content))


def flat_cols(df):
    df = df.copy()
    df.columns = [
        " ".join(str(x) for x in c if "Unnamed" not in str(x)).strip()
        if isinstance(c, tuple) else str(c).strip()
        for c in df.columns
    ]
    return df


def find_col(df, include, exclude=()):
    for c in df.columns:
        lc = c.lower()
        if any(k in lc for k in include) and not any(k in lc for k in exclude):
            return c
    return None


def is_gmp_table(df):
    df = flat_cols(df)
    return bool(find_col(df, ["gmp"], ["est"]) and find_col(df, ["close"]))


# ----------------------------------------------------------------- Parsing
def nums(s):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(s).replace(",", ""))]


def parse_close(s, today):
    s = re.sub(r"\s+", " ", str(s).strip().replace("-", " ").replace("/", " "))
    for fmt, has_year in (("%d %b %Y", True), ("%d %B %Y", True),
                          ("%d %b", False), ("%d %B", False),
                          ("%b %d", False)):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        year = d.year if has_year else today.year
        out = date(year, d.month, d.day)
        if not has_year and (today - out).days > 180:   # Dec -> Jan rollover
            out = date(year + 1, d.month, d.day)
        return out
    return None


def get_ipos(today):
    tables = [flat_cols(t) for t in fetch_tables()]
    tables = [t for t in tables if is_gmp_table(t)]
    if not tables:
        raise RuntimeError("Could not find the GMP table on the page "
                           "(site layout may have changed).")
    df = max(tables, key=len)
    c_name = find_col(df, ["ipo", "name", "company"]) or df.columns[0]
    c_price = find_col(df, ["price"])
    c_gmp = find_col(df, ["gmp"], ["est", "%"])
    c_close = find_col(df, ["close"])
    log("Columns used -> name:", c_name, "| price:", c_price,
        "| gmp:", c_gmp, "| close:", c_close)
    if not (c_price and c_gmp and c_close):
        raise RuntimeError(f"Missing columns. Found: {list(df.columns)}")

    ipos = []
    for _, row in df.iterrows():
        try:
            name = str(row[c_name]).strip()
            price, gmp = nums(row[c_price]), nums(row[c_gmp])
            close = parse_close(row[c_close], today)
            if not name or not price or not gmp or close is None:
                continue
            upper = price[-1]                     # upper end of the band
            if upper <= 0:
                continue
            ipos.append({
                "name": re.sub(r"\s*\bIPO\b\s*$", "", name, flags=re.I).strip(),
                "sme": bool(re.search(r"\bSME\b", name, re.I)),
                "upper": upper,
                "gmp": gmp[0],
                "pct": gmp[0] / upper * 100,
                "close": close,
            })
        except Exception as e:  # noqa - skip odd rows
            log("Skipping row:", e)
    return ipos


# -------------------------------------------------------------------- Main
def build_message(hits, now):
    lines = ["🔔 <b>IPO LAST-DAY ALERT</b>",
             f"GMP ≥ {MIN_GMP_PCT:g}% and bidding closes TODAY\n"]
    for h in sorted(hits, key=lambda x: -x["pct"]):
        tag = " (SME)" if h["sme"] else ""
        lines.append(
            f"<b>{html.escape(h['name'])}</b>{tag}\n"
            f"Upper band: ₹{h['upper']:g} | GMP: ₹{h['gmp']:g} | "
            f"<b>GMP: {h['pct']:.1f}%</b>"
        )
    lines.append(f"\n⏰ Checked {now:%d-%b %I:%M %p} IST. "
                 "Open 5paisa and apply before the cut-off "
                 "(UPI mandate can take time, don't wait for the last hour).")
    lines.append("⚠️ GMP is unofficial grey-market data, not a guarantee.")
    return "\n".join(lines)


def main():
    now = datetime.now(IST)
    if TEST_MODE:
        send_telegram("✅ Test message from your IPO GMP alert bot. Setup works!")
        log("Test message sent.")
        return

    today = now.date()
    if os.getenv("FORCE_DATE"):
        today = date.fromisoformat(os.environ["FORCE_DATE"])
    log(f"Today (IST): {today} | threshold: {MIN_GMP_PCT}% | SME: {INCLUDE_SME}")

    try:
        ipos = get_ipos(today)
        if not ipos:
            raise RuntimeError("Parsed 0 IPO rows - layout may have changed.")
    except Exception as e:
        log("ERROR:", e)
        if ADMIN_CHAT_ID:
            send_telegram(f"⚠️ IPO alert script failed:\n<code>{html.escape(str(e))}</code>",
                          ADMIN_CHAT_ID)
        sys.exit(1)

    closing = [i for i in ipos if i["close"] == today]
    log(f"Parsed {len(ipos)} IPOs; {len(closing)} close today.")
    for i in closing:
        log(f"  {i['name']}: GMP {i['gmp']:g} on {i['upper']:g} = {i['pct']:.1f}%")

    hits = [i for i in closing
            if i["pct"] >= MIN_GMP_PCT and (INCLUDE_SME or not i["sme"])]
    if not hits:
        log("No qualifying IPO today. No alert sent.")
        return
    send_telegram(build_message(hits, now))
    log(f"Alert sent for {len(hits)} IPO(s).")


if __name__ == "__main__":
    main()
