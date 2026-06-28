#!/usr/bin/env python3
"""Toast API helper for PITCH.

Loads credentials from ../.vault/toast.env (never hardcoded), authenticates against
the Toast API, and CACHES each credential's bearer token (per client id) so we respect
Toast's rate guidance (<=2 logins/hour/credential, reuse token, token lives ~hours-day).
Stdlib-only — no pip install needed.

PITCH uses TWO credentials:
  - Analytics  (TOAST_CLIENT_ID / TOAST_CLIENT_SECRET)         -> /era/* sales + restaurant list
  - Standard   (TOAST_STD_CLIENT_ID / TOAST_STD_CLIENT_SECRET) -> /orders/* order-level data

    token = get_token(creds)                                   # analytics (default)
    token = get_token(creds, "TOAST_STD_CLIENT_ID", "TOAST_STD_CLIENT_SECRET")  # standard
"""
import http.client
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

VAULT     = Path(__file__).resolve().parent.parent / ".vault"
VAULT_ENV = VAULT / "toast.env"
EXPIRY_SKEW_SECONDS = 120


def load_env(path=VAULT_ENV):
    """Load creds. Base = TOAST_* environment variables (for CLOUD runs, where there is
    no local vault). The .vault/toast.env file OVERRIDES env vars when present (LOCAL runs).
    Tolerant of a missing file so the same code runs both places."""
    creds = {k: v for k, v in os.environ.items() if k.startswith("TOAST_")}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            creds[key.strip()] = val.strip()
    except FileNotFoundError:
        pass  # cloud run: no local vault, rely on TOAST_* env vars
    return creds


def _post(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": "PITCH-Toast-Client/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _cache_path(client_id):
    return VAULT / f".toast_token_{client_id[:10]}.json"


def _login(creds, id_key, secret_key):
    """Hit Toast's auth endpoint for the given credential. Rate-limited!"""
    host = creds.get("TOAST_API_HOST", "https://ws-api.toasttab.com")
    url = f"{host}/authentication/v1/authentication/login"
    payload = {
        "clientId": creds[id_key],
        "clientSecret": creds[secret_key],
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }
    # Retry transient connection drops. NOTE: Toast enforces <=2 logins/hour/credential
    # and signals over-limit by DROPPING the connection (surfaces as RemoteDisconnected),
    # not a clean 4xx. The token cache (get_token) is the real fix — avoid force_login.
    last_err = None
    for attempt in range(3):
        try:
            body = _post(url, payload)
            break
        except urllib.error.HTTPError:
            raise  # real auth error with a status code — retrying won't help
        except (http.client.RemoteDisconnected, ConnectionError,
                urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(
            f"Toast login connection dropped after retries ({type(last_err).__name__}: {last_err}). "
            "Usually a transient blip OR Toast's rate limit (<=2 logins/hour/credential) closing the "
            "connection. Reuse the cached token (don't force_login); if needed, wait ~30 min."
        )
    tok = body.get("token", {})
    if isinstance(tok, dict):
        token = tok.get("accessToken") or tok.get("access_token")
        expires_in = tok.get("expiresIn") or tok.get("expires_in") or 86400
    else:
        token = tok or body.get("access_token")
        expires_in = body.get("expires_in", 86400)
    if not token:
        raise RuntimeError(f"No access token in response: {body}")
    return token, time.time() + float(expires_in)


def get_token(creds=None, id_key="TOAST_CLIENT_ID", secret_key="TOAST_CLIENT_SECRET", force_login=False):
    """Return a valid bearer token for the named credential, reusing its cached token
    until it nears expiry. Each credential caches separately (keyed by client id)."""
    creds = creds or load_env()
    client_id = creds.get(id_key, "")
    if not client_id:
        raise RuntimeError(f"{id_key} is blank in .vault/toast.env")
    cache = _cache_path(client_id)
    if not force_login and cache.exists():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("token") and cached.get("expires_at", 0) - EXPIRY_SKEW_SECONDS > time.time():
                return cached["token"]
        except Exception:
            pass  # corrupt cache -> re-login
    token, expires_at = _login(creds, id_key, secret_key)
    try:
        cache.write_text(json.dumps({"token": token, "expires_at": expires_at}))
        cache.chmod(0o600)
    except Exception:
        pass  # caching is best-effort
    return token


# Back-compat alias (analytics credential, forced login).
def authenticate(creds):
    return get_token(creds, force_login=True)


if __name__ == "__main__":
    creds = load_env()
    which = ("TOAST_STD_CLIENT_ID", "TOAST_STD_CLIENT_SECRET") if "--std" in sys.argv \
        else ("TOAST_CLIENT_ID", "TOAST_CLIENT_SECRET")
    try:
        token = get_token(creds, which[0], which[1])
        print(f"✅ {which[0]} token ready ({len(token)} chars)")
    except urllib.error.HTTPError as e:
        print(f"❌ LOGIN FAILED — HTTP {e.code}: {e.read().decode()[:200]}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)
