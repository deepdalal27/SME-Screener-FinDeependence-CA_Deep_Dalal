#!/usr/bin/env python3
"""
Builds data/sme_data.json from REAL captured exchange data in data/seed/.

Why this exists: so the app never ships fabricated companies. Every row here is
a real listed SME company with real figures from the source named against it.
It is a starter set only — the full universe with fundamentals arrives when the
GitHub Action runs pipeline/build_data.py.

Seeds (captured 28 Jul 2026, kept in the repo so any number here is auditable):
  data/seed/nse_emerge_live.json  — NSE's own live Emerge market feed
        (api.nseindia.com /api/live-analysis-emerge): last price, 52w high/low,
        30-day and 365-day % change, traded volume.
  data/seed/cg_ipo_records.json   — Chittorgarh public IPO report 82: company
        name, ISIN, NSE symbol / BSE scrip code, issue price, listing date.

Nothing is computed beyond compute_metrics(), and any field not in the seeds
stays blank. Run:  python pipeline/make_starter_data.py
"""

import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_data import compute_metrics, DATA_DIR, JSON_PATH, JS_PATH
import sources as S

SEED = os.path.join(DATA_DIR, "seed")
CAPTURED = "2026-07-28"
SRC_NSE_LIVE = "NSE live market feed"


def load(name):
    p = os.path.join(SEED, name)
    if not os.path.exists(p):
        print(f"  ! missing seed {p}")
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def clean_name(html):
    """'<a ...>Apollo Techno Industries Ltd.</a> ' -> 'Apollo Techno Industries Ltd.'"""
    txt = re.sub(r"<[^>]*>", "", str(html or ""))
    txt = re.sub(r"\s*\(.*?IPO\)\s*$", "", txt).strip()
    return txt or None


def main():
    nse_rows = load("nse_emerge_live.json")
    cg_rows = load("cg_ipo_records.json")
    print(f"  seeds: {len(nse_rows)} NSE live rows, {len(cg_rows)} IPO records")

    # ---- index the IPO records by every identifier they carry ----------
    ipo_by_sym, ipo_by_isin, ipo_list = {}, {}, []
    for r in cg_rows:
        if "SME" not in str(r.get("Issue Category") or ""):
            continue                                   # SME platform only
        name = clean_name(r.get("Company")) or r.get("~compare_name")
        ld = S.parse_date(re.sub(r"<[^>]*>", "", str(r.get("~ListingDate") or
                                                     r.get("Listing Date") or "")))
        if not ld or ld > CAPTURED:
            continue                                   # not yet listed -> skip
        rec = {
            "name": (name or "").replace(" IPO", "").strip(),
            "isin": (r.get("~isin") or "").strip() or None,
            "nse_symbol": (r.get("~nse_symbol") or "").strip().upper() or None,
            "bse_code": str(r.get("~bse_script_code") or "").strip() or None,
            "listing_date": ld,
            "issue_price": S.parse_price_band(r.get("Issue Price (Rs.)")),
            "listing_at": r.get("Listing at") or "",
        }
        ipo_list.append(rec)
        if rec["nse_symbol"]:
            ipo_by_sym[rec["nse_symbol"]] = rec
        if rec["isin"]:
            ipo_by_isin[rec["isin"]] = rec

    companies, seen = [], set()

    # ---- 1. every live NSE Emerge scrip, with its real traded prices ----
    for row in nse_rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        ipo = ipo_by_sym.get(sym, {})
        raw, src = {}, {}

        def put(key, val, source):
            if val is not None and val != 0:
                raw[key] = val
                src[key] = source

        put("price", S.num(row.get("lastPrice")), SRC_NSE_LIVE)
        put("hi52", S.num(row.get("yearHigh")), SRC_NSE_LIVE)
        put("lo52", S.num(row.get("yearLow")), SRC_NSE_LIVE)
        put("ret_1m", S.num(row.get("perChange30d")), SRC_NSE_LIVE)
        put("ret_1y", S.num(row.get("perChange365d")), SRC_NSE_LIVE)
        if ipo.get("issue_price"):
            put("issue_price", ipo["issue_price"], S.SRC_CG)
        if ipo.get("listing_date"):
            raw["listing_date"] = ipo["listing_date"]
            src["listing_date"] = S.SRC_CG

        companies.append({
            "symbol": sym,
            "name": ipo.get("name") or sym,          # real name where sourced
            "name_pending": not bool(ipo.get("name")),
            "isin": ipo.get("isin"), "exchange": "NSE-EMERGE",
            "bse_code": None, "yahoo": sym + ".NS",
            "sector": "Unknown", "industry": None, "website": None, "summary": None,
            "listing_date": ipo.get("listing_date"),
            "fin_asof": None, "fetched": CAPTURED,
            "quality": S.quality_of(src), "src": src, "raw": raw,
            "m": compute_metrics(raw, today=CAPTURED),
        })

    # ---- 2. SME listings we know of that aren't in the live NSE feed ----
    #        (BSE SME names, and NSE names not traded in the captured snapshot)
    for rec in ipo_list:
        sym = rec["nse_symbol"] or (rec["bse_code"] and "BSE" + rec["bse_code"])
        if not sym or sym in seen:
            continue
        seen.add(sym)
        is_bse = "BSE" in (rec["listing_at"] or "") and not rec["nse_symbol"]
        raw, src = {}, {}
        if rec.get("issue_price"):
            raw["issue_price"] = rec["issue_price"]
            src["issue_price"] = S.SRC_CG
        if rec.get("listing_date"):
            raw["listing_date"] = rec["listing_date"]
            src["listing_date"] = S.SRC_CG
        companies.append({
            "symbol": rec["nse_symbol"] or rec["bse_code"],
            "name": rec["name"], "name_pending": False,
            "isin": rec["isin"],
            "exchange": "BSE-SME" if is_bse else "NSE-EMERGE",
            "bse_code": rec["bse_code"] if is_bse else None,
            "yahoo": (rec["bse_code"] + ".BO") if is_bse else (rec["nse_symbol"] + ".NS"),
            "sector": "Unknown", "industry": None, "website": None, "summary": None,
            "listing_date": rec["listing_date"],
            "fin_asof": None, "fetched": CAPTURED,
            "quality": S.quality_of(src), "src": src, "raw": raw,
            "m": compute_metrics(raw, today=CAPTURED),
        })

    q = {}
    for c in companies:
        q[c["quality"]] = q.get(c["quality"], 0) + 1
    named = sum(1 for c in companies if not c.get("name_pending"))
    priced = sum(1 for c in companies if c["m"].get("price") is not None)
    with_ipo = sum(1 for c in companies if c["m"].get("issue_price") is not None)

    payload = {
        "generated": f"{CAPTURED} — real starter data (partial universe)",
        "starter": True,
        "universe": len(companies),
        "with_financials": 0,
        "with_listing": sum(1 for c in companies if c.get("listing_date")),
        "with_issue_price": with_ipo,
        "with_price": priced,
        "quality": q,
        "benchmark": None,
        "sources": [SRC_NSE_LIVE, S.SRC_CG],
        "note": ("Real listed SME companies with real exchange figures, captured "
                 f"{CAPTURED}. Prices, 52-week range and 1M/1Y returns come from NSE's "
                 "live Emerge feed; company names, ISINs, issue prices and listing dates "
                 "from the Chittorgarh IPO report. Fundamentals are blank until the "
                 "GitHub Action runs build_data.py — nothing here is estimated."),
        "companies": companies,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.SME_DATA=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";")
    print(f"  wrote {len(companies)} REAL companies — {named} with sourced names, "
          f"{priced} with live prices, {with_ipo} with IPO issue price, quality {q}")


if __name__ == "__main__":
    main()
