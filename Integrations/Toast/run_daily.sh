#!/usr/bin/env bash
# PITCH — daily Toast Data Export runner (pull + load).
# Used by BOTH the local launchd job (com.pitch.toast-export) and the cloud
# routine. The PULL is critical; the LOAD is best-effort (needs the Airtable PAT
# in .vault/airtable.env locally, or AIRTABLE_* env vars in the cloud) — until
# that exists, the pull still runs and files land locally, so nothing is lost.
#
# Logs to Integrations/Toast/exports/run.log (gitignored).
set -u
# Resolve paths from THIS script's location so the same file runs locally
# (~/Projects/PITCH Sports Bar/...) and in the cloud repo clone. Portable across
# bash (BASH_SOURCE) and zsh (falls back to $0).
SOURCE="${BASH_SOURCE[0]:-$0}"
TOAST="$(cd "$(dirname "$SOURCE")" && pwd)"
REPO="$(cd "$TOAST/../.." && pwd)"
LOG="$TOAST/exports/run.log"
mkdir -p "$TOAST/exports"

# Use a stamp passed in by launchd-free callers, else a plain marker.
echo "===== run $(date '+%Y-%m-%d %H:%M:%S %Z') =====" >> "$LOG"

# 1) PULL (critical) — grab the latest date folder from Toast SFTP.
if python3 "$TOAST/toast_export_pull.py" >> "$LOG" 2>&1; then
  echo "pull: OK" >> "$LOG"
else
  echo "pull: FAILED — capturing --debug handshake below" >> "$LOG"
  python3 "$TOAST/toast_export_pull.py" --list --debug >> "$LOG" 2>&1 || true
  exit 1   # no point loading if the pull failed
fi

# 2) LOAD (best-effort) — item-sales -> Airtable.
# Run if creds are available either as the local vault file OR as env vars (cloud).
if [ -f "$REPO/.vault/airtable.env" ] || [ -n "${AIRTABLE_PAT:-}" ]; then
  if python3 "$TOAST/toast_load_itemsales.py" >> "$LOG" 2>&1; then
    echo "load: OK" >> "$LOG"
  else
    echo "load: FAILED (see above)" >> "$LOG"
  fi
else
  echo "load: SKIPPED (no .vault/airtable.env — add the Airtable PAT to enable)" >> "$LOG"
fi
