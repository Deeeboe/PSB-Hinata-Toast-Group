#!/usr/bin/env python3
"""
catering_approval_poll.py — autonomous catering-approval poller (Phase 2).

Input: a JSON file of Google Calendar events from the PITCH info@ Private Cal
(the cloud routine dumps them there via the Calendar MCP). This script does the
DETERMINISTIC part: find tentative catering orders, classify the department,
flag URGENT, dedup against state, and either print the plan (--dry) or send the
approval SMS via ghl_send_sms.py and record state.

Why split this way: the calendar READ + the 5-min-hold orchestration live in the
routine (they need MCP / timing). Classification + dedup + message-building are
pure logic and belong in a tested, reviewable script.

Decisions baked in (Derrick 2026-06-29):
  - Recipients = full kitchen crew on EVERY catering approval: Darin + Jon Jon +
    Nixon. CC Derrick + Keita. (Classifier still names the lead dept in the text.)
  - Dedup keyed on calendar eventId (state file). Re-ping on material change
    (items/headcount/date/dept hash).
  - URGENT when earliest event date is <= 7 days out (date-floored in HST).

Usage:
  python3 catering_approval_poll.py --events events.json [--dry] [--state PATH]
"""
import argparse, json, os, re, sys, hashlib, datetime

HST = datetime.timezone(datetime.timedelta(hours=-10))

# GHL contact IDs
RECIPIENTS = {  # standing approval crew (all depts)
    "Darin":  "peVEGm1t884L4bZKuhrJ",
    "Jon Jon": "ZBS9x5MyTMJNOL6ONlO9",
    "Nixon":  "PQl9jt0IKdVD2qYFCFjQ",
}
CC = {  # visibility
    "Derrick": "a5VfxJQzMHa5SrdXXhcP",
    "Keita":   "znB4oBW3MVkvkeWwMuIE",
}

TENTATIVE = re.compile(r"Catering order status:\s*(Tentative|Estimate|Open|Inquiry)", re.I)
CONFIRMED = re.compile(r"Catering order status:\s*Confirmed", re.I)
LEAD = re.compile(r"Lead status:", re.I)
ORDERNO = re.compile(r"#(\d{4,})")
GUESTS = re.compile(r"Guest count:\s*(\d+)", re.I)

SUSHI_WORDS = re.compile(r"\b(sushi|sashimi|nigiri|maki|temaki|hand\s?roll|handroll|gunkan|chirashi|barazushi|matsuri)\b", re.I)
BENTO = re.compile(r"\bbento\b", re.I)
BENTO_SKU = re.compile(r"\$\d{1,2}\s?[A-Z]\b")


def classify(items_text):
    """Return lead dept label from the item list (notes ignored upstream)."""
    has_sushi = bool(SUSHI_WORDS.search(items_text))
    # 'platter' alone is NOT sushi; only sushi/sashimi platter (covered by words above)
    has_bento = bool(BENTO.search(items_text) or BENTO_SKU.search(items_text))
    has_hot = bool(re.search(r"\b(pan|pans|chicken|ribeye|noodles|rice|fries|tots|gyoza|edamame|pork|brussel|wings|skins|rinds|waffle)\b", items_text, re.I))
    if has_sushi and (has_hot or has_bento):
        return "Mixed"
    if has_sushi:
        return "Sushi"
    if has_bento:
        return "Bento"
    return "Hot"


def parse_event(ev):
    """Pull the order fields out of a calendar event. Return dict or None."""
    desc = ev.get("description", "") or ""
    if CONFIRMED.search(desc) or LEAD.search(desc):
        return None
    if not TENTATIVE.search(desc):
        return None  # not a tentative catering order

    # items = lines after the last '---' separator that look like "<qty> <item>"
    items_lines = []
    for line in desc.splitlines():
        if re.match(r"\s*\d+\s+\S", line) and "Guest count" not in line:
            items_lines.append(line.strip())
    items_text = "\n".join(items_lines)

    start = ev.get("start", {})
    start_dt = start.get("dateTime") or start.get("date")
    customer = ev.get("summary", "").strip()
    # prefer the name in the "Customer info:" block if present
    m = re.search(r"Customer info:\s*\n([^\n]+)", desc)
    if m:
        customer = m.group(1).strip()

    om = ORDERNO.search(desc)
    gm = GUESTS.search(desc)
    return {
        "eventId": ev.get("id"),
        "orderNo": om.group(1) if om else None,
        "customer": customer,
        "startDateTime": start_dt,
        "guests": gm.group(1) if gm else None,
        "itemsText": items_text,
        "dept": classify(items_text),
    }


def event_date(start_dt):
    if not start_dt:
        return None
    try:
        dt = datetime.datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
        return dt.astimezone(HST).date()
    except ValueError:
        try:
            return datetime.date.fromisoformat(start_dt[:10])
        except ValueError:
            return None


def is_urgent(d, today):
    return d is not None and (d - today).days <= 7


def content_hash(o):
    blob = f"{o['itemsText']}|{o['guests']}|{o['startDateTime']}|{o['dept']}"
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def fmt_ready(start_dt):
    d = event_date(start_dt)
    try:
        dt = datetime.datetime.fromisoformat(start_dt.replace("Z", "+00:00")).astimezone(HST)
        t = dt.strftime("%-I:%M%p").lower()
    except Exception:
        t = "TBD"
    day = d.strftime("%a %-m/%-d") if d else "TBD"
    return day, t


def item_summary(items_text, limit=160):
    parts = [re.sub(r"\s*\(1/2 Pan\)", "", l) for l in items_text.splitlines()]
    s = ", ".join(parts)
    return (s[:limit] + "…") if len(s) > limit else s


def build_message(o, today):
    day, ready = fmt_ready(o["startDateTime"])
    urgent = " 🔴 URGENT" if is_urgent(event_date(o["startDateTime"]), today) else ""
    head = f"{o['guests']} pax" if o.get("guests") and o["guests"] != "1" else "headcount TBD"
    order = o["orderNo"] or "(no #)"
    return (
        f"🍽️ CATERING APPROVAL{urgent}\n"
        f"{o['customer']} · {day} · ready {ready}\n"
        f"→ {o['dept']}: {item_summary(o['itemsText'])}\n"
        f"#{order} · {head}\n"
        f"Reply YES {order} to approve, or flag any issue. 🤙🏽"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, help="JSON file: list of calendar events OR {events:[...]}")
    ap.add_argument("--state", default="Integrations/GHL/approval-pinged.json")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    raw = json.load(open(a.events))
    events = raw.get("events", raw) if isinstance(raw, dict) else raw

    state = {"events": {}}
    if os.path.exists(a.state):
        state = json.load(open(a.state))
    state.setdefault("events", {})

    today = datetime.datetime.now(HST).date()
    plan = []
    for ev in events:
        o = parse_event(ev)
        if not o or not o["eventId"]:
            continue
        h = content_hash(o)
        prev = state["events"].get(o["eventId"])
        if prev and prev.get("hash") == h and prev.get("pingedAt"):
            continue  # already pinged, unchanged
        o["hash"] = h
        plan.append(o)

    if not plan:
        print("No new tentative catering orders to send.")
        return

    for o in plan:
        msg = build_message(o, today)
        print("=" * 60)
        print(f"[{'DRY' if a.dry else 'SEND'}] #{o['orderNo']} {o['customer']} ({o['dept']})")
        print(msg)
        if a.dry:
            continue
        # live send
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ghl_send_sms import send_sms
        for name, cid in {**RECIPIENTS, **CC}.items():
            tag = "" if name in RECIPIENTS else "📋 CC (sent to Darin/Jon Jon/Nixon)\n"
            send_sms(cid, tag + msg)
        state["events"][o["eventId"]] = {
            "orderNo": o["orderNo"], "customer": o["customer"],
            "eventDate": str(event_date(o["startDateTime"])), "dept": o["dept"],
            "hash": o["hash"], "pingedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "approvedAt": None, "approvedBy": None,
        }

    if not a.dry:
        tmp = a.state + ".tmp"
        json.dump(state, open(tmp, "w"), indent=2)
        os.replace(tmp, a.state)
        print(f"\nstate updated: {a.state}")


if __name__ == "__main__":
    main()
