#!/usr/bin/env python3
"""Toast item-sales -> BOH demand loader for PITCH.

Reads a day's Toast Data Export (already pulled by toast_export_pull.py),
aggregates ItemSelectionDetails.csv to per-menu-item daily sales, cross-refs
against the PAR Items "Demand Flex" tag, and upserts the result into the
Airtable "Item Sales (Toast)" table so the order-lookup agent + weekly ops
can read what actually sold. This is the BOH "data flowing OUT" module.

WHY ItemSelectionDetails (not AllItemsReport): it's clean line-item level
(real menu-item names, qty, net price, sales category, server). AllItemsReport
carries hierarchical subtotal rows with blank names — noise for our purpose.

WHY no ingredient depletion yet: Toast is item-level only; dish -> ingredient
needs the recipe BOM (chef-capture task). This loader lands the dish-level
truth that the BOM will later join onto. Where a menu-item name overlaps a PAR
item (e.g. "Garlic Chicken"), we copy its Demand Flex as a best-effort tag.

House style (matches toast_client.py / toast_export_pull.py): stdlib only,
creds load from env first then .vault override (so the same code runs in the
cloud routine and locally), errors surface, --debug shows what ran.

Creds (.vault/airtable.env, gitignored — or AIRTABLE_* env vars in cloud):
    AIRTABLE_PAT=pat...                 # Personal Access Token w/ data.records:write on the Inventory base
    AIRTABLE_INVENTORY_BASE=appxC1Dv2VROY8Tlp        # optional, default below
    AIRTABLE_ITEMSALES_TABLE=tbl...                  # the Item Sales (Toast) table id

Usage:
    python3 toast_load_itemsales.py --json            # parse latest export, print, NO Airtable
    python3 toast_load_itemsales.py --date 20260627   # specific day
    python3 toast_load_itemsales.py                    # parse latest + upsert to Airtable
    python3 toast_load_itemsales.py --all --json       # every pulled day, printed
    python3 toast_load_itemsales.py --debug
"""
import csv
import json
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

HERE        = Path(__file__).resolve().parent
REPO        = HERE.parent.parent
VAULT       = REPO / ".vault"
ENV_FILE    = VAULT / "airtable.env"
EXPORTS_DIR = HERE / "exports"

DEFAULT_BASE  = "appxC1Dv2VROY8Tlp"      # PITCH Inventory & Ordering
PAR_TABLE     = "tbl69alVAFPKbGJ0U"      # PAR Items (for Demand Flex cross-ref)
PAR_NAME_FLD  = "Item Name"
PAR_FLEX_FLD  = "Demand Flex"
AT_API        = "https://api.airtable.com/v0"


# ---------- creds ----------
def load_env():
    env = {k: v for k, v in os.environ.items() if k.startswith("AIRTABLE_")}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass  # cloud / --json runs: rely on env vars or skip Airtable
    return env


# ---------- export discovery ----------
def date_dirs():
    if not EXPORTS_DIR.exists():
        sys.exit(f"❌ No exports dir ({EXPORTS_DIR}). Run toast_export_pull.py first.")
    return sorted(p.name for p in EXPORTS_DIR.iterdir()
                  if p.is_dir() and p.name.isdigit() and len(p.name) == 8)


def resolve_dates(args):
    dirs = date_dirs()
    if not dirs:
        sys.exit("❌ No pulled date folders found. Run toast_export_pull.py first.")
    if "--all" in args:
        return dirs
    if "--date" in args:
        i = args.index("--date")
        if i + 1 >= len(args):
            sys.exit("❌ --date needs YYYYMMDD")
        d = args[i + 1]
        if d not in dirs:
            sys.exit(f"❌ {d} not pulled. Have: {', '.join(dirs)}")
        return [d]
    return [dirs[-1]]


# ---------- parse ----------
def aggregate_day(date):
    """ItemSelectionDetails.csv -> per-menu-item dict for one YYYYMMDD."""
    path = EXPORTS_DIR / date / "ItemSelectionDetails.csv"
    if not path.exists():
        print(f"  ⚠️  {date}: no ItemSelectionDetails.csv (slow/closed day?) — skipping", file=sys.stderr)
        return []
    agg = defaultdict(lambda: {"qty": 0.0, "net": 0.0, "orders": set(),
                               "group": "", "category": ""})
    for x in csv.DictReader(path.open()):
        if (x.get("Void?") or "").strip().lower() == "true":
            continue
        name = (x.get("Menu Item") or "").strip()
        if not name:
            continue
        a = agg[name]
        a["qty"]   += float(x.get("Qty") or 0)
        a["net"]   += float(x.get("Net Price") or 0)
        a["orders"].add(x.get("Order Id"))
        a["group"]    = (x.get("Menu Group") or "").strip()    or a["group"]
        a["category"] = (x.get("Sales Category") or "").strip() or a["category"]
    rows = []
    for name, a in agg.items():
        rows.append({
            "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "date_key": date,
            "menu_item": name,
            "menu_group": a["group"],
            "sales_category": a["category"],
            "qty": round(a["qty"], 2),
            "net_sales": round(a["net"], 2),
            "orders": len(a["orders"]),
        })
    rows.sort(key=lambda r: -r["qty"])
    return rows


# ---------- PAR Demand Flex cross-ref ----------
def _norm(s):
    s = s.lower()
    for junk in ("(1/2 pan)", "(half pan)", "(full pan)", "(1/2 pan", "1/2 pan",
                 "(hh)", "half pan", "full pan"):
        s = s.replace(junk, "")
    return " ".join(s.split())


def fetch_par_flex(env, debug):
    """Return {normalized PAR item name: Demand Flex} or {} if Airtable unavailable."""
    pat = env.get("AIRTABLE_PAT", "").strip()
    base = env.get("AIRTABLE_INVENTORY_BASE", DEFAULT_BASE).strip()
    if not pat:
        return {}
    flex = {}
    offset = None
    while True:
        url = f"{AT_API}/{base}/{PAR_TABLE}?pageSize=100"
        if offset:
            url += f"&offset={offset}"
        body = _at_get(url, pat, debug)
        for rec in body.get("records", []):
            f = rec.get("fields", {})
            nm = f.get(PAR_NAME_FLD)
            fx = f.get(PAR_FLEX_FLD)
            if nm and fx:
                flex[_norm(nm)] = fx
        offset = body.get("offset")
        if not offset:
            break
    return flex


def tag_flex(rows, flex):
    for r in rows:
        n = _norm(r["menu_item"])
        match = flex.get(n)
        if not match:  # contains-match fallback
            for pn, fx in flex.items():
                if pn and (pn in n or n in pn):
                    match = fx
                    break
        r["demand_flex"] = match or ""
    return rows


# ---------- Airtable I/O ----------
def _at_get(url, pat, debug):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {pat}"})
    if debug:
        print("→ GET", url, file=sys.stderr)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"❌ Airtable GET {e.code}: {e.read().decode()[:300]}")


def _at_send(url, pat, payload, method, debug):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {pat}", "Content-Type": "application/json"})
    if debug:
        print(f"→ {method} {url}\n  {json.dumps(payload)[:300]}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"❌ Airtable {method} {e.code}: {e.read().decode()[:300]}")


def existing_for_dates(env, table, dates, debug):
    """Map (date_key, menu_item) -> recordId for idempotent upsert."""
    pat = env.get("AIRTABLE_PAT", "").strip()
    base = env.get("AIRTABLE_INVENTORY_BASE", DEFAULT_BASE).strip()
    keyset = set(dates)
    out = {}
    offset = None
    while True:
        url = f"{AT_API}/{base}/{table}?pageSize=100&fields%5B%5D=Date+Key&fields%5B%5D=Menu+Item"
        if offset:
            url += f"&offset={offset}"
        body = _at_get(url, pat, debug)
        for rec in body.get("records", []):
            f = rec.get("fields", {})
            dk, mi = f.get("Date Key"), f.get("Menu Item")
            if dk in keyset and mi:
                out[(dk, mi)] = rec["id"]
        offset = body.get("offset")
        if not offset:
            break
    return out


def upsert(env, rows, debug):
    pat = env.get("AIRTABLE_PAT", "").strip()
    base = env.get("AIRTABLE_INVENTORY_BASE", DEFAULT_BASE).strip()
    table = env.get("AIRTABLE_ITEMSALES_TABLE", "").strip()
    if not pat or not table:
        sys.exit("❌ Airtable write needs AIRTABLE_PAT + AIRTABLE_ITEMSALES_TABLE in "
                 f"{ENV_FILE} (or env). Use --json to skip Airtable.")
    dates = sorted({r["date_key"] for r in rows})
    have = existing_for_dates(env, table, dates, debug)

    def fields(r):
        return {
            "Date": r["date"], "Date Key": r["date_key"],
            "Menu Item": r["menu_item"], "Menu Group": r["menu_group"],
            "Sales Category": r["sales_category"], "Qty": r["qty"],
            "Net Sales": r["net_sales"], "Orders": r["orders"],
            "Demand Flex": r.get("demand_flex", ""),
        }

    creates, updates = [], []
    for r in rows:
        rid = have.get((r["date_key"], r["menu_item"]))
        (updates if rid else creates).append(
            {"id": rid, "fields": fields(r)} if rid else {"fields": fields(r)})

    url = f"{AT_API}/{base}/{table}"
    for i in range(0, len(creates), 10):
        _at_send(url, pat, {"records": creates[i:i+10]}, "POST", debug)
    for i in range(0, len(updates), 10):
        _at_send(url, pat, {"records": updates[i:i+10]}, "PATCH", debug)
    return len(creates), len(updates)


# ---------- main ----------
def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    json_only = "--json" in args
    env = load_env()
    dates = resolve_dates(args)

    all_rows = []
    for d in dates:
        all_rows.extend(aggregate_day(d))
    if not all_rows:
        print("No item-sales rows for the selected day(s).")
        return

    flex = {} if json_only else fetch_par_flex(env, debug)
    if json_only and env.get("AIRTABLE_PAT", "").strip():
        flex = fetch_par_flex(env, debug)
    all_rows = tag_flex(all_rows, flex)

    if json_only:
        print(json.dumps(all_rows, indent=2))
        return

    created, updated = upsert(env, all_rows, debug)
    print(f"✅ Item Sales loaded for {', '.join(dates)}: "
          f"{created} created, {updated} updated ({len(all_rows)} rows).")
    flagged = [r for r in all_rows if r.get("demand_flex") in ("High", "Medium")]
    if flagged:
        print("   Demand-flex movers today:")
        for r in sorted(flagged, key=lambda r: -r["qty"]):
            print(f"     [{r['demand_flex']:6}] {r['menu_item']}  qty {r['qty']:.0f}")


if __name__ == "__main__":
    main()
