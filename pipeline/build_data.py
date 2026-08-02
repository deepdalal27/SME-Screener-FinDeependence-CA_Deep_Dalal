#!/usr/bin/env python3
"""
FinDeependence SME Screener — data pipeline (v2, multi-source with provenance)
=============================================================================
Builds data/sme_data.json + data/sme_data.js for the screener webapp.

Universe   : NSE Emerge + BSE SME
Financials : BSE filing -> NSE filing -> Moneycontrol -> Yahoo  (first source
             that actually has each field wins; the source is recorded per field)
Listing    : listing date, IPO issue price, listing-day open/close, all-time
             high, and benchmark return over each company's own listing window
Prices     : daily close, 52w range, 1M/3M/1Y returns

Accuracy policy — the whole point of this file:
  * Nothing is invented. A number we cannot source is absent, and shows as "—".
  * Every field carries the name of the source it came from (company["src"]).
  * Derived figures are computed in exactly one place, compute_metrics(), which
    is covered by unit tests (pipeline/test_pipeline.py).
  * Guards against plausible-looking nonsense: no CAGR on <1yr history, no
    listing-day price unless the first available bar really is the listing day,
    no P/E/CFO-PAT on loss-makers, no ratio on a zero/negative denominator.
  * A failed source never wipes good data — every run merges into the previous
    dataset, and each company keeps its own "fetched" date stamp.

Run:
  python pipeline/build_data.py --doctor   # check which sources work from here
  python pipeline/build_data.py            # normal daily run
  python pipeline/build_data.py --full     # refresh financials for everything
  python pipeline/build_data.py --limit 20 # small test run
"""

import argparse
import json
import math
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
JSON_PATH = os.path.join(DATA_DIR, "sme_data.json")
JS_PATH = os.path.join(DATA_DIR, "sme_data.js")

IST = timezone(timedelta(hours=5, minutes=30))
CR = 1e7

ROTATE_PER_RUN = 150      # companies given a full financials refresh per daily run
PRICE_BATCH = 60
SLEEP = 0.7

# Exchange industry labels are granular and inconsistently spelt. Fold the
# common ones into broad, screener-friendly sectors. Anything not listed here
# is kept as the exchange wrote it — never discarded, never guessed.
SECTOR_MAP = {
    "it - software": "Information Technology", "it software": "Information Technology",
    "computers - software": "Information Technology", "it services": "Information Technology",
    "software & services": "Information Technology",
    "pharmaceuticals": "Healthcare", "pharmaceuticals & drugs": "Healthcare",
    "healthcare": "Healthcare", "hospital & healthcare services": "Healthcare",
    "healthcare services": "Healthcare", "medical equipment": "Healthcare",
    "chemicals": "Chemicals", "speciality chemicals": "Chemicals",
    "dyes & pigments": "Chemicals", "fertilizers": "Chemicals",
    "agro chemicals": "Chemicals", "petrochemicals": "Chemicals",
    "plastic products": "Chemicals", "polymers": "Chemicals",
    "engineering": "Capital Goods", "engineering - industrial equipments": "Capital Goods",
    "capital goods": "Capital Goods", "capital goods - electrical equipment": "Capital Goods",
    "electric equipment": "Capital Goods", "industrial machinery": "Capital Goods",
    "castings & forgings": "Capital Goods", "bearings": "Capital Goods",
    "construction": "Infrastructure", "infrastructure": "Infrastructure",
    "engineering & construction": "Infrastructure", "realty": "Realty",
    "construction - real estate": "Realty", "real estate": "Realty",
    "textile": "Textiles", "textiles": "Textiles", "garments": "Textiles",
    "readymade garments": "Textiles", "spinning": "Textiles",
    "steel": "Metals & Mining", "steel & iron products": "Metals & Mining",
    "metals": "Metals & Mining", "aluminium": "Metals & Mining",
    "mining & minerals": "Metals & Mining", "non ferrous metals": "Metals & Mining",
    "auto ancillary": "Auto Components", "auto ancillaries": "Auto Components",
    "automobile": "Automobile", "automobiles": "Automobile", "tyres": "Auto Components",
    "consumer durables": "Consumer Durables", "household appliances": "Consumer Durables",
    "fmcg": "FMCG", "food processing": "FMCG", "consumer food": "FMCG",
    "agriculture": "FMCG", "sugar": "FMCG", "edible oil": "FMCG", "tea/coffee": "FMCG",
    "breweries & distilleries": "FMCG", "personal care": "FMCG",
    "finance": "Financial Services", "finance - nbfc": "Financial Services",
    "banks": "Financial Services", "financial services": "Financial Services",
    "insurance": "Financial Services", "stock/ commodity brokers": "Financial Services",
    "trading": "Services", "logistics": "Services", "transport & logistics": "Services",
    "shipping": "Services", "aviation": "Services", "hotels & restaurants": "Services",
    "hotel, resort & restaurants": "Services", "tourism": "Services",
    "media & entertainment": "Media", "tv broadcasting & software production": "Media",
    "printing & publishing": "Media", "advertising & media": "Media",
    "telecommunication": "Telecom", "telecom services": "Telecom",
    "power generation & distribution": "Power", "power": "Power",
    "oil exploration": "Energy", "refineries": "Energy", "energy": "Energy",
    "cement": "Cement", "cement & construction materials": "Cement",
    "ceramics": "Cement", "glass": "Cement",
    "paper": "Paper & Packaging", "packaging": "Paper & Packaging",
    "diamond & jewellery": "Consumer Discretionary", "retailing": "Consumer Discretionary",
    "education": "Services", "e-commerce": "Consumer Discretionary",
}

# Benchmark candidates, tried in order; the first with usable history is used.
BENCHMARKS = [
    ("^CNXSC", "Nifty Smallcap 100"),
    ("^CRSLDX", "Nifty 500"),
    ("^NSEI", "Nifty 50"),
    ("^BSESN", "BSE Sensex"),
]


# ==========================================================================
# PURE MATH — single source of truth, fully unit-tested
# ==========================================================================

def _f(x):
    try:
        if x is None:
            return None
        x = float(x)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except (TypeError, ValueError):
        return None


def _div(a, b):
    a, b = _f(a), _f(b)
    return None if (a is None or b in (None, 0)) else a / b


def _pct(a, b):
    d = _div(a, b)
    return None if d is None else d * 100.0


def _growth(new, old):
    """% change. Meaningless off a zero/negative base -> None."""
    new, old = _f(new), _f(old)
    if new is None or old is None or old <= 0:
        return None
    return (new / old - 1.0) * 100.0


def _cagr(new, old, years):
    """Annualised %. Refuses to annualise less than a full year, or a
       negative/zero base — both produce impressive-looking nonsense."""
    new, old, years = _f(new), _f(old), _f(years)
    if new is None or old is None or years is None:
        return None
    if old <= 0 or new <= 0 or years < 1.0:
        return None
    return (pow(new / old, 1.0 / years) - 1.0) * 100.0


def days_between(d1, d2):
    """Whole days between two 'YYYY-MM-DD' strings, or None."""
    try:
        a = datetime.strptime(str(d1)[:10], "%Y-%m-%d")
        b = datetime.strptime(str(d2)[:10], "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return None


def compute_metrics(raw, today=None):
    """raw: as-reported figures in rupees (absolute) unless noted.
       Returns every screener metric. Money outputs in Rs Cr."""
    today = today or datetime.now(IST).strftime("%Y-%m-%d")

    rev = _f(raw.get("revenue"))
    op = _f(raw.get("op_income"))
    dep = _f(raw.get("dep"))
    ebitda = _f(raw.get("ebitda"))
    ebit = _f(raw.get("ebit"))
    if ebitda is None and op is not None and dep is not None:
        ebitda = op + dep
    if ebit is None and ebitda is not None and dep is not None:
        ebit = ebitda - dep
    if ebitda is None and ebit is not None and dep is not None:
        ebitda = ebit + dep
    if ebit is None and op is not None:
        ebit = op

    pat = _f(raw.get("pat"))
    interest = _f(raw.get("interest"))
    tax = _f(raw.get("tax"))
    shares = _f(raw.get("shares"))
    price = _f(raw.get("price"))
    eps = _f(raw.get("eps"))
    if eps is None and pat is not None and shares:
        eps = pat / shares
    cfo = _f(raw.get("cfo"))
    capex = _f(raw.get("capex"))
    fcf = _f(raw.get("fcf"))
    if fcf is None and cfo is not None and capex is not None:
        fcf = cfo - capex
    equity = _f(raw.get("equity"))
    debt = _f(raw.get("debt"))

    mcap = None
    if price is not None and shares:
        mcap = price * shares
    elif _f(raw.get("mcap_cr")) is not None:
        mcap = _f(raw["mcap_cr"]) * CR

    cap_emp = (equity + debt) if (equity is not None and debt is not None) else equity
    bvps = _f(raw.get("bvps")) or _div(equity, shares)

    # ---- listing & since-listing performance -----------------------------
    issue = _f(raw.get("issue_price"))
    lopen = _f(raw.get("list_open"))
    lclose = _f(raw.get("list_close"))
    ath = _f(raw.get("ath"))
    ldate = raw.get("listing_date")
    age_d = days_between(ldate, today) if ldate else None
    age_y = (age_d / 365.25) if (age_d is not None and age_d >= 0) else None
    bench = _f(raw.get("bench_ret"))          # benchmark % over the same window

    ret_issue = _growth(price, issue)
    ret_listclose = _growth(price, lclose)
    list_pop = _growth(lclose, issue)          # listing-day gain over issue price

    m = {
        # size / price
        "mcap": _div(mcap, CR),
        "price": price,
        # P&L
        "revenue": _div(rev, CR),
        "op": _div(op, CR),
        "opm": _pct(op, rev) if (rev or 0) > 0 else None,
        "ebitda": _div(ebitda, CR),
        "ebitda_pct": _pct(ebitda, rev) if (rev or 0) > 0 else None,
        "dep": _div(dep, CR),
        "ebit": _div(ebit, CR),
        "ebit_pct": _pct(ebit, rev) if (rev or 0) > 0 else None,
        "interest": _div(interest, CR),
        "tax": _div(tax, CR),
        "pat": _div(pat, CR),
        "pat_pct": _pct(pat, rev) if (rev or 0) > 0 else None,
        "eps": eps,
        # valuation
        "pe": _div(price, eps) if (eps or 0) > 0 else _f(raw.get("_pe")),
        "pb": _div(price, bvps) if (bvps or 0) > 0 else _f(raw.get("_pb")),
        "bvps": bvps,
        "div_yield": _f(raw.get("div_yield")) if raw.get("div_yield") is not None
                     else _pct(_f(raw.get("dividends_paid")), mcap),
        # cash flow
        "cfo": _div(cfo, CR),
        "capex": _div(capex, CR),
        "fcf": _div(fcf, CR),
        "cfo_pat": _div(cfo, pat) if (pat or 0) > 0 else None,
        "fcf_pat": _div(fcf, pat) if (pat or 0) > 0 else None,
        # returns / leverage
        # ratios are derived from raw figures wherever possible; the _xxx keys
        # are fallbacks supplied by an imported export, used only when we
        # genuinely cannot compute the figure ourselves
        "roe": _pct(pat, equity) if (equity or 0) > 0 else _f(raw.get("_roe")),
        "roce": _pct(ebit, cap_emp) if (cap_emp or 0) > 0 else _f(raw.get("_roce")),
        "de": _div(debt, equity) if (equity or 0) > 0 else _f(raw.get("_de")),
        "int_cov": _div(ebit, interest) if (interest or 0) > 0 else None,
        # growth
        "sales_g1": _growth(raw.get("revenue"), raw.get("revenue_py")),
        "pat_g1": _growth(raw.get("pat"), raw.get("pat_py")),
        "sales_g3": _cagr(raw.get("revenue"), raw.get("revenue_3y"), 3),
        "pat_g3": _cagr(raw.get("pat"), raw.get("pat_3y"), 3),
        # holding & price data
        "promoter": _f(raw.get("promoter_pct")),
        "hi52": _f(raw.get("hi52")),
        "lo52": _f(raw.get("lo52")),
        "ret_1m": _f(raw.get("ret_1m")),
        "ret_3m": _f(raw.get("ret_3m")),
        "ret_1y": _f(raw.get("ret_1y")),
        # ---- listing block ----
        "issue_price": issue,
        "list_open": lopen,
        "list_close": lclose,
        "list_pop": list_pop,
        "ret_issue": ret_issue,
        "ret_listclose": ret_listclose,
        "cagr_issue": _cagr(price, issue, age_y),
        "cagr_listclose": _cagr(price, lclose, age_y),
        "age_yrs": round(age_y, 2) if age_y is not None else None,
        "ath": ath,
        "off_ath": _growth(price, ath),
        "bench_ret": bench,
        "alpha": (ret_listclose - bench) if (ret_listclose is not None and bench is not None) else None,
    }
    for k, v in m.items():
        if isinstance(v, float):
            m[k] = round(v, 4)
    return m


# ==========================================================================
# PRICE HISTORY  (listing-day prices, ATH, returns)
# ==========================================================================

def history_facts(closes, opens, listing_date, tol_days=7):
    """Derive listing-day and range facts from a price history.
       closes/opens: list of (date_str, value), oldest first.

       Accuracy guard: listing-day open/close are only reported when the FIRST
       available bar really is the listing day (within tol_days). Yahoo history
       for SME names often starts months late; taking its first bar as the
       'listing price' would silently produce a wrong return forever."""
    out = {}
    if not closes:
        return out
    px = float(closes[-1][1])
    out["price"] = round(px, 2)
    vals = [v for _, v in closes]
    out["ath"] = round(max(vals), 2)

    last_yr = closes[-248:] if len(closes) > 248 else closes
    yr_vals = [v for _, v in last_yr]
    out["hi52"] = round(max(yr_vals), 2)
    out["lo52"] = round(min(yr_vals), 2)
    for key, back in (("ret_1m", 21), ("ret_3m", 63), ("ret_1y", 248)):
        if len(closes) > back and closes[-1 - back][1] > 0:
            out[key] = round((px / closes[-1 - back][1] - 1) * 100, 2)

    if listing_date:
        gap = days_between(listing_date, closes[0][0])
        if gap is not None and abs(gap) <= tol_days:
            out["list_close"] = round(float(closes[0][1]), 2)
            if opens:
                out["list_open"] = round(float(opens[0][1]), 2)
        else:
            out["_list_price_skipped"] = f"history starts {gap}d after listing"
    return out


def benchmark_return(bench_closes, from_date, to_date=None):
    """Benchmark % return between two dates, using the first bar on/after
       from_date. Returns None if the index history doesn't cover the window."""
    if not bench_closes or not from_date:
        return None
    start = None
    for d, v in bench_closes:
        if d >= str(from_date)[:10]:
            start = v
            break
    if not start or start <= 0:
        return None
    end = bench_closes[-1][1]
    if to_date:
        for d, v in bench_closes:
            if d <= str(to_date)[:10]:
                end = v
    return round((end / start - 1) * 100, 2)


def fetch_history(tickers):
    """Full price history per ticker -> {ticker: {'closes':[(date,val)], 'opens':[...]}}"""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    for i in range(0, len(tickers), PRICE_BATCH):
        chunk = tickers[i:i + PRICE_BATCH]
        try:
            df = yf.download(chunk, period="max", interval="1d", group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            S.note_error("yahoo history batch", e)
            continue
        for tk in chunk:
            try:
                sub = df[tk] if len(chunk) > 1 else df
                cl = sub["Close"].dropna()
                if cl.empty:
                    continue
                op = sub["Open"].dropna() if "Open" in sub else None
                out[tk] = {
                    "closes": [(d.strftime("%Y-%m-%d"), float(v)) for d, v in cl.items()],
                    "opens": [(d.strftime("%Y-%m-%d"), float(v)) for d, v in op.items()] if op is not None else [],
                }
            except Exception:
                continue
        time.sleep(1.0)
    return out


def fetch_benchmark():
    """-> (name, [(date, close)]) for the first benchmark index that works."""
    try:
        import yfinance as yf
    except ImportError:
        return None, []
    for tk, name in BENCHMARKS:
        try:
            df = yf.download(tk, period="max", interval="1d", progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                cl = df["Close"].dropna()
                series = [(d.strftime("%Y-%m-%d"), float(v)) for d, v in cl.items()]
                if len(series) > 250:
                    print(f"   benchmark: {name} ({tk}), {len(series)} bars")
                    return name, series
        except Exception as e:
            S.note_error(f"benchmark {tk}", e)
    print("   benchmark: none reachable — alpha will be blank")
    return None, []


# ==========================================================================
# UNIVERSE
# ==========================================================================

def build_universe(prev_companies, nse_sess):
    nse = S.nse_universe(nse_sess)
    print(f"   NSE Emerge: {len(nse)}")
    bse = S.bse_universe()
    print(f"   BSE SME:    {len(bse)}")
    merged, seen = [], set()
    for c in nse + bse:
        isin = c.get("isin")
        if isin and isin in seen:
            continue
        if isin:
            seen.add(isin)
        merged.append(c)
    if len(merged) < 50:
        print("   !! both universe sources failed — reusing previous universe")
        keep = ("symbol", "name", "isin", "exchange", "bse_code", "yahoo",
                "listing_date", "face_value", "industry")
        return [{k: v for k, v in c.items() if k in keep} for c in prev_companies]
    return merged


# ==========================================================================
# MAIN
# ==========================================================================

CKPT_PATH = os.path.join(DATA_DIR, ".checkpoint.json")


def save_checkpoint(companies, stage):
    """A full build takes hours. If the connection drops, the window is closed
       or the machine sleeps, the next run resumes from here instead of
       starting again."""
    try:
        slim = {c["yahoo"]: {"raw": c.get("raw", {}), "src": c.get("src", {}),
                             "sector": c.get("sector"), "industry": c.get("industry"),
                             "listing_date": c.get("listing_date"),
                             "fin_asof": c.get("fin_asof"), "fetched": c.get("fetched"),
                             "sector_fetched": c.get("sector_fetched"),
                             "disputed": c.get("disputed"),
                             "website": c.get("website"), "summary": c.get("summary")}
                for c in companies if c.get("yahoo")}
        with open(CKPT_PATH, "w", encoding="utf-8") as f:
            json.dump({"when": datetime.now(IST).isoformat(), "stage": stage,
                       "companies": slim}, f, separators=(",", ":"))
    except Exception as e:
        S.note_error("checkpoint save", e)


def load_checkpoint(companies):
    """Merge a previous partial run back in. Returns how many were restored."""
    if not os.path.exists(CKPT_PATH):
        return 0
    try:
        with open(CKPT_PATH, encoding="utf-8") as f:
            ck = json.load(f)
        saved = ck.get("companies", {})
        n = 0
        for c in companies:
            s = saved.get(c.get("yahoo"))
            if not s:
                continue
            merged_raw = dict(s.get("raw") or {})
            merged_raw.update(c.get("raw") or {})
            c["raw"] = merged_raw
            merged_src = dict(s.get("src") or {})
            merged_src.update(c.get("src") or {})
            c["src"] = merged_src
            for k in ("sector", "industry", "listing_date", "fin_asof", "fetched",
                      "sector_fetched", "disputed", "website", "summary"):
                if s.get(k) and not c.get(k):
                    c[k] = s[k]
            n += 1
        print(f"   resumed {n} companies from checkpoint ({ck.get('when','?')[:16]}, "
              f"stage {ck.get('stage')})")
        return n
    except Exception as e:
        S.note_error("checkpoint load", e)
        return 0


def load_previous():
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, encoding="utf-8") as f:
                d = json.load(f)
                if d.get("sample"):
                    return {"companies": []}     # never merge demo data into real data
                return d
        except Exception:
            pass
    return {"companies": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doctor", action="store_true", help="probe every data source and exit")
    ap.add_argument("--full", action="store_true", help="refresh financials for ALL companies")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any saved checkpoint and start over")
    ap.add_argument("--no-xbrl", action="store_true",
                    help="skip XBRL filings (faster, slightly less accurate)")
    ap.add_argument("--verify-tol", type=float, default=0.05,
                    help="flag a company when two sources differ by more than this (0.05 = 5%%)")
    args = ap.parse_args()

    if args.doctor:
        rep = S.probe_all()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "source_report.json"), "w", encoding="utf-8") as f:
            json.dump({"checked": datetime.now(IST).isoformat(), "report": rep}, f, indent=1)
        print(f"\n   saved data/source_report.json")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    t0 = time.time()
    prev = load_previous()
    prev_by = {c.get("yahoo"): c for c in prev.get("companies", []) if c.get("yahoo")}
    today = datetime.now(IST).strftime("%Y-%m-%d")

    print("== 1/6 universe")
    nse_sess = S.nse_session()
    universe = build_universe(prev.get("companies", []), nse_sess)
    if args.limit:
        universe = universe[:args.limit]
    companies = []
    for u in universe:
        c = dict(prev_by.get(u["yahoo"], {}))
        c.update({k: v for k, v in u.items() if v is not None})
        c.setdefault("raw", {})
        c.setdefault("src", {})
        companies.append(c)
    print(f"   universe: {len(companies)}")
    if not args.fresh:
        load_checkpoint(companies)

    print("== 2/6 IPO history (issue price + listing date)")
    hist = S.ipo_history()
    matched = 0
    for c in companies:
        rec = S.match_ipo(c, hist)
        if rec.get("issue_price"):
            c["raw"]["issue_price"] = rec["issue_price"]
            c["src"]["issue_price"] = S.SRC_CG
            matched += 1
        if rec.get("listing_date") and not c.get("listing_date"):
            c["listing_date"] = rec["listing_date"]
            c["src"]["listing_date"] = S.SRC_CG
        if c.get("listing_date"):
            c["raw"]["listing_date"] = c["listing_date"]
            c["src"].setdefault("listing_date", S.SRC_NSE)
    print(f"   issue price matched for {matched}/{len(companies)}")

    print("== 3/6 price history, listing-day prices, 52w range, ATH")
    bench_name, bench_series = (None, [])
    if not args.no_history:
        bench_name, bench_series = fetch_benchmark()
        hist_px = fetch_history([c["yahoo"] for c in companies])
        got = skipped = 0
        for c in companies:
            h = hist_px.get(c["yahoo"])
            if not h:
                continue
            facts = history_facts(h["closes"], h["opens"], c.get("listing_date"))
            if facts.pop("_list_price_skipped", None):
                skipped += 1
            for k, v in facts.items():
                c["raw"][k] = v
                c["src"][k] = S.SRC_YF
            if bench_series and c.get("listing_date"):
                b = benchmark_return(bench_series, c["listing_date"])
                if b is not None:
                    c["raw"]["bench_ret"] = b
                    c["src"]["bench_ret"] = bench_name
            got += 1
        print(f"   history for {got}/{len(companies)}; "
              f"listing-day price withheld for {skipped} (history starts too late)")

    print("== 4/6 financials — XBRL > your export > BSE > NSE > Moneycontrol > Yahoo")
    imports = {}
    try:
        import import_external as IX
        imports = IX.load_imports(os.path.join(DATA_DIR, "import"))
        if imports:
            print(f"   using your own exports for {len(imports)} lookup keys")
        else:
            print("   (no files in data/import/ — see README to use a Screener export)")
    except Exception as e:
        S.note_error("imports", e)
    todo = sorted(companies, key=lambda c: c.get("fetched") or "1970-01-01")
    if not args.full:
        todo = todo[:ROTATE_PER_RUN]
    stats = {S.SRC_XBRL: 0, S.SRC_IMPORT: 0, S.SRC_BSE: 0, S.SRC_NSE: 0,
             S.SRC_MC: 0, S.SRC_YF: 0, "none": 0}
    for n, c in enumerate(todo, 1):
        cands = []
        try:
            # 1. the company's own XBRL filing — same source Screener parses
            if not args.no_xbrl:
                cands.append(S.xbrl_financials(
                    c.get("symbol") if c.get("exchange") == "NSE-EMERGE" else None,
                    c.get("bse_code"), nse_sess))
            # 2. your own Screener/Trendlyne export, if you dropped one in
            if imports:
                hit = IX.match_import(c, imports)
                if hit:
                    vals_i, extra_i, meta_i = hit
                    if vals_i:
                        cands.append((vals_i, S.SRC_IMPORT))
                    c["_import_extra"] = extra_i
                    if meta_i.get("sector") and not c.get("sector"):
                        c["sector"] = meta_i["sector"]
                        c["src"]["sector"] = S.SRC_IMPORT
                    if meta_i.get("industry") and not c.get("industry"):
                        c["industry"] = meta_i["industry"]
                    if meta_i.get("name") and (not c.get("name") or c.get("name") == c.get("symbol")):
                        c["name"] = meta_i["name"]
                        c["src"]["name"] = S.SRC_IMPORT
            # 3-6. exchange JSON APIs, then third-party
            if c.get("bse_code"):
                cands.append(S.bse_financials(c["bse_code"]))
            if c.get("exchange") == "NSE-EMERGE":
                cands.append(S.nse_financials(c.get("symbol"), nse_sess))
            if not any(v and v[0].get("revenue") for v in cands):
                cands.append(S.mc_snapshot(c.get("name") or c.get("symbol"), c.get("exchange")))
                cands.append(S.yahoo_financials(c["yahoo"]))
            # cross-check the same field across every source that reported it
            disputes = S.cross_check(cands, tol=args.verify_tol)
            c["disputed"] = disputes or None
            vals, srcs = S.merge_by_priority(cands)
            if vals:
                period = vals.pop("_period", None)
                for k, v in vals.items():
                    # never let a weaker source overwrite a price we already
                    # took from the official history
                    if k in ("price", "hi52", "lo52") and k in c["raw"]:
                        continue
                    c["raw"][k] = v
                    c["src"][k] = srcs.get(k)
                for k, v in (c.pop("_import_extra", {}) or {}).items():
                    c["raw"].setdefault(k, v)      # ratio fallbacks from your export
                if period:
                    c["fin_asof"] = period
                c["quality"] = S.quality_of(c["src"])
                stats[srcs.get("revenue") or srcs.get("pat") or "none"] = \
                    stats.get(srcs.get("revenue") or srcs.get("pat") or "none", 0) + 1
            else:
                stats["none"] += 1
            c["fetched"] = today
        except Exception as e:
            S.note_error(f"financials {c.get('symbol')}", e)
            stats["none"] += 1
        if n % 25 == 0:
            disp = sum(1 for x in companies if x.get("disputed"))
            print(f"   {n}/{len(todo)}… ({disp} cross-source disagreements so far)")
            save_checkpoint(companies, f"financials {n}/{len(todo)}")
        time.sleep(SLEEP + random.random() * 0.3)
    save_checkpoint(companies, "financials done")
    print("   sources used for core figures: " +
          ", ".join(f"{k}={v}" for k, v in stats.items() if v))
    n_disp = sum(1 for c in companies if c.get("disputed"))
    print(f"   cross-checked; {n_disp} companies where two sources disagree "
          f"by >{args.verify_tol*100:.0f}% (exchange filing kept, dispute published)")

    print("== 5/6 sectors")
    # (a) BSE's scrip list already carries an industry label for every BSE SME
    #     company — free, bulk, no extra calls. Use it as the sector.
    from_bse = 0
    for c in companies:
        if (not c.get("sector") or c["sector"] == "Unknown") and c.get("industry"):
            c["sector"] = SECTOR_MAP.get(c["industry"].strip().lower(), c["industry"].strip())
            c["src"]["sector"] = S.SRC_BSE
            from_bse += 1
    # (b) NSE quote metadata carries an industry too (one call per symbol)
    from_nse = 0
    todo_nse = [c for c in companies
                if (not c.get("sector") or c["sector"] == "Unknown")
                and c.get("exchange") == "NSE-EMERGE"]
    if not args.full:
        todo_nse = sorted(todo_nse, key=lambda c: c.get("sector_fetched") or "")[:250]
    for c in todo_nse:
        try:
            q = S.nse_quote(c.get("symbol"), nse_sess)
            if q.get("industry"):
                c["industry"] = q["industry"]
                c["sector"] = SECTOR_MAP.get(q["industry"].strip().lower(), q["industry"].strip())
                c["src"]["sector"] = S.SRC_NSE
                from_nse += 1
            if q.get("isin") and not c.get("isin"):
                c["isin"] = q["isin"]
            if q.get("listing_date") and not c.get("listing_date"):
                c["listing_date"] = q["listing_date"]
                c["raw"]["listing_date"] = q["listing_date"]
                c["src"]["listing_date"] = S.SRC_NSE
            c["sector_fetched"] = today
        except Exception as e:
            S.note_error(f"nse sector {c.get('symbol')}", e)
        time.sleep(0.35)
    # (c) anything still unlabelled: Yahoo profile (also gives website/summary)
    from_yf = 0
    still = [c for c in companies if not c.get("sector") or c["sector"] == "Unknown"]
    if not args.full:
        still = sorted(still, key=lambda c: c.get("sector_fetched") or "")[:250]
    if still:
        try:
            import yfinance as yf
            for c in still:
                try:
                    info = yf.Ticker(c["yahoo"]).get_info()
                    if info.get("sector"):
                        c["sector"] = info["sector"]
                        c["src"]["sector"] = S.SRC_YF
                        from_yf += 1
                    c["industry"] = c.get("industry") or info.get("industry")
                    c["website"] = c.get("website") or info.get("website")
                    c["summary"] = c.get("summary") or \
                        ((info.get("longBusinessSummary") or "")[:600] or None)
                except Exception:
                    pass
                c["sector_fetched"] = today
                time.sleep(0.35)
        except ImportError:
            pass
    for c in companies:
        c.setdefault("sector", "Unknown")
        if not c["sector"]:
            c["sector"] = "Unknown"
    labelled = sum(1 for c in companies if c.get("sector") and c["sector"] != "Unknown")
    print(f"   sectors: {labelled}/{len(companies)} labelled "
          f"(BSE {from_bse}, NSE {from_nse}, Yahoo {from_yf})")

    print("== 6/6 computing metrics + writing")
    out = []
    for c in companies:
        m = compute_metrics(c.get("raw", {}), today)
        c["quality"] = S.quality_of(c.get("src", {}))
        out.append({
            "symbol": c.get("symbol"), "name": c.get("name"), "isin": c.get("isin"),
            "exchange": c.get("exchange"), "bse_code": c.get("bse_code"),
            "yahoo": c.get("yahoo"), "sector": c.get("sector") or "Unknown",
            "industry": c.get("industry"), "website": c.get("website"),
            "summary": c.get("summary"),
            "listing_date": c.get("listing_date"),
            "fin_asof": c.get("fin_asof"), "fetched": c.get("fetched"),
            "quality": c["quality"], "src": c.get("src", {}),
            "disputed": c.get("disputed") or None,
            "raw": c.get("raw", {}), "m": m,
        })

    q = {}
    for c in out:
        q[c["quality"]] = q.get(c["quality"], 0) + 1
    payload = {
        "generated": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "universe": len(out),
        "with_financials": sum(1 for c in out if c["m"].get("revenue") is not None),
        "with_listing": sum(1 for c in out if c.get("listing_date")),
        "with_issue_price": sum(1 for c in out if c["m"].get("issue_price") is not None),
        "quality": q,
        "disputed_count": sum(1 for c in out if c.get("disputed")),
        "verify_tol_pct": round(args.verify_tol * 100, 1),
        "benchmark": bench_name,
        "sources": ["BSE filing", "NSE filing", "Moneycontrol", "Yahoo", "Chittorgarh IPO feed"],
        "errors": S.ERRORS[-40:],
        "note": ("Figures as filed with NSE/BSE where available; each field's source is "
                 "recorded per company. Money in Rs Cr unless noted. Blank means not "
                 "sourced — never estimated."),
        "companies": out,
    }
    # ---- refuse to publish a dataset that is worse than what's already live ----
    # If every source was blocked (e.g. the exchanges rejected this IP today), we
    # would otherwise overwrite good data with an empty file. Keep the old one.
    prev_n = len(prev.get("companies", []))
    if not out or (prev_n >= 50 and len(out) < prev_n * 0.5):
        print(f"   !! ABORT: built {len(out)} companies vs {prev_n} already live — "
              f"refusing to overwrite. {len(S.ERRORS)} source errors; run --doctor.")
        with open(os.path.join(DATA_DIR, "last_failed_run.json"), "w", encoding="utf-8") as f:
            json.dump({"when": datetime.now(IST).isoformat(), "built": len(out),
                       "previous": prev_n, "errors": S.ERRORS[-40:]}, f, indent=1)
        sys.exit(1)

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.SME_DATA=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";")
    try:                       # published successfully — checkpoint no longer needed
        if os.path.exists(CKPT_PATH):
            os.remove(CKPT_PATH)
    except Exception:
        pass
    print(f"   wrote {os.path.getsize(JSON_PATH)//1024} KB · "
          f"{payload['with_financials']} with financials · "
          f"{payload['with_issue_price']} with issue price · quality {q} · "
          f"{len(S.ERRORS)} source errors · {int(time.time()-t0)}s")


if __name__ == "__main__":
    main()
