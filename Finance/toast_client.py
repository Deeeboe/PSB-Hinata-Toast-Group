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
import hashlib
import http.client
import json
import os
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

VAULT     = Path(__file__).resolve().parent.parent / ".vault"
VAULT_ENV = VAULT / "toast.env"
EXPIRY_SKEW_SECONDS = 120
MAX_LOGINS_PER_HOUR = 2   # Toast's documented limit per credential


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


def _cache_dir():
    """Where token caches live. LOCAL: the .vault (gitignored). CLOUD: no vault exists, so
    the old code's write silently failed and every run re-logged in (burning the <=2/hr budget);
    fall back to a writable cache dir instead. Overridable with TOAST_CACHE_DIR. Always created."""
    override = os.environ.get("TOAST_CACHE_DIR")
    if override:
        d = Path(override)
    elif VAULT.exists():
        d = VAULT
    else:
        d = Path.home() / ".cache" / "pitch-toast"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[toast] cache dir {d} not creatable ({e}); using temp dir", file=sys.stderr)
        d = Path(tempfile.gettempdir())
    return d


def _cache_path(client_id):
    # hash the FULL id (client_id[:10] could collide between two credentials)
    h = hashlib.sha1(client_id.encode()).hexdigest()[:16]
    return _cache_dir() / f".toast_token_{h}.json"


def _read_cache(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_cache(path, data):
    """Atomic write (temp file + os.replace) so a concurrent reader never sees a torn file.
    Non-fatal on failure, but LOUD to stderr (no silent swallow, per the repo's ?debug rule)."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tok", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)   # atomic rename
        tmp = None
    except Exception as e:
        print(f"[toast] token cache write failed ({e}); continuing without cache", file=sys.stderr)
    finally:
        if tmp and os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass


def _recent_logins(cached, now):
    return [t for t in (cached.get("logins") or []) if now - t < 3600]


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
        raise RuntimeError(f"{id_key} is blank (checked .vault/toast.env and TOAST_* env vars)")
    path = _cache_path(client_id)
    cached = _read_cache(path)
    now = time.time()
    if not force_login and cached.get("token") \
            and cached.get("expires_at", 0) - EXPIRY_SKEW_SECONDS > now:
        return cached["token"]

    # Rate guard: refuse a login that would exceed Toast's <=2/hour/credential, rather than
    # attempting a 3rd that Toast rejects by dropping the connection (which also burns the budget).
    recent = _recent_logins(cached, now)
    if len(recent) >= MAX_LOGINS_PER_HOUR:
        wait = int((3600 - (now - min(recent))) / 60) + 1
        raise RuntimeError(
            f"Refusing to log in: {len(recent)} logins in the last hour already hit Toast's "
            f"<=2/hour/credential limit. Reuse the cached token or wait ~{wait} min."
        )

    token, expires_at = _login(creds, id_key, secret_key)
    _write_cache(path, {"token": token, "expires_at": expires_at, "logins": recent + [now]})
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
