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
    python3 toast_export_pull.py                     # SYNC: pull every day we're missing
    python3 toast_export_pull.py --date 20260627     # pull one specific date
    python3 toast_export_pull.py --all               # force re-pull of every date folder
    python3 toast_export_pull.py --debug             # verbose: prints the sftp command + -v output

⚠️ WHY SYNC IS THE DEFAULT (fixed 2026-07-13 — this bug cost us a week of sales data):
Toast keeps only ~7 days of date folders on the SFTP server. The old default pulled
ONLY the latest folder, so any missed/failed run meant that day was skipped and then
rotated off the server FOREVER. It also mkdir'd the destination before pulling, so a
failed pull left an EMPTY folder behind that looked "present" and was never retried.
Result: 7/7, 7/10 and 7/12 never landed, 7/9 was an empty shell, and the weekly report
couldn't answer net sales.

Now the default compares every remote date against what's on disk and pulls anything
missing OR incomplete (fewer files locally than remotely — which covers the empty-shell
case). That makes the job self-healing: one bad night is repaired by the next run, as
long as it happens inside Toast's retention window. Days that fall outside that window
are reported as PERMANENTLY LOST so they never silently read as zero.

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


# TWO locations live at the SFTP root — this matters more than it looks:
#   86897  = PITCH Sports Bar   (the floor: bar, kitchen, sushi — the real sales)
#   230312 = Hinata Catering    (catering / mobile sushi / bento only — a few orders a day)
# The old code did `roots[0]`, which sorts to 230312, so for weeks we pulled HINATA
# and stored it as if it were PITCH. Every sales read came back ~$0 and looked like a
# broken feed. It wasn't — it was the wrong restaurant. Never index into roots again.
LOCATIONS = {
    "86897":  {"name": "PITCH",  "dest": lambda: LOCAL_DIR},
    "230312": {"name": "Hinata", "dest": lambda: LOCAL_DIR / "hinata"},
}
PITCH_ID = "86897"


def restaurant_dirs(env, debug):
    """Every <RESTAURANT_ID> folder at the SFTP root, mapped to its local destination."""
    roots = list_names(env, ".", debug)
    if not roots:
        sys.exit("❌ Connected, but the SFTP root is empty. Is Data Export enabled + has it run yet?")
    unknown = [r for r in roots if r not in LOCATIONS]
    if unknown:
        print(f"⚠️  Unrecognized location folder(s) on the server: {', '.join(unknown)} — "
              f"add them to LOCATIONS in this file or they'll be skipped.", file=sys.stderr)
    known = [r for r in roots if r in LOCATIONS]
    if PITCH_ID not in known:
        sys.exit(f"❌ PITCH's folder ({PITCH_ID}) is NOT on the server. Found: {', '.join(roots)}.\n"
                 f"   Refusing to run — pulling only Hinata is what caused the $0-sales bug.")
    return known


def dest_for(rid):
    return LOCATIONS[rid]["dest"]()


def date_folders(env, rid, debug):
    return sorted(d for d in list_names(env, rid, debug) if d.isdigit() and len(d) == 8)


def pull_date(env, rid, date, debug, expected=None):
    dest = dest_for(rid) / date
    dest.mkdir(parents=True, exist_ok=True)
    script = f'lcd "{dest}"\ncd {rid}/{date}\nget *\n'
    run(env, script, debug)
    got = sorted(p.name for p in dest.iterdir() if p.is_file())
    if expected is not None and len(got) < expected:
        # Don't pretend this worked. Leaving the partial folder is fine — the next
        # sync sees the short count and retries it.
        print(f"  ⚠️  {date}: got {len(got)} of {expected} files — INCOMPLETE, will retry next run")
    else:
        print(f"  ✅ {date}: {len(got)} files → {dest}")
    return got


def local_count(rid, date):
    d = dest_for(rid) / date
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.is_file())


def sync(env, rid, dates, debug):
    """Pull every remote date for this location we don't already have complete on disk.

    A local folder counts as complete only when it holds at least as many files as
    the remote one. That single check covers both failure modes we've actually hit:
    the folder never being pulled, and a failed pull leaving an empty shell behind.
    """
    label = LOCATIONS[rid]["name"]
    print(f"\n=== {label} ({rid}) → {dest_for(rid)} ===")
    print(f"Syncing {len(dates)} remote date folders against local…")
    pulled, ok, incomplete = [], [], []

    for d in dates:
        remote_n = len(list_names(env, f"{rid}/{d}", debug))
        have = local_count(rid, d)
        if have >= remote_n and remote_n > 0:
            ok.append(d)
            if debug:
                print(f"  [debug] {d}: have {have}/{remote_n} — up to date", file=sys.stderr)
            continue
        why = "missing" if have == 0 else f"incomplete ({have}/{remote_n})"
        print(f"  ↓ {d}: {why} — pulling")
        got = pull_date(env, rid, d, debug, expected=remote_n)
        (pulled if len(got) >= remote_n else incomplete).append(d)

    print(f"  up to date : {len(ok)}")
    print(f"  backfilled : {len(pulled)}" + (f"  → {', '.join(pulled)}" if pulled else ""))
    if incomplete:
        print(f"  ⚠️ INCOMPLETE: {', '.join(incomplete)} — will retry next run")

    report_lost(rid, dates)
    return pulled


def report_lost(rid, remote_dates):
    """Days that are gone: not on disk, and no longer on the server to re-pull.

    Toast's retention window is ~7 days, so anything older that we never captured is
    unrecoverable. Say so loudly — a silent hole reads as a $0 sales day.
    """
    from datetime import date as _date, timedelta

    root = dest_for(rid)
    if not root.is_dir():
        return
    have = {d for d in (p.name for p in root.iterdir() if p.is_dir())
            if d.isdigit() and len(d) == 8 and local_count(rid, d) > 0}
    if not have and not remote_dates:
        return
    known = sorted(have | set(remote_dates))
    start = _date.fromisoformat(f"{known[0][:4]}-{known[0][4:6]}-{known[0][6:]}")
    yesterday = _date.today() - timedelta(days=1)

    lost, day = [], start
    while day <= yesterday:
        s = day.strftime("%Y%m%d")
        if s not in have and s not in remote_dates:
            lost.append(s)
        day += timedelta(days=1)

    if lost:
        print(f"\n  🔴 PERMANENTLY LOST ({len(lost)}) — past Toast's ~7-day retention, "
              f"cannot be re-pulled:")
        print(f"     {', '.join(lost)}")
        print(f"     For sales on these days use the live API instead: "
              f"Finance/toast_weekly_sales.py <start> <end>")


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    env = load_env()

    rids = restaurant_dirs(env, debug)

    if "--list" in args:
        for rid in rids:
            dates = date_folders(env, rid, debug)
            print(f"\n{LOCATIONS[rid]['name']} ({rid}) → {dest_for(rid)}")
            print(f"  date folders available ({len(dates)}): {', '.join(dates)}")
        return

    # --pitch / --hinata narrow the run to one location; default does both.
    if "--pitch" in args:
        rids = [PITCH_ID]
    elif "--hinata" in args:
        rids = [r for r in rids if r != PITCH_ID]

    for rid in rids:
        dates = date_folders(env, rid, debug)
        if not dates:
            print(f"⚠️  {LOCATIONS[rid]['name']} ({rid}): no date folders on the server yet.")
            continue

        if "--all" in args:
            print(f"\n=== {LOCATIONS[rid]['name']} ({rid}) === force re-pulling all {len(dates)}…")
            for d in dates:
                pull_date(env, rid, d, debug)
        elif "--date" in args:
            i = args.index("--date")
            if i + 1 >= len(args):
                sys.exit("❌ --date needs a value, e.g. --date 20260627")
            d = args[i + 1]
            if d not in dates:
                sys.exit(f"❌ {d} not on the server for {rid}. Available: {', '.join(dates)}")
            print(f"\n=== {LOCATIONS[rid]['name']} ({rid}) === pulling {d}…")
            pull_date(env, rid, d, debug)
        else:
            # Default is SYNC across BOTH locations. See the module docstring — pulling
            # one folder blindly (and it being the wrong one) is the whole bug history here.
            sync(env, rid, dates, debug)

    print(f"\n✅ Done. PITCH → {LOCAL_DIR}   ·   Hinata → {LOCAL_DIR / 'hinata'}")


if __name__ == "__main__":
    main()
