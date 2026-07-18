#!/usr/bin/env python3
"""
ghl_send_sms.py — GoHighLevel (LeadConnector REST) SMS send + thread read.

Creds from ENV first (GHL_PIT, GHL_LOCATION_ID, GHL_FROM_NUMBER) so the cloud
routine injects them as env vars; falls back to .vault/ghl.env locally. Secret
never printed.

CLI (send): python3 ghl_send_sms.py --contact <id> --message "hi" [--from +1808...] [--dry] [--debug]

Library:
  send_sms(contact_id, message, from_number=None, dry=False)
  get_recent_messages(contact_id, limit=30) -> [{"direction","body"}]
  thread_has(contact_id, *substrings, direction=None, limit=30) -> bool
"""
import argparse, json, os, sys, urllib.request, urllib.error

BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-04-15"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")  # GHL Cloudflare bans python-urllib UA


def _load_vault(path=".vault/ghl.env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _creds():
    _load_vault()
    pit = os.environ.get("GHL_PIT", "").strip()
    if not pit:
        sys.exit("ERROR: GHL_PIT not set (env or .vault/ghl.env).")
    return pit, os.environ.get("GHL_LOCATION_ID", "").strip(), os.environ.get("GHL_FROM_NUMBER", "").strip()


def _headers(pit):
    return {"Authorization": f"Bearer {pit}", "Version": API_VERSION,
            "Accept": "application/json", "User-Agent": UA}


def _get(url, pit):
    req = urllib.request.Request(url, headers=_headers(pit))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: GHL GET failed HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: GHL GET network failure: {e}")


def get_recent_messages(contact_id, limit=30):
    """Return up to `limit` recent messages for a contact's conversation:
    [{direction, body}].

    PAGINATES. The messages endpoint returns ~20 per page by default, and the old
    version just sliced that single page — so `limit=50` silently gave you ~20.
    On a busy thread that window is only a few hours, which made dedup markers
    disappear and caused duplicate catering-approval blasts to the chefs.
    """
    pit, loc, _ = _creds()
    s = _get(f"{BASE}/conversations/search?locationId={loc}&contactId={contact_id}", pit)
    convs = s.get("conversations", []) if isinstance(s, dict) else []
    if not convs:
        return []
    conv_id = convs[0]["id"]
    out, last_id, guard = [], None, 0
    while len(out) < limit and guard < 12:
        url = f"{BASE}/conversations/{conv_id}/messages?limit=100"
        if last_id:
            url += f"&lastMessageId={last_id}"
        m = _get(url, pit)
        arr = m.get("messages", {})
        arr = arr.get("messages", []) if isinstance(arr, dict) else (arr if isinstance(arr, list) else [])
        if not arr:
            break
        for msg in arr:
            out.append({"direction": msg.get("direction"), "body": msg.get("body") or ""})
        nxt = arr[-1].get("id")
        if not nxt or nxt == last_id:
            break
        last_id = nxt
        guard += 1
    return out[:limit]


def thread_has(contact_id, *substrings, direction=None, limit=30):
    """True if any recent message contains ALL substrings (optionally filtered by direction)."""
    for m in get_recent_messages(contact_id, limit):
        if direction and m["direction"] != direction:
            continue
        body = m["body"].upper()
        if all(s.upper() in body for s in substrings):
            return True
    return False


def send_sms(contact_id, message, from_number=None, dry=False, debug=False):
    pit, _, frm = _creds()
    from_number = from_number or frm
    if dry:
        print(f"[DRY] -> {contact_id}: {message[:60]}...")
        return {"dry": True}
    payload = {"type": "SMS", "contactId": contact_id, "message": message}
    if from_number:
        payload["fromNumber"] = from_number
    req = urllib.request.Request(f"{BASE}/conversations/messages",
                                 data=json.dumps(payload).encode(),
                                 headers={**_headers(pit), "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
            print(f"OK {r.status}: conv={body.get('conversationId')} msg={body.get('messageId')}")
            return body
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if debug:
            print(f"HTTP {e.code} for {contact_id}\n{detail}", file=sys.stderr)
        sys.exit(f"ERROR: GHL send failed HTTP {e.code}: {detail[:400]}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: GHL send network failure: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--from", dest="from_number", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    send_sms(a.contact, a.message, a.from_number, a.dry, a.debug)
