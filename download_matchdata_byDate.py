#!/usr/bin/env python3
"""Download Coinbase BTC-USD trades (matches) for one UTC date, with progress display.

Usage:
    python download_matchdata_byDate.py 2026-11-18
    python download_matchdata_byDate.py 2026-11-18 --product ETH-USD --out eth.csv
"""
import argparse
import csv
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.exchange.coinbase.com"
session = requests.Session()
session.headers["User-Agent"] = "cb-trades-downloader/1.0"


def api_get(path, params=None, retries=6):
    """GET with retry/backoff (public limit is ~10 req/s)."""
    for attempt in range(retries):
        try:
            r = session.get(BASE + path, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError("unreachable")


def parse_time(s):
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s)


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def print_line(text):
    """Overwrite the current console line."""
    sys.stdout.write("\r" + text.ljust(110))
    sys.stdout.flush()


def trade_at(product, trade_id):
    data = api_get(f"/products/{product}/trades", {"after": trade_id + 1, "limit": 1})
    if data and data[0]["trade_id"] == trade_id:
        return data[0]
    return None


def first_id_at_or_after(product, ts, latest_id, label):
    """Smallest trade_id whose time >= ts, with step progress."""
    lo, hi = 1, latest_id + 1
    total_steps = max(1, math.ceil(math.log2(latest_id + 1)))
    step = 0
    while lo < hi:
        mid = (lo + hi) // 2
        t = trade_at(product, mid)
        if t is None or parse_time(t["time"]) < ts:
            lo = mid + 1
        else:
            hi = mid
        step += 1
        print_line(f"Searching {label} trade ID... step {min(step, total_steps)}/{total_steps}")
        time.sleep(0.12)
    print_line(f"Found {label} trade ID: {lo:,}")
    print()
    return lo


def download(product, day, out_path):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    print(f"Product : {product}")
    print(f"Window  : {start:%Y-%m-%d %H:%M:%S} -> {end:%Y-%m-%d %H:%M:%S} UTC")

    latest = api_get(f"/products/{product}/trades", {"limit": 1})[0]["trade_id"]
    print(f"Latest trade id: {latest:,}")

    start_id = first_id_at_or_after(product, start, latest, "start")
    end_id = first_id_at_or_after(product, end, latest, "end")  # exclusive

    total = end_id - start_id
    if total <= 0:
        print("No trades found for that date.")
        return
    print(f"Trade id range: [{start_id:,}, {end_id:,})  ~{total:,} trades\n")

    count = 0
    cursor = end_id
    t0 = time.time()
    bar_width = 30

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trade_id", "time", "price", "size", "side"])
        while cursor > start_id:
            trades = api_get(f"/products/{product}/trades",
                             {"after": cursor, "limit": 1000})
            if not trades:
                break
            for t in trades:  # descending by id
                if start_id <= t["trade_id"] < end_id:
                    w.writerow([t["trade_id"], t["time"], t["price"],
                                t["size"], t["side"]])
                    count += 1
            cursor = min(t["trade_id"] for t in trades)

            # progress
            done_ids = min(total, end_id - cursor)
            frac = done_ids / total
            elapsed = time.time() - t0
            speed = count / elapsed if elapsed > 0 else 0
            eta = elapsed * (1 - frac) / frac if frac > 0 else 0
            filled = int(bar_width * frac)
            bar = "#" * filled + "-" * (bar_width - filled)
            print_line(
                f"[{bar}] {frac*100:5.1f}% | {count:,}/~{total:,} trades | "
                f"{speed:,.0f}/s | elapsed {fmt_time(elapsed)} | ETA {fmt_time(eta)}"
            )
            time.sleep(0.12)

    elapsed = time.time() - t0
    print_line(f"[{'#' * bar_width}] 100.0% | {count:,} trades | elapsed {fmt_time(elapsed)}")
    print(f"\nDone: {count:,} trades -> {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("date", help="UTC date, YYYY-MM-DD")
    p.add_argument("--product", default="BTC-USD")
    p.add_argument("--out", help="output CSV path")
    a = p.parse_args()

    day = datetime.strptime(a.date, "%Y-%m-%d").date()
    out = a.out or f"{a.product}_trades_{a.date}.csv"
    download(a.product, day, out)