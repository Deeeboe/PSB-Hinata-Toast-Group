#!/usr/bin/env python3
"""
ghl_send_sms.py — send an SMS through GoHighLevel (LeadConnector REST API).

House pattern: creds come from ENV first (GHL_PIT, GHL_LOCATION_ID,
GHL_FROM_NUMBER) so the cloud routine can inject them as env vars, then fall
back to .vault/ghl.env for local runs. Secret NEVER printed.

Usage:
  python3 Integrations/GHL/ghl_send_sms.py --contact <contactId> --message "hi" [--from +1808...] [--dry] [--debug]

Exit 0 on success, non-zero on failure (no silent catch — see memory
pitch-script-debug-rule). Returns/prints the GHL conversationId + messageId.
"""
import argparse, json, os, sys, urllib.request, urllib.error

API = "https://services.leadconnectorhq.com/conversations/messages"
API_VERSION = "2021-04-15"


def _load_vault(path=".vault/ghl.env"):
    """Fill any missing GHL_* env var from the local vault file (local runs)."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def get_creds():
    _load_vault()
    pit = os.environ.get("GHL_PIT", "").strip()
    frm = os.environ.get("GHL_FROM_NUMBER", "").strip()
    if not pit:
        sys.exit("ERROR: GHL_PIT not set (env or .vault/ghl.env).")
    return pit, frm


def send_sms(contact_id, message, from_number=None, dry=False, debug=False):
    pit, frm = get_creds()
    from_number = from_number or frm
    payload = {"type": "SMS", "contactId": contact_id, "message": message}
    if from_number:
        payload["fromNumber"] = from_number

    if dry:
        masked = pit[:4] + "…" + pit[-4:]
        print(f"[DRY] would POST to {contact_id} from {from_number or '(default)'} "
              f"(auth {masked}):\n{message}")
        return {"dry": True}

    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {pit}",
            "Version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            # GHL's Cloudflare bans the default python-urllib UA (error 1010) —
            # send a normal browser UA.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
            print(f"OK {r.status}: conversationId={body.get('conversationId')} "
                  f"messageId={body.get('messageId')}")
            return body
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if debug:
            print(f"HTTP {e.code} for contact {contact_id}\n{detail}", file=sys.stderr)
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
