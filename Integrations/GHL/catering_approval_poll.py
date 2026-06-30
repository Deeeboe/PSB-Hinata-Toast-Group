#!/usr/bin/env python3
"""
catering_approval_poll.py — autonomous catering-approval poller (Phase 2, LIVE).

Reads tentative catering orders off the PITCH Private Calendar (events dumped to
a JSON file by the cloud routine via the Calendar MCP), classifies the dept,
flags URGENT, and runs the HEADS-UP HOLD flow (Derrick 2026-06-29):

  Run 1 (first time an order is seen): text Derrick a preview heads-up.
  Run 2+ (next hourly check): if Derrick has NOT replied "STOP <order#>",
          send the approval to the chefs (Darin + Jon Jon + Nixon) and CC
          Derrick + Keita. If he replied STOP, hold and skip.

State is tracked STATELESSLY by reading Derrick's own GHL thread (survives fresh
cloud runs — no state file to lose):
  - heads-up already sent?  -> his thread has an outbound "🆕 ... #NNNN"
  - already sent to chefs?   -> his thread has an outbound "CC (sent ... #NNNN"
  - Derrick said stop?       -> his thread has an inbound "STOP ... NNNN"

GO-LIVE CUTOFF: only orders whose calendar event was CREATED after GOLIVE_AFTER
are ever acted on, so the existing backlog of tentatives is grandfathered and
never blasted. Override with env GOLIVE_AFTER (ISO8601).

Recipients (Derrick 2026-06-29): chefs = Darin + Jon Jon + Nixon on every order.

Usage:
  python3 catering_approval_poll.py --events events.json [--dry]
"""
import argparse, json, os, re, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ghl_send_sms import send_sms, get_recent_messages

HST = datetime.timezone(datetime.timedelta(hours=-10))
GOLIVE_AFTER = os.environ.get("GOLIVE_AFTER", "2026-06-29T21:05:00-10:00")
HEARTBEAT_HOUR = 8  # one liveness ping/day, at ~8am HST

DERRICK = "a5VfxJQzMHa5SrdXXhcP"
KEITA = "znB4oBW3MVkvkeWwMuIE"
CHEFS = {"Darin": "peVEGm1t884L4bZKuhrJ", "Jon Jon": "ZBS9x5MyTMJNOL6ONlO9", "Nixon": "PQl9jt0IKdVD2qYFCFjQ"}

TENTATIVE = re.compile(r"Catering order status:\s*(Tentative|Estimate|Open|Inquiry)", re.I)
CONFIRMED = re.compile(r"Catering order status:\s*Confirmed", re.I)
LEAD = re.compile(r"Lead status:", re.I)
ORDERNO = re.compile(r"#(\d{4,})")
GUESTS = re.compile(r"Guest count:\s*(\d+)", re.I)
SUSHI = re.compile(r"\b(sushi|sashimi|nigiri|maki|temaki|hand\s?roll|handroll|gunkan|chirashi|barazushi|matsuri)\b", re.I)
BENTO = re.compile(r"\bbento\b", re.I)
BENTO_SKU = re.compile(r"\$\d{1,2}\s?[A-Z]\b")
HOT = re.compile(r"\b(pan|pans|chicken|ribeye|noodles|rice|fries|tots|gyoza|edamame|pork|brussel|wings|skins|rinds|waffle|chop|salad)\b", re.I)


def classify(items):
    s, b, h = bool(SUSHI.search(items)), bool(BENTO.search(items) or BENTO_SKU.search(items)), bool(HOT.search(items))
    if s and (h or b):
        return "Mixed"
    if s:
        return "Sushi"
    if b:
        return "Bento"
    return "Hot"


def parse_event(ev):
    desc = ev.get("description", "") or ""
    if CONFIRMED.search(desc) or LEAD.search(desc) or not TENTATIVE.search(desc):
        return None
    items = "\n".join(l.strip() for l in desc.splitlines()
                      if re.match(r"\s*\d+\s+\S", l) and "Guest count" not in l)
    start = ev.get("start", {})
    cust = ev.get("summary", "").strip()
    m = re.search(r"Customer info:\s*\n([^\n]+)", desc)
    if m:
        cust = m.group(1).strip()
    om, gm = ORDERNO.search(desc), GUESTS.search(desc)
    return {"eventId": ev.get("id"), "created": ev.get("created"),
            "orderNo": om.group(1) if om else None, "customer": cust,
            "startDateTime": start.get("dateTime") or start.get("date"),
            "guests": gm.group(1) if gm else None, "itemsText": items,
            "dept": classify(items)}


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.datetime.fromisoformat(ts[:10]).replace(tzinfo=HST)
        except ValueError:
            return None


def event_date(ts):
    d = _parse(ts)
    return d.astimezone(HST).date() if d else None


def is_urgent(d, today):
    return d is not None and (d - today).days <= 7


def fmt_ready(ts):
    d = event_date(ts)
    dt = _parse(ts)
    t = dt.astimezone(HST).strftime("%-I:%M%p").lower() if dt else "TBD"
    return (d.strftime("%a %-m/%-d") if d else "TBD"), t


def summary(items, limit=150):
    s = ", ".join(re.sub(r"\s*\(1/2 Pan\)", "", l) for l in items.splitlines())
    return (s[:limit] + "…") if len(s) > limit else s


def head_count(o):
    return f"{o['guests']} pax" if o.get("guests") and o["guests"] != "1" else "headcount TBD"


def approval_text(o, today):
    day, ready = fmt_ready(o["startDateTime"])
    u = " 🔴 URGENT" if is_urgent(event_date(o["startDateTime"]), today) else ""
    return (f"🍽️ CATERING APPROVAL{u}\n{o['customer']} · {day} · ready {ready}\n"
            f"→ {o['dept']}: {summary(o['itemsText'])}\n#{o['orderNo']} · {head_count(o)}\n"
            f"Reply YES {o['orderNo']} to approve, or flag any issue. 🤙🏽")


def headsup_text(o, today):
    day, ready = fmt_ready(o["startDateTime"])
    u = " 🔴 URGENT" if is_urgent(event_date(o["startDateTime"]), today) else ""
    return (f"🆕 NEW CATERING TENTATIVE{u}\n{o['customer']} · {day} · ready {ready}\n"
            f"→ {o['dept']}: {summary(o['itemsText'])}\n#{o['orderNo']} · {head_count(o)}\n"
            f"Sending to the chefs at the next check (~1hr) unless you reply STOP {o['orderNo']}. 🤙🏽")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    raw = json.load(open(a.events))
    events = raw.get("events", raw) if isinstance(raw, dict) else raw
    now = datetime.datetime.now(HST)
    today = now.date()
    golive = _parse(GOLIVE_AFTER)

    # candidates = tentatives created AFTER go-live (backlog grandfathered)
    cands = []
    for ev in events:
        o = parse_event(ev)
        if not o or not o["orderNo"]:
            continue
        c = _parse(o["created"])
        if c is None or (golive and c <= golive):
            continue
        cands.append(o)

    # read Derrick's thread ONCE; derive state per order from it
    derrick_msgs = get_recent_messages(DERRICK, limit=50)
    def has(*subs, direction=None):
        for m in derrick_msgs:
            if direction and m["direction"] != direction:
                continue
            body = m["body"].upper()
            if all(s.upper() in body for s in subs):
                return True
        return False

    actions = []
    for o in cands:
        no = o["orderNo"]
        if has("CC (SENT", no):
            actions.append(f"#{no} {o['customer']}: already sent to chefs — skip")
            continue
        if has("🆕", no):  # heads-up already went out
            if has("STOP", no, direction="inbound"):
                actions.append(f"#{no} {o['customer']}: STOP by Derrick — held")
                continue
            # promote: send to chefs + CC
            msg = approval_text(o, today)
            cc = f"📋 CC (sent to Darin/Jon Jon/Nixon) #{no}\n" + msg
            if a.dry:
                actions.append(f"#{no} {o['customer']}: WOULD SEND to chefs now")
            else:
                for cid in CHEFS.values():
                    send_sms(cid, msg)
                send_sms(DERRICK, cc)
                send_sms(KEITA, cc)
                actions.append(f"#{no} {o['customer']}: SENT to chefs ✅")
        else:
            # first sighting -> heads-up to Derrick
            if a.dry:
                actions.append(f"#{no} {o['customer']}: WOULD send heads-up to Derrick")
            else:
                send_sms(DERRICK, headsup_text(o, today))
                actions.append(f"#{no} {o['customer']}: heads-up sent to Derrick 🆕")

    # report
    print(f"Ran {now.strftime('%Y-%m-%d %H:%M HST')}. {len(events)} events, "
          f"{len(cands)} live candidate(s) (after cutoff {GOLIVE_AFTER}).")
    for line in actions:
        print("  " + line)
    if not actions:
        print("  nothing to do.")

    # daily liveness heartbeat to Derrick (so silence = alarm)
    if not a.dry and now.hour == HEARTBEAT_HOUR:
        send_sms(DERRICK, f"✅ Catering poller alive. Ran {now.strftime('%-I:%M%p HST %-m/%-d')}. "
                          f"{len(events)} events scanned, {len(cands)} active tentative(s).")


if __name__ == "__main__":
    main()
