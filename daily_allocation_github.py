import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------- Config (can be overridden by environment variables) ----------------
PRODUCT_ID = os.getenv("PRODUCT_ID", "BTC-USD")
BUCKET = 500
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh/daily_allocation")
NTFY_MAX_BYTES = 4096                                   # ntfy message size limit; if exceeded, nothing is sent
SUMMARY_DAY = os.getenv("SUMMARY_DAY", "yesterday")     # "yesterday" = previous full UTC day, "today" = current UTC day so far
MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))            # 0 = no limit; set e.g. 20 for a quick test (partial counts)
PAGE_LIMIT = 1000
SLEEP_BETWEEN_CALLS = 0.12                              # Coinbase public limit is ~10 requests/sec
MAX_RETRIES = 5

TRADES_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/trades"
HEADERS = {"User-Agent": "github-daily-allocation/1.0"}


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_page(after=None):
    params = {"limit": PAGE_LIMIT}
    if after is not None:
        params["after"] = after
    for attempt in range(MAX_RETRIES):
        r = requests.get(TRADES_URL, params=params, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"Coinbase API failed after {MAX_RETRIES} retries (last status {r.status_code})")


def summarise_day(day_start: datetime, day_end: datetime):
    """Walk trades newest -> oldest and count per price bucket until the first trade of the day."""
    counts = {}
    total = 0
    after = None
    pages = 0
    done = False
    while not done:
        trades = fetch_page(after)
        pages += 1
        if not trades:
            break
        for t in trades:
            ts = parse_time(t["time"])
            if ts >= day_end:
                continue                 # newer than the window
            if ts < day_start:
                done = True              # reached the previous day
                break
            low = int(float(t["price"]) // BUCKET * BUCKET)
            counts[low] = counts.get(low, 0) + 1
            total += 1
        after = min(t["trade_id"] for t in trades)
        if MAX_PAGES and pages >= MAX_PAGES:
            print(f"MAX_PAGES={MAX_PAGES} reached: counts are PARTIAL (test mode)")
            break
        if pages % 100 == 0:
            print(f"... {pages} pages, {total:,} trades counted")
        time.sleep(SLEEP_BETWEEN_CALLS)
    return counts, total, pages


def build_body(counts: dict) -> str:
    return "\n".join(f"{low:,} - {low + BUCKET - 1:,} : {counts[low]:,}" for low in sorted(counts))


def main():
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if SUMMARY_DAY == "today":
        day_start, day_end = today_start, today_start + timedelta(days=1)
    else:
        day_start, day_end = today_start - timedelta(days=1), today_start

    title = f"{day_start.strftime('%Y-%m-%d')} summary"
    counts, total, pages = summarise_day(day_start, day_end)
    print(f"{title}: {total:,} trades, {len(counts)} groups, {pages} pages")

    if not counts:
        print("No trades found. Nothing sent.")
        return

    body = build_body(counts)
    size = len(body.encode("utf-8"))
    if size > NTFY_MAX_BYTES:
        print(f"Body too large ({size} bytes > {NTFY_MAX_BYTES}). Nothing sent.")
        return

    r = requests.post(NTFY_URL, data=body.encode("utf-8"), headers={"Title": title}, timeout=20)
    r.raise_for_status()
    print(f"Sent to ntfy ({size} bytes).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
