# Toast Data Export Pipeline

The master daily data feed for PITCH. SFTP-delivered Toast exports → local files →
Airtable → BOH demand / finance / staff engines. Set up & confirmed working 2026-06-27.

## Architecture (hybrid)

```
Toast Data Export (AWS Transfer SFTP, daily ~morning)
        │  s-9b0f88558b264dfda.server.transfer.us-east-1.amazonaws.com
        │  user PitchxHinataDataExports, SSH key auth, layout 86897/YYYYMMDD/<files>
        ▼
 toast_export_pull.py  ──►  Integrations/Toast/exports/<YYYYMMDD>/*.csv|json   (PULL, key-only)
        ▼
 toast_load_itemsales.py  ──►  Airtable "Item Sales (Toast)"  tblpiyZfDU7TuP9eh   (LOAD, needs PAT)
        ▼
 order-lookup agent · weekly ops · (future) BOM depletion / par engine
```

**Local half (LIVE):** launchd `com.pitch.toast-export` runs `run_daily.sh` at 10:30 HST daily
(catches up on wake). Pull is critical; load is best-effort and auto-enables once the PAT exists.
- Plist: `com.pitch.toast-export.plist` → installed at `~/Library/LaunchAgents/`
- Logs: `exports/run.log`, `exports/launchd.{out,err}.log` (gitignored)
- Manage: `launchctl unload|load ~/Library/LaunchAgents/com.pitch.toast-export.plist`

**Cloud half (primary, always-on — pending PAT):** a Claude Code routine like the Weekly Ops
Brief. Cloud has no `.vault`, so secrets come from env vars:
- `TOAST_SFTP_HOST`, `TOAST_SFTP_USER`, `TOAST_SFTP_KEY_CONTENTS` (the private key text; the
  pull script writes it to a temp 600 file), `AIRTABLE_PAT`,
  `AIRTABLE_INVENTORY_BASE=appxC1Dv2VROY8Tlp`, `AIRTABLE_ITEMSALES_TABLE=tblpiyZfDU7TuP9eh`
- **Network egress:** must allowlist the SFTP host (Custom network, NOT "Trusted" — same gotcha
  as the Toast REST API per the weekly-ops SKILL).

## Scripts

| Script | Does | Key flags |
|---|---|---|
| `toast_export_pull.py` | SFTP pull of latest (or `--date`/`--all`) date folder | `--list`, `--date YYYYMMDD`, `--all`, `--debug` |
| `toast_load_itemsales.py` | Aggregate ItemSelectionDetails → upsert Item Sales table | `--json` (no Airtable), `--date`, `--all`, `--debug` |
| `run_daily.sh` | pull + best-effort load (the launchd/cloud entrypoint) | — |

Both scripts are stdlib-only and follow the house cred pattern (env vars first, `.vault/*.env`
override locally). Idempotent: re-running a date updates rather than duplicates
(key = `Date Key` + `Menu Item`).

## Credentials

- SFTP: `.vault/toast-sftp.env` (HOST/USER), private key `.vault/toast_sftp` (600). ✅ done.
- Airtable: `.vault/airtable.env` (PAT + base + table). ⛔ **PAT not yet created** — see below.

## ⛔ ONE STEP LEFT to fully automate the LOAD: create an Airtable PAT

1. https://airtable.com/create/tokens → scopes `data.records:read` + `data.records:write`
   → grant access to base **PITCH Inventory & Ordering** (`appxC1Dv2VROY8Tlp`).
2. `cp .vault/airtable.env.example .vault/airtable.env` and paste the PAT in.
3. Then `python3 Integrations/Toast/toast_load_itemsales.py` loads to Airtable; the local
   launchd job picks it up automatically next morning.

Until then the **pull runs daily** and files are saved locally — no data lost.

## Scope notes

- Feeds: BOH demand (Item Sales), finance, staff (TimeEntries/CheckDetails — future module).
- Does NOT feed reservations: `kitchen-demand-radar.py` / `seating-availability.py` use the
  manual Toast Tables bookings CSV, which the export does NOT include.
- Ingredient depletion (dish → ingredient) needs the recipe BOM (chef capture); Item Sales is
  dish-level and is the foundation the BOM will join onto. Demand Flex is best-effort name-matched.

## Backfill

Ask Toast / use `--all` once historical date folders appear on the SFTP, so the demand history
starts deep. (Today only `20260627` is present.)
