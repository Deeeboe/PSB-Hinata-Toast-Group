#!/usr/bin/env python3
"""Combined upcoming catering — PITCH + Hinata — from the Toast Orders API.

Pulls source=="Catering" orders across BOTH restaurant GUIDs for the next N days
(by event/business date), merges them, and prints one clean chronological list.

Usage:
    python3 Finance/toast_catering.py            # next 14 days
    python3 Finance/toast_catering.py 30         # next 30 days
    python3 Finance/toast_catering.py 7 --json   # next 7 days, machine-readable

Reads creds from .vault/toast.env via toast_client (Standard "demo webhook" cred).
Stdlib-only. Respects Toast's token cache + rate limits.
"""
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toast_client import load_env, get_token

HST_OFFSET = timedelta(hours=10)  # Hawaii Standard Time = UTC-10, no DST


def fmt_local(iso):
    """ISO-8601 UTC timestamp -> 'Sat 06/27 06:30PM' in Hawaii local time."""
    if not iso:
        return "?"
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S") - HST_OFFSET
        return dt.strftime("%a %m/%d %I:%M%p")
    except Exception:
        return iso


def fetch_day(host, token, guid, bdate):
    """All orders for one restaurant on one business date, following offset pages."""
    out, page = [], 1
    while True:
        url = f"{host}/orders/v2/ordersBulk?businessDate={bdate}&pageSize=100&page={page}"
        req = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                batch = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:120]}")
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:          # short page == last page (no Link headers)
            break
        page += 1
        time.sleep(0.25)              # stay well under 5 req/sec/location
    return out


def order_row(o, restaurant):
    checks = o.get("checks") or []
    customer = "—"
    for ch in checks:
        c = ch.get("customer") or {}
        name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        if name:
            customer = name
            break
    amount = 0.0
    for ch in checks:
        amount += ch.get("totalAmount") or ch.get("amount") or 0
    items = []
    for ch in checks:
        for sel in (ch.get("selections") or []):
            name = sel.get("displayName") or sel.get("name")
            qty = sel.get("quantity")
            if name:
                items.append(f"{int(qty)}x {name}" if qty else name)
    return {
        "when": o.get("promisedDate") or o.get("openedDate"),
        "restaurant": restaurant,
        "number": o.get("displayNumber"),
        "customer": customer,
        "amount": amount,
        "items": items,
        "source": o.get("source"),
        "guid": o.get("guid"),
    }


def collect(days):
    creds = load_env()
    host = creds["TOAST_API_HOST"]
    locations = [("PITCH", creds["TOAST_PITCH_GUID"]), ("Hinata", creds["TOAST_HINATA_GUID"])]
    token = get_token(creds, "TOAST_STD_CLIENT_ID", "TOAST_STD_CLIENT_SECRET")

    today = datetime.now()
    rows, errors = [], []
    for d in range(days + 1):
        bdate = (today + timedelta(days=d)).strftime("%Y%m%d")
        for name, guid in locations:
            try:
                for o in fetch_day(host, token, guid, bdate):
                    if str(o.get("source", "")).lower() == "catering":
                        rows.append(order_row(o, name))
            except RuntimeError as e:
                errors.append(f"{name} {bdate}: {e}")
    rows.sort(key=lambda r: r["when"] or "")
    return today, rows, errors


def main():
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    nums = [a for a in args if a.isdigit()]
    days = int(nums[0]) if nums else 14

    today, rows, errors = collect(days)

    if as_json:
        print(json.dumps({"generated": today.isoformat(), "days": days, "orders": rows}, indent=2))
        return

    end = today + timedelta(days=days)
    print(f"\n🍱 Upcoming catering — PITCH + Hinata — next {days} days "
          f"({today.strftime('%m/%d')}–{end.strftime('%m/%d')})\n")
    if errors:
        for e in errors:
            print(f"  ! {e}")
        print()
    if not rows:
        print("  (no catering orders found in this window)")
        return
    for r in rows:
        print(f"  {fmt_local(r['when']):<17} [{r['restaurant']:<6}] #{r['number'] or '—':<7} "
              f"{r['customer'][:24]:<24} ${r['amount']:,.2f}")
        if r["items"]:
            print(f"       {', '.join(r['items'][:8])}")
    total = sum(r["amount"] for r in rows)
    print(f"\n  {len(rows)} catering orders · ${total:,.2f} total")


if __name__ == "__main__":
    main()
