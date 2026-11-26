#!/usr/bin/env python3
"""
Partial-day (UTC) trade-count summary from Coinbase Exchange -> ntfy.sh

- Walks Coinbase "matches" (public trades) backwards from the latest trade
  until it crosses 00:00:00 UTC of today. Nothing is saved to disk; trades are
  counted on the fly into 500-wide price buckets.
- Prints the summary to the console and posts the same body to ntfy.
- If the body is larger than ntfy's message limit (4096 bytes), nothing is sent.
"""
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests

PRODUCT_ID = os.environ.get("PRODUCT_ID", "BTC-USD")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/daily_allocation")
BUCKET = 500
NTFY_MAX_BYTES = 4096          # ntfy.sh message size limit
API_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/trades"
PAGE_LIMIT = 1000              # max allowed by Coinbase
PAUSE = 0.15                   # seconds between calls (public limit ~10 req/s)


def get_page(session, after=None):
    """Fetch one page of trades (newest first). `after` = return trades older than this trade_id."""
    params = {"limit": PAGE_LIMIT}
    if after is not None:
        params["after"] = after
    for attempt in range(8):
        try:
            r = session.get(API_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"request error ({exc}), retrying...", file=sys.stderr)
            time.sleep(2 ** min(attempt, 5))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** min(attempt, 5))
            continue
        r.raise_for_status()
    raise RuntimeError("Coinbase API kept failing after retries")


def fmt_range(lo):
    return f"{lo:,} - {lo + BUCKET - 1:,}"


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    start_iso = f"{today}T00:00:00"  # ISO UTC strings compare correctly as text

    counts = Counter()
    newest_ts = None   # latest trade of the day (first one seen, we go backwards)
    oldest_ts = None   # first trade of the day (last one counted)
    total = 0
    after = None
    done = False
    pages = 0

    with requests.Session() as session:
        while not done:
            trades = get_page(session, after)
            pages += 1
            if not trades:
                break
            for t in trades:
                ts = t["time"]
                if ts < start_iso:
                    done = True
                    break
                if newest_ts is None:
                    newest_ts = ts
                oldest_ts = ts
                lo = math.floor(float(t["price"]) / BUCKET) * BUCKET
                counts[lo] += 1
                total += 1
            if len(trades) < PAGE_LIMIT:
                break  # reached the start of the available history
            after = trades[-1]["trade_id"]
            time.sleep(PAUSE)

    if not counts:
        print(f"No {PRODUCT_ID} trades found yet for {today} (UTC). Nothing to send.")
        return

    body = "\n".join(f"{fmt_range(lo)} : {counts[lo]:,}" for lo in sorted(counts))
    title = f"{today} summary"

    # Console output (same summary as the notification body)
    print(f"{title} | {PRODUCT_ID} | {total:,} trades | {pages} pages")
    print(f"From first trade: {oldest_ts}  To latest trade: {newest_ts}  (UTC)")
    print("-" * 40)
    print(body)

    size = len(body.encode("utf-8"))
    if size > NTFY_MAX_BYTES:
        print(f"\nBody is {size} bytes (limit {NTFY_MAX_BYTES}). Not sending notification.")
        return

    resp = requests.post(
        NTFY_URL,
        data=body.encode("utf-8"),
        headers={"Title": title},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"\nNotification sent to {NTFY_URL} ({size} bytes).")


if __name__ == "__main__":
    main()
