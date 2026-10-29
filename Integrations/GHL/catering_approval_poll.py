#!/usr/bin/env python3
"""
catering_approval_poll.py — autonomous catering-approval poller (Phase 2, LIVE).

Reads tentative catering orders off the PITCH Private Calendar (events dumped to
a JSON file by the cloud routine via the Calendar MCP), classifies the dept,
flags URGENT, and sends the approval to the chefs (Darin + Jon Jon + Nixon)
the FIRST time an order is seen, CC Derrick + Keita (Derrick 2026-10-18: drop the
1-hour heads-up/HOLD window; orders entered in Toast have been accurate).

  Legacy: an order that already got the old "🆕" heads-up and that Derrick
  replied "HOLD <order#>" to stays held. Nothing new asks for HOLD anymore.
  (This is staff-only: chefs, Derrick, Keita. It never contacts a customer.)

State is tracked STATELESSLY by reading Derrick's own GHL thread (survives fresh
cloud runs — no state file to lose):
  - already sent to chefs?   -> his thread has an outbound "CC (sent ... #NNNN"
  - legacy hold?             -> his thread has an inbound "HOLD ... NNNN"

GO-LIVE CUTOFF: only orders whose calendar event was CREATED after GOLIVE_AFTER
are ever acted on, so the existing backlog of tentatives is grandfathered and
never blasted. Override with env GOLIVE_AFTER (ISO8601).

Recipients (Derrick 2026-06-29): chefs = Darin + Jon Jon + Nixon on every order.

CONFIRMED orders (paid online at checkout, or entered as Confirmed) get a one-time
"📣 NEW CATERING ORDER ✅ CONFIRMED" announcement to the same people instead of an
approval request (2026-10-25). Same dedup marker. Orders created before ANNOUNCE_AFTER
are never announced, and more than MAX_ANNOUNCE_PER_RUN at once are held for Derrick
(a Toast calendar re-sync re-creates old events).

THANKSGIVING (Derrick 2026-10-26): "Holiday special menus" orders NEVER go to the chef
lane. Instead:
  - each new Thanksgiving order -> management (Derrick, Keita, Adam, Zenn, Alisa) with the
    running total; orders created after the 11/20 cutoff are flagged AFTER CUTOFF
  - 11/21 9am -> final count to Darin + management (turkey/supply order)
  - 11/26 9am -> pickup sheet (time, name, phone, items) to management
Dedup markers live in Derrick's thread, same as the chef lane.

Usage:
  python3 catering_approval_poll.py --events events.json [--dry]
"""
import argparse, json, os, re, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ghl_send_sms import send_sms, get_recent_messages

HST = datetime.timezone(datetime.timedelta(hours=-10))
GOLIVE_AFTER = os.environ.get("GOLIVE_AFTER", "2026-06-29T21:05:00-10:00")
# Confirmed orders (paid online at checkout) used to be skipped entirely, so management
# never got a text for them (Kellie Shiraishi #001675). They now get a one-time announcement.
# Separate cutoff so the existing confirmed backlog is never blasted on go-live.
ANNOUNCE_AFTER = os.environ.get("ANNOUNCE_AFTER", "2026-10-25T15:00:00-10:00")
# More than this many brand-new confirmed orders in one run almost certainly means a Toast
# calendar re-sync re-created old events (it did on 10/23). Hold them for Derrick instead.
MAX_ANNOUNCE_PER_RUN = 3
HEARTBEAT_HOUR = 8  # one liveness ping/day, at ~8am HST

DERRICK = "a5VfxJQzMHa5SrdXXhcP"
KEITA = "znB4oBW3MVkvkeWwMuIE"
CHEFS = {"Darin": "peVEGm1t884L4bZKuhrJ", "Jon Jon": "ZBS9x5MyTMJNOL6ONlO9", "Nixon": "PQl9jt0IKdVD2qYFCFjQ"}
# Derrick first: his copy is the dedup marker for the Thanksgiving lane.
MANAGEMENT = {"Derrick": DERRICK, "Keita": KEITA, "Adam": "nwkHj14UyoZtLvRoV0bG",
              "Zenn": "164HKrXo2bWGiQI9PbH4", "Alisa": "3bB8JWj6tnXXtdvsqcXD"}

HOLIDAY = re.compile(r"Holiday special menus", re.I)
TG_DATE = datetime.date(2026, 11, 26)
# Thanksgiving orders created before this were never announced (James Small #001682); they
# still count in every running total.
TG_ANNOUNCE_AFTER = os.environ.get("TG_ANNOUNCE_AFTER", "2026-10-26T11:00:00-10:00")
TG_ORDER_CUTOFF = datetime.datetime(2026, 11, 21, 0, 0, tzinfo=HST)  # last day to order = 11/20
TG_FINAL_COUNT_DAY = datetime.date(2026, 11, 21)
TG_SEND_HOUR = 9  # final count + pickup sheet go out at ~9am HST
PHONE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")

TENTATIVE = re.compile(r"Catering order status:\s*(Tentative|Estimate|Open|Inquiry)", re.I)
CONFIRMED = re.compile(r"Catering order status:\s*Confirmed", re.I)
LEAD = re.compile(r"Lead status:", re.I)
# The inbox drafter routine rewrites an online order's calendar description into its own format
# ("Order #001692 | Pickup | ..."), which drops Toast's "Catering order status" line. Orders it
# reached before this poller were invisible here (Rachael Han #001692, 10/28). Drafter-format
# events are always paid online orders. The drafter puts either " | " or a line break after the
# order # ("Order #001703\nPickup | ..."); the pipe-only pattern missed Josiah Morita #001703, 10/29.
DRAFTER = re.compile(r"\A\s*Order #\d{4,}\s*(?:\||\n\s*(?:Pickup|Deliver|Take))", re.I)
DRAFTER_KIND = re.compile(r"\A\s*Order #\d{4,}\s*[|\n]\s*([^|\n]+?)\s*\|")
DRAFTER_CUST = re.compile(r"^Customer:\s*(.+)$", re.M)
DRAFTER_ADDR = re.compile(r"^(?:Deliver to|Delivery address):\s*(.+)$", re.M | re.I)
DELIVER_AT = re.compile(r"deliver at\s+([0-9:]+\s*[AP]M|TBD)", re.I)
REPLACES = re.compile(r"REPLACES\s+Order\s+#(\d{4,})", re.I)
CANCELLED = re.compile(r"^\s*CANCEL", re.I)
ORDERNO = re.compile(r"#(\d{4,})")
GUESTS = re.compile(r"Guest count:\s*(\d+)", re.I)
# hand-roll fillings too: Toast lists them by filling ("Spicy Tuna", "Imi Crab Mayo", "Negi Hamachi"), so
# without these a sushi-only order fell through to "Hot" (#001674, #001502).
SUSHI = re.compile(r"\b(sushi|sashimi|nigiri|maki|temaki|hand\s?roll|handroll|gunkan|chirashi|barazushi|matsuri"
                   r"|tuna|hamachi|salmon|imi\s?crab|tobiko|ikura|unagi|natt?ou?|kyuri)\b", re.I)
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
    if LEAD.search(desc):
        return None
    online = bool(DRAFTER.search(desc))
    if online or CONFIRMED.search(desc):
        status = "confirmed"
    elif TENTATIVE.search(desc):
        status = "tentative"
    else:
        return None
    if online:  # items = the "Items:" block, "1x Edamame (1/2 Pan)" -> "1 Edamame (1/2 Pan)"
        block = desc.split("Items:", 1)[1].split("\n\n", 1)[0] if "Items:" in desc else ""
        items = "\n".join(re.sub(r"^(\d+)x\s+", r"\1 ", l.strip()) for l in block.splitlines()
                          if re.match(r"\s*\d+x?\s+\S", l))
    else:
        items = "\n".join(l.strip() for l in desc.splitlines()
                          if re.match(r"\s*\d+\s+\S", l) and "Guest count" not in l)
    start = ev.get("start", {})
    cust = ev.get("summary", "").strip()
    m = re.search(r"Customer info:\s*\n([^\n]+)", desc) or (DRAFTER_CUST.search(desc) if online else None)
    if m:
        cust = m.group(1).strip()
    km = DRAFTER_KIND.search(desc) if online else None
    kind = km.group(1) if km else ""
    am, dm, rm = DRAFTER_ADDR.search(desc), DELIVER_AT.search(ev.get("summary", "")), REPLACES.search(desc)
    om, gm, pm = ORDERNO.search(desc), GUESTS.search(desc), PHONE.search(desc)
    return {"eventId": ev.get("id"), "created": ev.get("created"),
            "orderNo": om.group(1) if om else None, "customer": cust,
            "startDateTime": start.get("dateTime") or start.get("date"),
            "guests": gm.group(1) if gm else None, "itemsText": items,
            "dept": classify(items), "status": status,
            "holiday": bool(HOLIDAY.search(desc)), "phone": pm.group(0) if pm else "",
            "online": online, "kind": kind, "address": am.group(1).strip() if am else "",
            "deliverAt": dm.group(1).strip() if dm else "", "replaces": rm.group(1) if rm else None,
            "cancelled": bool(CANCELLED.search(ev.get("summary", "")))}


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


def _try_send(contact_id, message):
    """send_sms, but one failed recipient can't abort the run (send_sms sys.exits on HTTP errors)."""
    try:
        send_sms(contact_id, message)
        return True
    except (SystemExit, Exception) as e:
        print(f"SEND FAILED -> {contact_id}: {e}", file=sys.stderr)
        return False


def announce_text(o, today):
    day, ready = fmt_ready(o["startDateTime"])
    u = " 🔴 URGENT" if is_urgent(event_date(o["startDateTime"]), today) else ""
    return (f"📣 NEW CATERING ORDER ✅ CONFIRMED{u}\n{o['customer']} · {day} · ready {ready}\n"
            f"→ {o['dept']}: {summary(o['itemsText'])}\n#{o['orderNo']} · {head_count(o)}\n"
            f"Already confirmed, no approval needed. It's on the calendar. 🤙🏽")


def online_text(o):
    """Same layout as the manual announce in Catering-Orders/toast-order-processor-SKILL.md step 10."""
    dt = _parse(o["startDateTime"])
    day = dt.astimezone(HST).strftime("%a %-m/%-d") if dt else "TBD"
    ready = dt.astimezone(HST).strftime("%-I:%M %p") if dt else "TBD"
    if o["kind"].lower().startswith("deliver"):
        how = f"Delivery to {o['address'] or 'address on BEO'} at {o['deliverAt'] or 'TBD'}"
    else:
        how = o["kind"] or "Pickup"
    rep = (f"⚠️ REPLACES #{o['replaces']}. #{o['replaces']} is CANCELLED, do not make it. "
           f"Toss the #{o['replaces']} BEO.\n") if o["replaces"] else ""
    items = "\n".join(re.sub(r"\(1/2 Pan\)", "(1/2 pan)", l) for l in o["itemsText"].splitlines())
    guests = f"Guest count: {o['guests']}\n" if o.get("guests") and o["guests"] != "1" else ""
    return (f"🍱 NEW ONLINE CATERING ORDER (placed + paid online by the customer)\n"
            f"{o['customer']} · #{o['orderNo']} · {day}\n{rep}Ready by {ready}  ·  {how}\n"
            f"{items}\n{guests}Dept: {o['dept']}. BEO sent to the bar printer, no reply needed.")


def _start(o):
    return _parse(o["startDateTime"]) or datetime.datetime.max.replace(tzinfo=HST)


def tg_tally(orders):
    """Running totals: item counts (most first) + how many pickups per time slot."""
    items, slots = {}, {}
    for o in sorted(orders, key=_start):
        for line in o["itemsText"].splitlines():
            m = re.match(r"\s*(\d+)\s+(.+)", line)
            if m:
                items[m.group(2).strip()] = items.get(m.group(2).strip(), 0) + int(m.group(1))
        t = fmt_ready(o["startDateTime"])[1]
        slots[t] = slots.get(t, 0) + 1
    item_lines = "\n".join(f"{q} {n}" for n, q in sorted(items.items(), key=lambda kv: (-kv[1], kv[0])))
    pickups = " · ".join(f"{t} ({n})" for t, n in slots.items())
    return (f"Running total: {len(orders)} order{'s' if len(orders) != 1 else ''}\n"
            f"{item_lines}\nPickups: {pickups}")


def tg_new_text(o, orders):
    late = " ⚠️ AFTER CUTOFF (ordering closed 11/20)" if (_parse(o["created"]) or TG_ORDER_CUTOFF) >= TG_ORDER_CUTOFF else ""
    day, ready = fmt_ready(o["startDateTime"])
    return (f"🦃 NEW THANKSGIVING ORDER #{o['orderNo']}{late}\n{o['customer']} · pickup {day} {ready}\n"
            f"{summary(o['itemsText'], 300)}\n\n{tg_tally(orders)}")


def tg_final_text(orders):
    return (f"🦃 THANKSGIVING FINAL COUNT (ordering closed 11/20)\n"
            f"Pickups Thu 11/26, 1:00 to 4:30pm\n\n{tg_tally(orders)}\n\n"
            f"Darin, this is the number for turkeys + supplies. Late orders will still text management. 🤙🏽")


def tg_pickup_texts(orders, limit=1200):
    """Pickup sheet, split into several texts if it runs long."""
    rows = [f"{fmt_ready(o['startDateTime'])[1]} {o['customer']} {o['phone']}".strip()
            + f"\n  {summary(o['itemsText'], 200)}" + ("  (UNPAID)" if o["status"] != "confirmed" else "")
            for o in sorted(orders, key=_start)]
    chunks, cur = [], ""
    for r in rows:
        if cur and len(cur) + len(r) > limit:
            chunks.append(cur)
            cur = ""
        cur += r + "\n"
    chunks.append(cur)
    n = len(chunks)
    return [f"🦃 THANKSGIVING PICKUP SHEET{f' ({i}/{n})' if n > 1 else ''} · today 1:00 to 4:30pm · "
            f"{len(orders)} orders\n\n{c.rstrip()}" for i, c in enumerate(chunks, 1)]


def send_to_management(msg, extra=None):
    """Derrick FIRST (his copy is the dedup marker). Returns (marker_ok, failed_names)."""
    if not _try_send(DERRICK, msg):
        return False, []
    people = {k: v for k, v in {**MANAGEMENT, **(extra or {})}.items() if v != DERRICK}
    return True, [name for name, cid in people.items() if not _try_send(cid, msg)]


def thanksgiving_lane(tg, has, now, dry):
    """Thanksgiving pre-orders: never the chef lane. See module docstring."""
    actions, today = [], now.date()
    announce_after = _parse(TG_ANNOUNCE_AFTER)

    def deliver(label, msg, extra=None):
        if dry:
            actions.append(f"WOULD SEND {label}:\n" + "\n".join("      " + l for l in msg.splitlines()))
            return
        ok, failed = send_to_management(msg, extra)
        if not ok:
            actions.append(f"{label}: marker to Derrick FAILED, sent nothing, retry next hour")
        elif failed:
            _try_send(DERRICK, f"⚠️ {label} did NOT reach: {', '.join(failed)}. Please forward it.")
            actions.append(f"{label}: SENT, but FAILED to {', '.join(failed)} ⚠️")
        else:
            actions.append(f"{label}: SENT to management ✅")

    fresh = [o for o in tg if (_parse(o["created"]) or announce_after) > announce_after
             and not has("NEW THANKSGIVING ORDER", o["orderNo"])]
    if len(fresh) > MAX_ANNOUNCE_PER_RUN:  # calendar re-sync guard, same as the chef lane
        nos = " ".join(f"#{o['orderNo']}" for o in fresh)
        deliver_hold = (f"🦃 NEW THANKSGIVING ORDER hold (NOT sent to management) {nos}\n"
                        f"{len(fresh)} Thanksgiving orders showed up at once, probably a Toast calendar "
                        f"re-sync. I did not text management. Forward any that are actually new.")
        if dry:
            actions.append(f"WOULD HOLD {len(fresh)} Thanksgiving orders for Derrick: {nos}")
        else:
            _try_send(DERRICK, deliver_hold)
            actions.append(f"HELD {len(fresh)} Thanksgiving orders for Derrick (bulk guard): {nos}")
        fresh = []
    for o in sorted(fresh, key=lambda o: o["created"] or ""):
        deliver(f"Thanksgiving #{o['orderNo']} {o['customer']}", tg_new_text(o, tg))

    if today == TG_FINAL_COUNT_DAY and now.hour >= TG_SEND_HOUR and not has("THANKSGIVING FINAL COUNT"):
        deliver("Thanksgiving final count", tg_final_text(tg), extra={"Darin": CHEFS["Darin"]})
    if today == TG_DATE and now.hour >= TG_SEND_HOUR and tg and not has("THANKSGIVING PICKUP SHEET"):
        for i, msg in enumerate(tg_pickup_texts(tg), 1):
            deliver(f"Thanksgiving pickup sheet part {i}", msg)
    return actions


def _one_per_order(orders):
    """One entry per order #, preferring a confirmed copy over a tentative one."""
    by_no = {}
    for o in orders:
        kept = by_no.get(o["orderNo"])
        if kept is None or (kept["status"] != "confirmed" and o["status"] == "confirmed"):
            by_no[o["orderNo"]] = o
    return list(by_no.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--now", help="pretend it's this HST time (ISO8601), for testing the dated sends")
    a = ap.parse_args()

    raw = json.load(open(a.events))
    events = raw.get("events", raw) if isinstance(raw, dict) else raw
    now = (datetime.datetime.fromisoformat(a.now).replace(tzinfo=HST) if a.now
           else datetime.datetime.now(HST))
    today = now.date()
    golive = _parse(GOLIVE_AFTER)
    announce_after = _parse(ANNOUNCE_AFTER)

    # candidates = tentatives created after go-live, plus confirmed orders created after the
    # announce cutoff that haven't happened yet (each backlog grandfathered by its cutoff)
    cands, tg = [], []
    for ev in events:
        o = parse_event(ev)
        if not o or not o["orderNo"] or o["cancelled"]:
            continue
        if o["holiday"]:  # holiday menus never go to the chefs; Thanksgiving gets its own lane
            if event_date(o["startDateTime"]) == TG_DATE:
                tg.append(o)
            continue
        c = _parse(o["created"])
        cutoff = announce_after if o["status"] == "confirmed" else golive
        if c is None or (cutoff and c <= cutoff):
            continue
        if o["status"] == "confirmed":
            d = event_date(o["startDateTime"])
            if d is None or d < today:
                continue
        cands.append(o)

    # Toast sometimes writes the same order to the calendar twice in the same second (e.g. sent
    # then confirmed right away, Shimomura #001689 on 10/27). The thread marker only dedups across
    # runs, so collapse same-order copies here, keeping the confirmed one if they differ.
    cands, tg = _one_per_order(cands), _one_per_order(tg)

    # read Derrick's thread ONCE; derive state per order from it.
    # 200, not 50: an order can sit for days between the heads-up and approval, and
    # this thread is busy (call recaps, omakase alerts, CCs). Too small a window
    # makes the "already sent" marker scroll out and the chefs get blasted again.
    derrick_msgs = get_recent_messages(DERRICK, limit=200)

    # FAIL SAFE (do not remove): an empty read means we could not SEE the thread,
    # it does NOT mean nothing was ever sent. Without this guard, one transient GHL
    # hiccup makes every marker look absent and this script re-sends every pending
    # order to every chef. Silence is the safe failure here.
    if not derrick_msgs:
        print("ABORT: could not read Derrick's GHL thread (empty response). "
              "Sending NOTHING this run so already-approved orders are not re-blasted. "
              "Will retry next hour.")
        return
    def sent(no):
        # poller marker, or a manual announce (local order-processor skill step 10) CC'd to Derrick
        return has("CC (SENT", no) or has("NEW ONLINE CATERING ORDER", f"#{no}", direction="outbound")

    def has(*subs, direction=None):
        for m in derrick_msgs:
            if direction and m["direction"] != direction:
                continue
            body = m["body"].upper()
            if all(s.upper() in body for s in subs):
                return True
        return False

    actions = []
    fresh_confirmed = [o for o in cands if o["status"] == "confirmed" and not sent(o["orderNo"])]
    if len(fresh_confirmed) > MAX_ANNOUNCE_PER_RUN:
        nos = " ".join(f"#{o['orderNo']}" for o in fresh_confirmed)
        hold = (f"📋 CC (sent: bulk hold, NOT sent to chefs) {nos}\n"
                f"{len(fresh_confirmed)} confirmed orders showed up at once, probably a Toast calendar "
                f"re-sync of old orders. I did not text the chefs. Forward any that are actually new.")
        if a.dry:
            actions.append(f"WOULD HOLD {len(fresh_confirmed)} confirmed orders for Derrick: {nos}")
        elif _try_send(DERRICK, hold):
            actions.append(f"HELD {len(fresh_confirmed)} confirmed orders for Derrick (bulk guard): {nos}")
        else:
            actions.append(f"bulk-hold note to Derrick FAILED, sent nothing, retry next hour: {nos}")
        held = {o["orderNo"] for o in fresh_confirmed}
        cands = [o for o in cands if o["orderNo"] not in held]

    for o in cands:
        no = o["orderNo"]
        if sent(no):
            actions.append(f"#{no} {o['customer']}: already sent to chefs — skip")
            continue
        if has("HOLD", no, direction="inbound"):  # legacy holds from the old heads-up flow
            actions.append(f"#{no} {o['customer']}: HOLD by Derrick — held")
            continue
        # first sighting -> straight to the chefs, CC Derrick + Keita (no heads-up wait).
        # Confirmed orders get an announcement instead of an approval request.
        if o["status"] != "confirmed":
            msg = approval_text(o, today)
        else:
            msg = online_text(o) if o["online"] else announce_text(o, today)
        kind = "announcement" if o["status"] == "confirmed" else "approval"
        cc = f"📋 CC (sent to Darin/Jon Jon/Nixon) #{no}\n" + msg
        if a.dry:
            actions.append(f"#{no} {o['customer']}: WOULD SEND {kind} to chefs now")
        else:
            # Derrick's CC IS the dedup marker, so it goes FIRST. If it can't be written, send
            # nothing this run (retry next hour) rather than text the chefs with no marker,
            # which would re-text every chef every hour (e.g. if Derrick's contact is DND).
            if not _try_send(DERRICK, cc):
                actions.append(f"#{no} {o['customer']}: marker to Derrick FAILED, sent nothing, retry next hour")
                continue
            failed = [name for name, cid in CHEFS.items() if not _try_send(cid, msg)]
            _try_send(KEITA, cc)
            if failed:
                _try_send(DERRICK, f"⚠️ Catering approval #{no} did NOT reach: {', '.join(failed)}. "
                                   f"Please forward it to them.")
                actions.append(f"#{no} {o['customer']}: SENT, but FAILED to {', '.join(failed)} ⚠️")
            else:
                actions.append(f"#{no} {o['customer']}: SENT {kind} to chefs ✅")

    actions += thanksgiving_lane(tg, has, now, a.dry)

    # report
    print(f"Ran {now.strftime('%Y-%m-%d %H:%M HST')}. {len(events)} events, "
          f"{len(cands)} live candidate(s) (tentative after {GOLIVE_AFTER}, "
          f"confirmed after {ANNOUNCE_AFTER}). {len(tg)} Thanksgiving order(s) on 11/26.")
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
