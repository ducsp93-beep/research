#!/usr/bin/env python3
"""
Allocation of 24H
-----------------
Counts Coinbase trades per (hour, price group) over the last TIME_LENGTH full
hours, prints the table, renders it as a PNG and sends it to an ntfy topic.

Window (all labels in UTC+7):
    end   = current time floored to the hour          (e.g. run at 09:31 -> 09:00 today)
    start = end - TIME_LENGTH hours                   (e.g. 09:00 yesterday)
    -> covers 09:00 yesterday ... 08:59:59 today

Coinbase's public trades endpoint has no time filter, so the script binary-searches
the trade IDs at the window boundaries and then pages only through that range.
Trades are aggregated on the fly; nothing is saved to disk except the PNG.

Install:  pip install requests matplotlib
Run:      python allocation_24h.py            (add --no-notify to skip the ntfy push)
"""

import argparse
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
TIME_LENGTH = 24                                  # hours, always in hours
NOTIF_URL = "https://ntfy.sh/daily_allocation"
PRODUCT = "BTC-USD"
PRICE_STEP = 500                                  # price groups, floored to nearest 500
TZ = timezone(timedelta(hours=7))                 # UTC+7 for window and labels
IMAGE_FILE = "allocation_24h.png"

API = "https://api.exchange.coinbase.com"
PAGE_LIMIT = 1000                                 # max trades per request
REQUEST_PAUSE = 0.12                              # seconds; public limit is ~10 req/s
MAX_RETRIES = 8


# --------------------------------------------------------------------------- #
# Coinbase helpers
# --------------------------------------------------------------------------- #
def parse_time(s: str) -> datetime:
    """Parse Coinbase timestamps such as 2026-11-26T02:31:12.123456Z."""
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    return datetime.fromisoformat(s)


def api_get(session, path, params):
    """GET with pacing and retry/backoff on rate limits and server errors."""
    for attempt in range(MAX_RETRIES):
        wait = min(2 ** attempt, 30)
        try:
            r = session.get(API + path, params=params, timeout=20)
            if r.status_code == 200:
                time.sleep(REQUEST_PAUSE)
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504):
                r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(wait)
    raise RuntimeError(f"Coinbase API kept failing for {path} {params}")


def get_trade(session, product, trade_id):
    """Trade with this ID (or the nearest older one if the ID is missing)."""
    data = api_get(session, f"/products/{product}/trades",
                   {"limit": 1, "after": trade_id + 1})
    return data[0] if data else None


def first_id_at_or_after(session, product, t, lo, hi):
    """Smallest trade ID in [lo, hi] whose time >= t, or hi + 1 if there is none."""
    answer = hi + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        trade = get_trade(session, product, mid)
        if trade is not None and parse_time(trade["time"]) >= t:
            answer = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return answer


# --------------------------------------------------------------------------- #
# Summarise
# --------------------------------------------------------------------------- #
def window(hours):
    end = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=hours), end


def summarise(session, product, start, end, hours):
    """Return Counter {(hour_index, price_group): trades} for [start, end)."""
    path = f"/products/{product}/trades"
    latest_id = api_get(session, path, {"limit": 1})[0]["trade_id"]

    print("Locating window boundaries (binary search on trade ID)...")
    start_id = first_id_at_or_after(session, product, start, 1, latest_id)
    end_id = first_id_at_or_after(session, product, end, start_id, latest_id)
    total_ids = end_id - start_id
    print(f"  trade IDs {start_id:,} to {end_id - 1:,} ({total_ids:,} trades)")

    utc = timezone.utc
    hour_index = {
        (start.astimezone(utc) + timedelta(hours=i)).strftime("%Y-%m-%dT%H"): i
        for i in range(hours)
    }

    counts = Counter()
    cursor = end_id                       # 'after' returns trades with ID < cursor
    pages = 0
    while cursor > start_id:
        page = api_get(session, path, {"limit": PAGE_LIMIT, "after": cursor})
        if not page:
            break
        for t in page:
            tid = t["trade_id"]
            if tid < start_id or tid >= end_id:
                continue
            idx = hour_index.get(t["time"][:13])
            if idx is None:
                continue
            group = int(Decimal(t["price"]) // PRICE_STEP) * PRICE_STEP
            counts[(idx, group)] += 1
        cursor = min(t["trade_id"] for t in page)
        pages += 1
        if pages % 20 == 0:
            done = min(1.0, (end_id - cursor) / max(total_ids, 1))
            print(f"\r  aggregating... {done:.0%}", end="", flush=True)
    if pages >= 20:
        print("\r  aggregating... 100%")
    return counts


# --------------------------------------------------------------------------- #
# Table, console, image
# --------------------------------------------------------------------------- #
def build_table(counts, start, hours):
    groups = [g for (_, g) in counts]
    cols = list(range(min(groups), max(groups) + PRICE_STEP, PRICE_STEP))
    header = ["Hour"] + [f"{c:,}" for c in cols]
    rows = []
    for i in range(hours):
        h = (start + timedelta(hours=i)).hour
        label = f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"
        rows.append([label] + [f"{counts[(i, c)]:,}" if counts[(i, c)] else "-"
                               for c in cols])
    return header, rows


def format_text(header, rows):
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]

    def line(r):
        cells = [r[0].ljust(widths[0])] + [r[i].rjust(widths[i]) for i in range(1, len(r))]
        return " | ".join(cells)

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([line(header), sep] + [line(r) for r in rows])


def render_image(header, rows, title, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ncols, nrows = len(header), len(rows)
    fig, ax = plt.subplots(figsize=(max(7, 0.9 * ncols + 0.8), 0.36 * (nrows + 1) + 1.0))
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=header, loc="center",
                     cellLoc="center", bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.auto_set_column_width(list(range(ncols)))
    for (r, _), cell in table.get_celld().items():
        cell.set_facecolor("white")
        cell.set_edgecolor("black")
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_text_props(weight="bold")
    ax.set_title(title, fontsize=11, pad=10)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def send_notification(path, title, message):
    with open(path, "rb") as f:
        body = f.read()
    r = requests.put(
        NOTIF_URL, data=body, timeout=60,
        headers={"Title": title, "Message": message,
                 "Filename": os.path.basename(path), "Content-Type": "image/png"},
    )
    r.raise_for_status()
    print(f"Notification sent to {NOTIF_URL} (HTTP {r.status_code})")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--product", default=PRODUCT)
    ap.add_argument("--hours", type=int, default=TIME_LENGTH)
    ap.add_argument("--no-notify", action="store_true", help="skip the ntfy push")
    args = ap.parse_args()

    start, end = window(args.hours)
    last = end - timedelta(minutes=1)
    span = f"{start:%d %b %H:%M} to {last:%d %b %H:%M} (UTC+7)"
    title = f"Allocation of {args.hours}H"
    print(f"{title} | {args.product} | {span}")

    with requests.Session() as session:
        session.headers["User-Agent"] = "allocation-24h/1.0"
        counts = summarise(session, args.product, start, end, args.hours)

    total = sum(counts.values())
    if not total:
        print("No trades found in the window.")
        sys.exit(1)

    header, rows = build_table(counts, start, args.hours)
    print(f"\n{title} | {args.product} | {span} | {total:,} trades\n")
    print(format_text(header, rows))

    image_title = f"{title} - {args.product} | {span} | {total:,} trades"
    render_image(header, rows, image_title, IMAGE_FILE)
    print(f"\nImage saved to {os.path.abspath(IMAGE_FILE)}")

    if not args.no_notify:
        send_notification(IMAGE_FILE, title, f"{args.product} | {span} | {total:,} trades")


if __name__ == "__main__":
    main()
