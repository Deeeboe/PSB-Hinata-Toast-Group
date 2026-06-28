#!/usr/bin/env python3
"""Toast Data Export — daily SFTP auto-pull for PITCH.

Mirrors the Toast Data Export S3 bucket (item sales, checks, orders, labor,
payments, menu, etc.) into a local folder so the BOH ordering/PAR engine,
finance/weekly-ops, demand radar, and staff-performance tools can read it.

Auth is an SSH KEY (no password). Uses the system `sftp` binary (no pip
install — matches the stdlib-only house style of toast_client.py). Toast's
SFTP is an AWS Transfer (S3) gateway, so `rsync` and `ls -R` are NOT
supported; we drive `sftp -b` batch mode and walk the tree ourselves.

Remote layout (confirmed 2026-06-27):
    <RESTAURANT_ID>/<YYYYMMDD>/<files>     e.g. 86897/20260627/ItemSelectionDetails.csv
One dated folder per business day, dropped ~daily.

Credentials come from .vault/toast-sftp.env (gitignored, never in chat):
    TOAST_SFTP_HOST=...        # "Server URL" from Toast Web -> Reports -> Settings -> SSH Keys
    TOAST_SFTP_USER=...        # "Data Export user" from the Restaurant Setup page
    TOAST_SFTP_KEY=.vault/toast_sftp   # private key path (default if omitted)
    TOAST_SFTP_PORT=22         # optional, default 22

Usage:
    python3 toast_export_pull.py --list             # show the remote date folders
    python3 toast_export_pull.py                     # pull the LATEST date folder
    python3 toast_export_pull.py --date 20260627     # pull one specific date
    python3 toast_export_pull.py --all               # mirror every date folder
    python3 toast_export_pull.py --debug             # verbose: prints the sftp command + -v output

Per house rule (pitch-script-debug-rule): real errors surface — nothing is
silently swallowed, and --debug shows exactly what ran.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE       = Path(__file__).resolve().parent
REPO       = HERE.parent.parent
VAULT      = REPO / ".vault"
ENV_FILE   = VAULT / "toast-sftp.env"
LOCAL_DIR  = HERE / "exports"                 # gitignored — export data, not source
KNOWN_HOSTS = VAULT / "toast_sftp_known_hosts"  # pins Toast's host key after first accept

# Lines AWS Transfer / OpenSSH print that are just noise in our output.
_NOISE = ("post-quantum", "store now", "may need to be upgraded",
          "openssh.com/pq", "Permanently added", "Connected to")


def load_env():
    """Read .vault/toast-sftp.env. Env vars (TOAST_SFTP_*) seed it for cloud runs;
    the file overrides when present. Mirrors toast_client.load_env() behavior."""
    env = {k: v for k, v in os.environ.items() if k.startswith("TOAST_SFTP_")}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return env


def resolve_key(env):
    # Cloud runs have no .vault: materialize the key from an env var to a temp
    # 600 file. PREFER base64 (TOAST_SFTP_KEY_B64) — single line, immune to the
    # newline mangling that corrupts raw multi-line keys passed via secrets.
    import tempfile
    b64 = env.get("TOAST_SFTP_KEY_B64", "")
    if b64.strip():
        import base64
        tmp = Path(tempfile.gettempdir()) / "toast_sftp_key"
        tmp.write_bytes(base64.b64decode("".join(b64.split())))
        tmp.chmod(0o600)
        return tmp
    contents = env.get("TOAST_SFTP_KEY_CONTENTS", "")
    if contents.strip():
        tmp = Path(tempfile.gettempdir()) / "toast_sftp_key"
        tmp.write_text(contents.replace("\\n", "\n").rstrip() + "\n")
        tmp.chmod(0o600)
        return tmp
    raw = env.get("TOAST_SFTP_KEY", ".vault/toast_sftp")
    p = Path(raw)
    if not p.is_absolute():
        p = REPO / raw
    return p


def build_sftp_cmd(env, debug):
    host = env.get("TOAST_SFTP_HOST", "").strip()
    user = env.get("TOAST_SFTP_USER", "").strip()
    port = env.get("TOAST_SFTP_PORT", "22").strip()
    key  = resolve_key(env)

    missing = [k for k in ("TOAST_SFTP_HOST", "TOAST_SFTP_USER") if not env.get(k, "").strip()]
    if missing:
        sys.exit(f"❌ {ENV_FILE} is missing: {', '.join(missing)}.\n"
                 f"   HOST = 'Server URL' (SSH Keys page); USER = 'Data Export user' (Restaurant Setup page).")
    if not key.exists():
        sys.exit(f"❌ Private key not found: {key}  (expected the keypair generated 2026-06-26).")

    cmd = ["sftp"]
    if debug:
        cmd.append("-v")
    cmd += [
        "-b", "-",                                   # read batch commands from stdin
        "-i", str(key),
        "-P", port,
        "-o", "BatchMode=yes",                       # never prompt (key-only, unattended)
        "-o", "ConnectTimeout=30",                   # fail fast if egress to SFTP is blocked
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "StrictHostKeyChecking=accept-new",    # accept Toast's host key on first run, pin after
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        f"{user}@{host}",
    ]
    return cmd


def run(env, batch_script, debug, allow_fail=False):
    # Cloud clones have no .vault dir, so the known_hosts path can't be written
    # and sftp aborts. Ensure the parent exists (ephemeral in cloud, that's fine).
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_sftp_cmd(env, debug)
    if debug:
        print("→ sftp command:", " ".join(cmd), file=sys.stderr)
        print("→ batch:\n" + batch_script, file=sys.stderr)
    # Hard timeout so a blocked-egress hang fails fast instead of running forever.
    proc = subprocess.run(cmd, input=batch_script, capture_output=True, text=True,
                          timeout=120)
    if proc.returncode != 0 and not allow_fail:
        # surface the real error — do NOT swallow (house rule)
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        sys.exit(f"❌ sftp exited {proc.returncode}. Re-run with --debug for the full handshake.\n"
                 f"   Common causes: wrong Server URL/user, key not yet added in Toast Web, "
                 f"or Data Export not enabled for the location.")
    if debug and proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return proc.stdout


def _clean(text):
    return "\n".join(l for l in text.splitlines()
                     if l.strip() and not any(n in l for n in _NOISE))


def list_names(env, remote_path, debug):
    """Return the entry names under a remote dir (one `ls -1` call)."""
    out = run(env, f"ls -1 {remote_path}\n", debug)
    names = []
    for line in _clean(out).splitlines():
        line = line.strip()
        if line.startswith("sftp>") or line.endswith(":"):
            continue
        # ls -1 may print "dir/name"; keep just the leaf
        names.append(line.rstrip("/").split("/")[-1])
    return [n for n in names if n]


def restaurant_dir(env, debug):
    """The single <RESTAURANT_ID> folder at the SFTP root."""
    roots = list_names(env, ".", debug)
    if not roots:
        sys.exit("❌ Connected, but the SFTP root is empty. Is Data Export enabled + has it run yet?")
    return roots[0]


def date_folders(env, debug):
    rid = restaurant_dir(env, debug)
    dates = sorted(d for d in list_names(env, rid, debug) if d.isdigit() and len(d) == 8)
    return rid, dates


def pull_date(env, rid, date, debug):
    dest = LOCAL_DIR / date
    dest.mkdir(parents=True, exist_ok=True)
    script = f'lcd "{dest}"\ncd {rid}/{date}\nget *\n'
    run(env, script, debug)
    got = sorted(p.name for p in dest.iterdir() if p.is_file())
    print(f"  ✅ {date}: {len(got)} files → {dest}")
    return got


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    env = load_env()

    if "--list" in args:
        rid, dates = date_folders(env, debug)
        print(f"Restaurant folder: {rid}")
        print(f"Date folders available ({len(dates)}):")
        for d in dates:
            print(f"  {d}")
        if dates:
            print(f"\nLatest: {dates[-1]}.  Pull it with:  python3 {Path(__file__).name}")
        return

    rid, dates = date_folders(env, debug)
    if not dates:
        sys.exit("❌ No date folders found yet under the restaurant folder.")

    if "--all" in args:
        print(f"Mirroring all {len(dates)} date folders…")
        for d in dates:
            pull_date(env, rid, d, debug)
    elif "--date" in args:
        i = args.index("--date")
        if i + 1 >= len(args):
            sys.exit("❌ --date needs a value, e.g. --date 20260627")
        d = args[i + 1]
        if d not in dates:
            sys.exit(f"❌ {d} not on the server. Available: {', '.join(dates)}")
        print(f"Pulling {d}…")
        pull_date(env, rid, d, debug)
    else:
        latest = dates[-1]
        print(f"Pulling latest date folder: {latest}…")
        pull_date(env, rid, latest, debug)

    print(f"\n✅ Done. Local data lives in {LOCAL_DIR}")


if __name__ == "__main__":
    main()
