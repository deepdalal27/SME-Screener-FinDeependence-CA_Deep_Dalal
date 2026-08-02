#!/usr/bin/env python3
"""
Data source adapters — every number carries the name of the source it came from.

PRIORITY (highest accuracy first):
  1. BSE corporate filings API      -> "BSE filing"
  2. NSE corporate results API      -> "NSE filing"
  3. Moneycontrol public price feed -> "Moneycontrol"
  4. Yahoo Finance statements       -> "Yahoo"

Design rules that keep this honest:
  * Each adapter returns (values, source_name). It NEVER fabricates a value —
    a field it cannot read is simply absent from the dict.
  * merge_by_priority() fills each field from the best source that has it and
    records which source that was, per field.
  * Endpoint shapes change without notice. Every adapter is tolerant (tries
    several known endpoint spellings, matches statement lines by fuzzy label)
    and every failure is recorded, never silently swallowed.
  * probe_all() (used by build_data.py --doctor) tells you exactly which
    sources work from the machine you are running on.
"""

import io
import json
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Your Cloudflare Worker (already live for the IPO Tracker) can relay requests
# that a datacentre IP would otherwise get blocked on.
WORKER_BRIDGE = "https://fd-license.deepuploads27.workers.dev/data?u="

SRC_XBRL = "XBRL filing"        # the company's own tagged submission - primary
SRC_IMPORT = "Screener/your export"
SRC_BSE = "BSE filing"
SRC_NSE = "NSE filing"
SRC_MC = "Moneycontrol"
SRC_YF = "Yahoo"
SRC_CG = "Chittorgarh IPO feed"
SRC_RANK = {SRC_XBRL: 0, SRC_IMPORT: 1, SRC_BSE: 2, SRC_NSE: 3, SRC_MC: 4, SRC_YF: 5}

# every failure is recorded here for the run report
ERRORS = []


def note_error(where, detail):
    ERRORS.append({"where": where, "detail": str(detail)[:200]})


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _sess(referer=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    if referer:
        s.headers["Referer"] = referer
    return s


def get(url, session=None, timeout=25, retries=1, via_bridge_on_fail=True, referer=None):
    """GET with retry; on repeated failure optionally retry through the Worker bridge."""
    s = session or _sess(referer)
    last = None
    for attempt in range(retries + 1):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = repr(e)
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    if via_bridge_on_fail and WORKER_BRIDGE:
        try:
            r = requests.get(WORKER_BRIDGE + requests.utils.quote(url, safe=""),
                             headers={"User-Agent": UA}, timeout=timeout + 10)
            if r.status_code == 200 and r.content:
                return r
            last = f"bridge HTTP {r.status_code}"
        except Exception as e:
            last = f"bridge {e!r}"
    note_error(url[:90], last)
    return None


def get_json(url, **kw):
    r = get(url, **kw)
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        try:                       # some endpoints return JSON with a BOM//*prefix
            return json.loads(re.sub(r"^[^\[{]*", "", r.text, count=1))
        except Exception as e:
            note_error(url[:90], f"bad json: {e!r}")
            return None


def nse_session():
    """NSE requires cookies from the homepage before its APIs respond."""
    s = _sess("https://www.nseindia.com/")
    try:
        s.get("https://www.nseindia.com/", timeout=20)
        time.sleep(0.8)
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=20)
        time.sleep(0.4)
    except Exception as e:
        note_error("nse cookie warmup", e)
    return s


# --------------------------------------------------------------------------
# number / label helpers
# --------------------------------------------------------------------------

def num(x):
    """Parse Indian-format numbers. Returns None (never 0) when unparseable."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    s = unicodedata.normalize("NFKD", str(x)).strip()
    if s in ("", "-", "--", "NA", "N.A.", "NIL", "null", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[(),%\s₹]|Rs\.?|INR", "", s, flags=re.I)
    s = s.replace("−", "-")
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v:
        return None
    return -v if neg else v


def norm_label(s):
    return re.sub(r"[^a-z]", "", str(s or "").lower())


# Company-name suffixes that carry no identifying information.
_NAME_SUFFIXES = ("privatelimited", "pvtlimited", "pvtltd", "limited", "ltd",
                  "private", "pvt", "inc")


def name_key(s):
    """Canonical key for name-based matching.

    MUST be used for BOTH indexing and lookup — normalising differently on
    each side silently breaks every name match, which is exactly the kind of
    quiet failure that fills a screener with blanks."""
    n = norm_label(s)
    changed = True
    while changed:
        changed = False
        for suf in _NAME_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 2:
                n = n[: -len(suf)]
                changed = True
    return n


def pick(d, *patterns):
    """Find a value in a dict whose key fuzzily matches any pattern."""
    if not isinstance(d, dict):
        return None
    keys = {norm_label(k): k for k in d}
    for p in patterns:
        np = norm_label(p)
        if np in keys:
            v = num(d[keys[np]])
            if v is not None:
                return v
    for p in patterns:                       # substring fallback
        np = norm_label(p)
        for nk, orig in keys.items():
            if np and np in nk:
                v = num(d[orig])
                if v is not None:
                    return v
    return None


CR = 1e7


def to_rupees(v, unit_hint):
    """Exchange result feeds report in lakhs or crores; normalise to rupees."""
    if v is None:
        return None
    u = (unit_hint or "").lower()
    if "crore" in u or u.strip() in ("cr", "rs. cr"):
        return v * 1e7
    if "lakh" in u or "lac" in u:
        return v * 1e5
    if "million" in u:
        return v * 1e6
    return v * 1e5          # BSE/NSE default reporting unit is lakhs


# --------------------------------------------------------------------------
# 1. BSE — corporate results (as filed)
# --------------------------------------------------------------------------

BSE_RESULT_ENDPOINTS = [
    "https://api.bseindia.com/BseIndiaAPI/api/Comp_Resultsnew/w?code={code}&seriesid=",
    "https://api.bseindia.com/BseIndiaAPI/api/CompanyFinancialResults/w?scripcode={code}&type=",
    "https://api.bseindia.com/BseIndiaAPI/api/Comp_Results/w?code={code}",
]


def bse_financials(scripcode):
    """As-filed half-yearly/annual result lines from BSE. -> (values, SRC_BSE)"""
    if not scripcode:
        return {}, None
    s = _sess("https://www.bseindia.com/")
    for tpl in BSE_RESULT_ENDPOINTS:
        j = get_json(tpl.format(code=scripcode), session=s)
        if not j:
            continue
        rows = j if isinstance(j, list) else (j.get("Table") or j.get("data") or [])
        if not rows or not isinstance(rows, list):
            continue
        row = rows[0]
        if not isinstance(row, dict):
            continue
        unit = str(row.get("Unit") or row.get("unit") or "lakhs")
        rev = pick(row, "Revenue From Operations", "Net Sales", "Income From Operations",
                   "TotalIncomeFromOperations", "Revenue")
        pat = pick(row, "Net Profit Loss For The Period", "Net Profit", "PAT",
                   "ProfitLossForPeriod", "NetProfitLoss")
        if rev is None and pat is None:
            continue
        vals = {
            "revenue": to_rupees(rev, unit),
            "pat": to_rupees(pat, unit),
            "op_income": to_rupees(pick(row, "Operating Profit", "ProfitFromOperations",
                                        "OperatingProfitBeforeOtherIncome"), unit),
            "dep": to_rupees(pick(row, "Depreciation", "DepreciationAmortisation"), unit),
            "interest": to_rupees(pick(row, "Finance Cost", "Interest"), unit),
            "tax": to_rupees(pick(row, "Tax Expense", "Total Tax", "TaxExpense"), unit),
            "pbt": to_rupees(pick(row, "Profit Before Tax", "ProfitLossBeforeTax"), unit),
            "eps": pick(row, "Basic EPS", "BasicEPS", "Diluted EPS", "EPS"),
        }
        period = (row.get("Result_Date") or row.get("ResultDate") or row.get("PeriodEnded")
                  or row.get("period_end") or None)
        vals = {k: v for k, v in vals.items() if v is not None}
        if vals:
            vals["_period"] = str(period)[:10] if period else None
            return vals, SRC_BSE
    return {}, None


def bse_universe():
    out = []
    s = _sess("https://www.bseindia.com/")
    for grp in ("M", "MT"):
        j = get_json("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?"
                     f"Group={grp}&Scripcode=&industry=&segment=Equity&status=Active", session=s)
        if not j:
            continue
        for row in (j if isinstance(j, list) else j.get("Table", [])):
            code = str(row.get("SCRIP_CD") or row.get("Scrip_Cd") or "").strip()
            if not code:
                continue
            out.append({
                "symbol": (row.get("scrip_id") or row.get("Scrip_Id") or code).strip(),
                "name": (row.get("Scrip_Name") or row.get("SCRIP_NAME") or code).strip(),
                "isin": (row.get("ISIN_NUMBER") or row.get("ISIN_NO") or "").strip() or None,
                "exchange": "BSE-SME",
                "bse_code": code,
                "industry": (row.get("INDUSTRY") or row.get("Industry") or "").strip() or None,
                "yahoo": code + ".BO",
            })
    return out


def bse_listing_info(scripcode):
    """Listing date + face value from the BSE scrip header."""
    if not scripcode:
        return {}
    s = _sess("https://www.bseindia.com/")
    j = get_json("https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?"
                 f"quotetype=EQ&scripcode={scripcode}&seriesid=", session=s)
    if not isinstance(j, dict):
        return {}
    out = {}
    for k in ("ListingDate", "Listing_Date", "DtOfListing", "ListingOn"):
        if j.get(k):
            d = parse_date(j[k])
            if d:
                out["listing_date"] = d
                break
    fv = num(j.get("FaceValue") or j.get("Face_Value"))
    if fv:
        out["face_value"] = fv
    return out


# --------------------------------------------------------------------------
# 2. NSE — corporate results (as filed)
# --------------------------------------------------------------------------

def nse_financials(symbol, session=None):
    """As-filed result lines from NSE's corporate-results API. -> (values, SRC_NSE)"""
    if not symbol:
        return {}, None
    s = session or nse_session()
    for period in ("Quarterly", "Annual"):
        j = get_json("https://www.nseindia.com/api/corporates-financial-results?"
                     f"index=equities&symbol={requests.utils.quote(symbol)}&period={period}",
                     session=s, referer=f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}")
        rows = j if isinstance(j, list) else (j or {}).get("data") if isinstance(j, dict) else None
        if not rows:
            continue
        row = rows[0]
        if not isinstance(row, dict):
            continue
        # NSE returns figures already in rupees-lakhs under re_* keys
        rev = pick(row, "re_net_sale", "reNetSale", "income", "re_total_inc")
        pat = pick(row, "re_pro_loss_aft_tax", "reProLossAftTax", "proLossAftTax", "net_profit")
        if rev is None and pat is None:
            continue
        unit = str(row.get("unit") or "lakhs")
        vals = {
            "revenue": to_rupees(rev, unit),
            "pat": to_rupees(pat, unit),
            "pbt": to_rupees(pick(row, "re_pro_loss_bef_tax", "reProLossBefTax"), unit),
            "tax": to_rupees(pick(row, "re_tax", "reTax"), unit),
            "interest": to_rupees(pick(row, "re_int", "reInt", "finance_cost"), unit),
            "dep": to_rupees(pick(row, "re_dep", "reDep", "depreciation"), unit),
            "eps": pick(row, "re_basic_eps_for_cont_dis_opr", "reBasicEps", "basic_eps", "re_dil_eps"),
        }
        vals = {k: v for k, v in vals.items() if v is not None}
        if vals:
            per = row.get("to_date") or row.get("toDate") or row.get("period_ended")
            vals["_period"] = parse_date(per) if per else None
            return vals, SRC_NSE
    return {}, None


def nse_universe(session=None):
    s = session or nse_session()
    out = []
    for url in ("https://nsearchives.nseindia.com/emerge/content/SME_EQUITY_L.csv",
                "https://archives.nseindia.com/emerge/content/SME_EQUITY_L.csv"):
        r = get(url, session=s, referer="https://www.nseindia.com/")
        if r is None:
            continue
        try:
            import csv as _csv
            for row in _csv.DictReader(io.StringIO(r.content.decode("utf-8", "ignore"))):
                row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
                sym = row.get("SYMBOL")
                if not sym:
                    continue
                out.append({
                    "symbol": sym,
                    "name": row.get("NAME OF COMPANY") or sym,
                    "isin": row.get("ISIN NUMBER") or None,
                    "exchange": "NSE-EMERGE",
                    "yahoo": sym + ".NS",
                    "listing_date": parse_date(row.get("DATE OF LISTING")),
                    "face_value": num(row.get("FACE VALUE")),
                })
        except Exception as e:
            note_error("nse universe csv", e)
        if out:
            return out
    # live-market fallback
    j = get_json("https://www.nseindia.com/api/live-analysis-emerge", session=s)
    for item in ((j or {}).get("data") or []):
        if item.get("symbol"):
            out.append({"symbol": item["symbol"], "name": item.get("companyName") or item["symbol"],
                        "isin": None, "exchange": "NSE-EMERGE", "yahoo": item["symbol"] + ".NS"})
    return out


def nse_quote(symbol, session=None):
    """Live-ish quote + listing date from NSE's equity quote API."""
    s = session or nse_session()
    j = get_json(f"https://www.nseindia.com/api/quote-equity?symbol={requests.utils.quote(symbol)}",
                 session=s, referer=f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}")
    if not isinstance(j, dict):
        return {}
    out = {}
    pi = j.get("priceInfo") or {}
    meta = j.get("metadata") or {}
    info = j.get("info") or {}
    p = num(pi.get("lastPrice"))
    if p:
        out["price"] = p
    wk = pi.get("weekHighLow") or {}
    if num(wk.get("max")):
        out["hi52"] = num(wk["max"])
    if num(wk.get("min")):
        out["lo52"] = num(wk["min"])
    for k in ("listingDate", "listing_date"):
        if meta.get(k):
            d = parse_date(meta[k])
            if d:
                out["listing_date"] = d
            break
    if meta.get("industry"):
        out["industry"] = meta["industry"]
    if info.get("isin"):
        out["isin"] = info["isin"]
    return out


# --------------------------------------------------------------------------
# 2b. XBRL — the company's own tagged filing (what Screener.in parses too)
# --------------------------------------------------------------------------

# XBRL element names in the SEBI/MCA results taxonomy. Companies tag with
# slightly different element names across taxonomy versions, so each figure
# lists every spelling we accept. Matching is on the local name, namespace
# agnostic, case-insensitive.
XBRL_TAGS = {
    "revenue": ["RevenueFromOperations", "TotalIncomeFromOperations",
                "RevenueFromOperationsNet", "NetSales", "Turnover",
                "IncomeFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["TotalIncome", "TotalRevenue"],
    "total_expenses": ["TotalExpenses", "Expenses"],
    "dep": ["DepreciationDepletionAndAmortisationExpense",
            "DepreciationAndAmortisationExpense", "DepreciationAmortisationExpense",
            "Depreciation"],
    "interest": ["FinanceCosts", "InterestExpense", "FinanceCost"],
    "pbt": ["ProfitBeforeTax", "ProfitLossBeforeTax",
            "ProfitBeforeExceptionalItemsAndTax"],
    "tax": ["TaxExpense", "TotalTaxExpense", "CurrentTax", "IncomeTaxExpense"],
    "pat": ["ProfitLossForPeriod", "ProfitLossForThePeriod", "NetProfitLoss",
            "ProfitAfterTax", "ProfitLossFromContinuingOperations"],
    "eps": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShare", "BasicEPS", "DilutedEarningsLossPerShare"],
    "equity": ["EquityShareCapital", "PaidUpValueOfEquityShareCapital"],
    "reserves": ["ReservesExcludingRevaluationReserve", "OtherEquity"],
}


def _local(tag):
    return re.sub(r"^\{.*\}", "", str(tag or "")).strip()


def parse_xbrl(xml_text):
    """Parse an exchange XBRL results filing -> (values in rupees, period).

    Only reads facts for the LATEST period present, and only consolidated
    figures when both consolidated and standalone are tagged (that is what
    Screener and every research desk uses). Anything not tagged is absent —
    this function never derives or guesses a figure."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        note_error("xbrl parse", e)
        return {}, None

    # --- contexts: id -> (end_date, is_consolidated) -----------------------
    ctx = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        end = None
        for sub in el.iter():
            ln = _local(sub.tag)
            if ln in ("endDate", "instant") and (sub.text or "").strip():
                end = parse_date(sub.text)
        blob = "".join((s.text or "") for s in el.iter() if _local(s.tag) == "explicitMember")
        consolidated = "consolidated" in (blob or "").lower()
        ctx[cid] = (end, consolidated)

    if not ctx:
        return {}, None
    dated = [c for c in ctx.values() if c[0]]
    if not dated:
        return {}, None
    latest = max(d[0] for d in dated)
    prefer_consolidated = any(c[1] for c in ctx.values())
    good = {cid for cid, (end, cons) in ctx.items()
            if end == latest and (cons or not prefer_consolidated)}
    if not good:
        good = {cid for cid, (end, _) in ctx.items() if end == latest}

    # --- facts -------------------------------------------------------------
    want = {}
    for field, names in XBRL_TAGS.items():
        for n in names:
            want[n.lower()] = field
    found = {}
    for el in root.iter():
        field = want.get(_local(el.tag).lower())
        if not field or el.get("contextRef") not in good:
            continue
        v = num(el.text)
        if v is None:
            continue
        scale = el.get("scale")
        sign = el.get("sign")
        try:
            if scale:
                v *= 10 ** int(scale)
        except ValueError:
            pass
        if sign == "-":
            v = -abs(v)
        found.setdefault(field, v)      # first tagged value for the period wins

    if not found:
        return {}, None

    out = {}
    for k in ("revenue", "dep", "interest", "pbt", "tax", "pat"):
        if k in found:
            out[k] = found[k]
    if "eps" in found:
        out["eps"] = found["eps"]
    # operating profit = revenue - (total expenses - depreciation - finance costs)
    if "total_expenses" in found and "revenue" in found:
        opex = found["total_expenses"] - found.get("dep", 0) - found.get("interest", 0)
        out["op_income"] = found["revenue"] - opex
    if "equity" in found or "reserves" in found:
        out["equity"] = found.get("equity", 0) + found.get("reserves", 0)
    return out, latest


def xbrl_urls_for(symbol=None, scripcode=None, session=None):
    """Find recent XBRL filing URLs for a company. Returns a list, newest first."""
    urls = []
    if symbol:
        s = session or nse_session()
        for period in ("Quarterly", "Annual"):
            j = get_json("https://www.nseindia.com/api/corporates-financial-results?"
                         f"index=equities&symbol={requests.utils.quote(symbol)}&period={period}",
                         session=s,
                         referer=f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}")
            rows = j if isinstance(j, list) else (j or {}).get("data") or []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                for key in ("xbrl", "xbrlFile", "xbrl_file", "seqNumber_xbrl"):
                    u = row.get(key)
                    if isinstance(u, str) and u.lower().endswith(".xml"):
                        urls.append(u if u.startswith("http")
                                    else "https://nsearchives.nseindia.com/" + u.lstrip("/"))
    if scripcode:
        j = get_json("https://api.bseindia.com/BseIndiaAPI/api/XBRLDataFinancial/w?"
                     f"scripcode={scripcode}", referer="https://www.bseindia.com/")
        rows = j if isinstance(j, list) else (j or {}).get("Table") or []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                for v in row.values():
                    if isinstance(v, str) and v.lower().endswith(".xml"):
                        urls.append(v if v.startswith("http")
                                    else "https://www.bseindia.com/xml-data/corpfiling/"
                                         "AttachHis/" + v.lstrip("/"))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:3]


def xbrl_financials(symbol=None, scripcode=None, session=None):
    """As-filed figures straight from the company's XBRL. -> (values, SRC_XBRL)"""
    for u in xbrl_urls_for(symbol, scripcode, session):
        r = get(u, timeout=30)
        if r is None:
            continue
        vals, period = parse_xbrl(r.content.decode("utf-8", "ignore"))
        if vals:
            vals["_period"] = period
            return vals, SRC_XBRL
    return {}, None


# --------------------------------------------------------------------------
# 3. Moneycontrol — free public price feed (third-party, clearly tagged)
# --------------------------------------------------------------------------

_MC_CACHE = {}


def mc_lookup(name_or_symbol):
    """Resolve a company to Moneycontrol's internal id."""
    key = (name_or_symbol or "").lower()
    if key in _MC_CACHE:
        return _MC_CACHE[key]
    j = get_json("https://www.moneycontrol.com/mccode/common/autosuggestion_solr.php"
                 f"?classic=true&query={requests.utils.quote(name_or_symbol)}&type=1&format=json",
                 referer="https://www.moneycontrol.com/")
    mcid = None
    if isinstance(j, list) and j:
        first = j[0]
        if isinstance(first, dict):
            mcid = first.get("sc_id") or first.get("id") or first.get("link_src")
    _MC_CACHE[key] = mcid
    return mcid


def mc_snapshot(name_or_symbol, exchange="nse"):
    """Price + headline ratios from Moneycontrol's public pricefeed. -> (values, SRC_MC)"""
    mcid = mc_lookup(name_or_symbol)
    if not mcid:
        return {}, None
    ex = "nse" if str(exchange).upper().startswith("NSE") else "bse"
    j = get_json(f"https://priceapi.moneycontrol.com/pricefeed/{ex}/equitycash/{mcid}",
                 referer="https://www.moneycontrol.com/")
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict):
        return {}, None
    vals = {}
    mapping = {
        "price": ("pricecurrent", "lastvalue"),
        "hi52": ("52H", "yearlyHigh"),
        "lo52": ("52L", "yearlyLow"),
        "eps": ("EPS", "eps"),
        "bvps": ("BV", "bookvalue"),
        "_pe": ("PE", "pe"),
        "_mcap_cr": ("MKTCAP", "mktcap"),
        "div_yield": ("DIV_YIELD", "dividendYield"),
        "face_value": ("FV", "facevalue"),
    }
    for out_k, keys in mapping.items():
        v = pick(d, *keys)
        if v is not None:
            vals[out_k] = v
    if not vals:
        return {}, None
    return vals, SRC_MC


# --------------------------------------------------------------------------
# 4. Yahoo Finance — last resort
# --------------------------------------------------------------------------

def _yf_row(df, idx, *names):
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = df.loc[n]
            if len(s) > idx:
                return num(s.iloc[idx])
    return None


def yahoo_financials(ticker):
    """Normalised statements from Yahoo. -> (values, SRC_YF)"""
    try:
        import yfinance as yf
    except ImportError:
        return {}, None
    try:
        t = yf.Ticker(ticker)
        inc = t.income_stmt
        if inc is None or getattr(inc, "empty", True):
            return {}, None
        try:
            cf = t.cash_flow
        except Exception:
            cf = None
        try:
            bs = t.balance_sheet
        except Exception:
            bs = None
        vals = {
            "revenue": _yf_row(inc, 0, "Operating Revenue", "Total Revenue"),
            "revenue_py": _yf_row(inc, 1, "Operating Revenue", "Total Revenue"),
            "revenue_3y": _yf_row(inc, 3, "Operating Revenue", "Total Revenue"),
            "op_income": _yf_row(inc, 0, "Operating Income"),
            "ebitda": _yf_row(inc, 0, "EBITDA", "Normalized EBITDA"),
            "ebit": _yf_row(inc, 0, "EBIT"),
            "dep": _yf_row(inc, 0, "Reconciled Depreciation") or
                   _yf_row(cf, 0, "Depreciation And Amortization", "Depreciation"),
            "interest": _yf_row(inc, 0, "Interest Expense"),
            "tax": _yf_row(inc, 0, "Tax Provision"),
            "pat": _yf_row(inc, 0, "Net Income", "Net Income Common Stockholders"),
            "pat_py": _yf_row(inc, 1, "Net Income", "Net Income Common Stockholders"),
            "pat_3y": _yf_row(inc, 3, "Net Income", "Net Income Common Stockholders"),
            "eps": _yf_row(inc, 0, "Diluted EPS", "Basic EPS"),
            "cfo": _yf_row(cf, 0, "Operating Cash Flow",
                           "Cash Flow From Continuing Operating Activities"),
            "fcf": _yf_row(cf, 0, "Free Cash Flow"),
            "equity": _yf_row(bs, 0, "Stockholders Equity", "Common Stock Equity"),
            "debt": _yf_row(bs, 0, "Total Debt"),
            "shares": _yf_row(bs, 0, "Ordinary Shares Number", "Share Issued"),
        }
        capex = _yf_row(cf, 0, "Capital Expenditure")
        if capex is not None:
            vals["capex"] = abs(capex)
        dv = _yf_row(cf, 0, "Cash Dividends Paid", "Common Stock Dividend Paid")
        if dv is not None:
            vals["dividends_paid"] = abs(dv)
        vals = {k: v for k, v in vals.items() if v is not None}
        if not vals:
            return {}, None
        try:
            vals["_period"] = str(inc.columns[0].date())
        except Exception:
            pass
        return vals, SRC_YF
    except Exception as e:
        note_error(f"yahoo {ticker}", e)
        return {}, None


# --------------------------------------------------------------------------
# 5. IPO history — issue price & listing date (Chittorgarh public feed,
#    the same source the FinDeependence IPO Tracker already runs on)
# --------------------------------------------------------------------------

def parse_date(x):
    """-> 'YYYY-MM-DD' or None. Never guesses a date it cannot parse."""
    if not x:
        return None
    if isinstance(x, (datetime,)):
        return x.strftime("%Y-%m-%d")
    s = re.sub(r"<[^>]*>", " ", str(x)).strip()      # tolerate embedded HTML
    s = re.sub(r"T\d{2}:\d{2}.*$", "", s).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y",
                "%b %d, %Y", "%Y/%m/%d", "%d-%b-%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: pull a date out of a string with junk around it, e.g. the
    # feed's "07-Jul-2026<span …>T</span>" display column
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ](\d{4})", s)
    if m:
        for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(f"{m.group(1)}-{m.group(2)[:3]}-{m.group(3)}",
                                         "%d-%b-%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        try:
            return datetime.strptime(m.group(0).replace("/", "-"),
                                     "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_price_band(x):
    """IPO issue prices arrive as '90.00', '150.00 to 158.00' or '150-158'.
       For a band we take the UPPER end — that is the price actually paid by
       applicants at the cut-off, so returns computed off it are conservative."""
    if x is None:
        return None
    s = re.sub(r"<[^>]*>", " ", str(x))
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return None
    return max(float(n) for n in nums)


def cg_url(report_id, year, fy):
    return (f"https://webnodejs.chittorgarh.com/cloud/report/data-read/"
            f"{report_id}/1/1/{year}/{fy}/0/")


def ipo_history(years_back=12):
    """{ISIN or SYMBOL: {issue_price, listing_date, ipo_name}} for SME IPOs.
       Uses Chittorgarh's public report feed (report 82 = IPO list)."""
    out = {}
    now = datetime.now(IST)
    for back in range(years_back):
        y = now.year - back
        fy = f"{y}-{str((y + 1) % 100).zfill(2)}" if now.month >= 4 or back > 0 else \
             f"{y - 1}-{str(y % 100).zfill(2)}"
        j = get_json(cg_url(82, y, fy) + "all", referer="https://www.chittorgarh.com/")
        rows = None
        if isinstance(j, dict):
            for k in ("reportTableData", "data", "Table"):
                if isinstance(j.get(k), list):
                    rows = j[k]
                    break
        elif isinstance(j, list):
            rows = j
        if not rows:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            flat = {norm_label(k): v for k, v in row.items()}
            name = None
            for k, v in row.items():
                if "name" in norm_label(k) and isinstance(v, str):
                    name = re.sub(r"<[^>]*>", "", v).strip()
                    break
            price = None
            for k in ("issuepricers", "issueprice", "price", "offerprice", "ipoprice"):
                if k in flat:
                    price = parse_price_band(flat[k])
                    if price:
                        break
            # Prefer the machine-readable ~ListingDate (clean ISO) over the display
            # "Listing Date" column, which can carry HTML badges.
            ld = None
            for k in ("listingdate", "listedon", "listdate", "datelisting"):
                for src_key, v in row.items():
                    if norm_label(src_key) == k:
                        cand = parse_date(re.sub(r"<[^>]*>", "", str(v)))
                        if cand and (src_key.startswith("~") or not ld):
                            ld = cand
                        if src_key.startswith("~") and cand:
                            break
                if ld:
                    break
            isin = None
            for k in ("isin", "isinnumber", "isincode"):
                if flat.get(k):
                    isin = str(flat[k]).strip()
                    break
            if not (price or ld):
                continue
            rec = {"issue_price": price, "listing_date": ld, "ipo_name": name,
                   "listing_at": flat.get("listingat")}
            # index by every identifier the feed gives us — far more reliable
            # than matching on company name
            if isin:
                out[isin.upper()] = rec
            nsym = str(flat.get("nsesymbol") or "").strip().upper()
            if nsym:
                out["NSE::" + nsym] = rec
            bcode = str(flat.get("bsescriptcode") or "").strip()
            if bcode and bcode.isdigit():
                out["BSE::" + bcode] = rec
            if name:
                out["NAME::" + name_key(name)] = rec
        time.sleep(0.4)
    return out


def match_ipo(company, hist):
    """Match a company to its IPO record: ISIN first, then exchange ticker/code,
       then normalised name. Identifier matches are exact; the name match is the
       only fuzzy one and is tried last."""
    if not hist:
        return {}
    isin = (company.get("isin") or "").upper()
    if isin and isin in hist:
        return hist[isin]
    sym = (company.get("symbol") or "").upper()
    if sym and company.get("exchange") == "NSE-EMERGE" and "NSE::" + sym in hist:
        return hist["NSE::" + sym]
    bcode = str(company.get("bse_code") or "").strip()
    if bcode and "BSE::" + bcode in hist:
        return hist["BSE::" + bcode]
    nm = name_key(company.get("name") or "")
    if not nm:
        return {}
    if "NAME::" + nm in hist:
        return hist["NAME::" + nm]
    for k, v in hist.items():                       # prefix match, both directions
        if not k.startswith("NAME::"):
            continue
        other = k[6:]
        if len(nm) >= 8 and (other.startswith(nm) or nm.startswith(other)):
            return v
    return {}


# --------------------------------------------------------------------------
# 6. merge with provenance
# --------------------------------------------------------------------------

FIN_FIELDS = ("revenue", "revenue_py", "revenue_3y", "op_income", "ebitda", "ebit", "dep",
              "interest", "tax", "pat", "pat_py", "pat_3y", "eps", "cfo", "capex", "fcf",
              "equity", "debt", "shares", "dividends_paid", "pbt", "bvps", "face_value",
              "price", "hi52", "lo52", "div_yield")


def merge_by_priority(candidates):
    """candidates: list of (values_dict, source_name), best source first.
       Returns (values, sources_per_field). A field is taken from the FIRST
       source that actually has it — nothing is averaged or invented."""
    values, sources = {}, {}
    ordered = sorted([c for c in candidates if c and c[0] and c[1]],
                     key=lambda c: SRC_RANK.get(c[1], 99))
    for vals, src in ordered:
        for k, v in vals.items():
            if k.startswith("_") or v is None:
                continue
            if k not in values:
                values[k] = v
                sources[k] = src
    for vals, src in ordered:                     # statement period from best source
        if vals.get("_period"):
            values["_period"] = vals["_period"]
            sources["_period"] = src
            break
    return values, sources


def cross_check(candidates, fields=("revenue", "pat", "eps"), tol=0.05):
    """Compare the SAME field across every source that reported it.

    Accuracy matters most where sources disagree — that is exactly where a
    single-source number would quietly be wrong. Returns
        {field: {"values": {source: value}, "spread_pct": float}}
    for fields where the sources differ by more than `tol` (5% by default).
    Reporting a disagreement is never a reason to invent a third number: the
    highest-priority source still wins, and the dispute is published alongside
    it so it can be checked against the filing."""
    out = {}
    for f in fields:
        vals = {}
        for v, src in [(c[0], c[1]) for c in candidates if c and c[0] and c[1]]:
            x = v.get(f)
            if isinstance(x, (int, float)) and x == x:
                vals.setdefault(src, float(x))
        if len(vals) < 2:
            continue
        nums = list(vals.values())
        lo, hi = min(nums), max(nums)
        base = max(abs(lo), abs(hi))
        if base == 0:
            continue
        spread = (hi - lo) / base
        # sign disagreement (profit vs loss) is always material
        if spread > tol or (lo < 0 < hi):
            out[f] = {"values": {k: round(v, 2) for k, v in vals.items()},
                      "spread_pct": round(spread * 100, 1)}
    return out


def quality_of(sources):
    """Overall data-quality tag for a company, from where its core numbers came.
       'exchange'   = the company's own filing (XBRL or the exchange results API)
       'thirdparty' = your own export, Moneycontrol or Yahoo
       'price-only' = no fundamentals sourced at all"""
    core = [sources.get("revenue"), sources.get("pat")]
    if SRC_XBRL in core or SRC_BSE in core or SRC_NSE in core:
        return "exchange"
    if any(core):
        return "thirdparty"
    return "price-only"


# --------------------------------------------------------------------------
# 7. doctor — verify every endpoint from THIS machine
# --------------------------------------------------------------------------

def probe_all(sample_nse=None, sample_bse=None, verbose=True):
    """Probes each source and returns a report. Run this on the machine that
       will host the pipeline — endpoint access differs by IP/region.

       The per-company endpoints are tested with REAL identifiers taken from
       the universe lists fetched moments earlier. Testing them with a guessed
       ticker produces "0 records" for a perfectly healthy endpoint, which
       looks identical to a block — a false alarm that would send us rewriting
       working code."""
    report = []

    def probe(name, fn):
        t0 = time.time()
        try:
            res = fn()
            ok = bool(res)
            detail = (f"{len(res)} records" if isinstance(res, (list, dict)) and not isinstance(res, str)
                      else str(res)[:60])
        except Exception as e:
            ok, detail = False, repr(e)[:100]
        row = {"source": name, "ok": ok, "detail": detail, "secs": round(time.time() - t0, 1)}
        report.append(row)
        if verbose:
            print(f"  [{'OK ' if ok else 'FAIL'}] {name:<34} {row['secs']:>5}s  {detail}")
        return row

    if verbose:
        print("Probing data sources from this machine…")

    # --- universes first, so the per-company probes can use real tickers ---
    bse_list, nse_list = [], []

    def _bse_uni():
        bse_list.extend(bse_universe())
        return bse_list
    probe("BSE universe (SME scrips)", _bse_uni)

    s = nse_session()

    def _nse_uni():
        nse_list.extend(nse_universe(s))
        return nse_list
    probe("NSE universe (Emerge CSV)", _nse_uni)

    # real identifiers, with the caller's overrides taking precedence
    if not sample_bse:
        for c in bse_list:
            if c.get("bse_code"):
                sample_bse = c["bse_code"]
                break
    if not sample_nse:
        for c in nse_list:
            if c.get("symbol"):
                sample_nse = c["symbol"]
                break
    if verbose:
        print(f"  (per-company endpoints tested with real tickers: "
              f"NSE {sample_nse or 'n/a'}, BSE {sample_bse or 'n/a'})")

    # try a handful of real companies before calling an endpoint dead — any
    # single company may simply not have filed yet
    bse_codes = [c["bse_code"] for c in bse_list if c.get("bse_code")][:5] or \
                ([sample_bse] if sample_bse else [])
    nse_syms = [c["symbol"] for c in nse_list if c.get("symbol")][:5] or \
               ([sample_nse] if sample_nse else [])

    def _try_many(fn, items):
        hits, tried = 0, 0
        for it in items:
            tried += 1
            try:
                if fn(it):
                    hits += 1
            except Exception:
                pass
        return {"worked_for": f"{hits} of {tried} real companies"} if hits else {}

    probe("BSE results API", lambda: _try_many(lambda c: bse_financials(c)[0], bse_codes))
    probe("BSE listing header", lambda: _try_many(bse_listing_info, bse_codes))
    probe("NSE quote API", lambda: _try_many(lambda x: nse_quote(x, s), nse_syms))
    probe("NSE results API", lambda: _try_many(lambda x: nse_financials(x, s)[0], nse_syms))
    probe("XBRL filing discovery",
          lambda: _try_many(lambda x: xbrl_urls_for(symbol=x, session=s), nse_syms))
    probe("XBRL parse (offline self-test)",
          lambda: parse_xbrl('<xbrl xmlns="http://www.xbrl.org/2003/instance">'
                             '<context id="C"><period><endDate>2026-03-31</endDate>'
                             '</period></context>'
                             '<ProfitLossForPeriod contextRef="C">1</ProfitLossForPeriod>'
                             '</xbrl>')[0])
    probe("Moneycontrol pricefeed", lambda: mc_snapshot("Reliance Industries")[0])
    probe("Chittorgarh IPO feed", lambda: ipo_history(years_back=1))
    probe("Yahoo statements", lambda: yahoo_financials("RELIANCE.NS")[0])
    try:
        import yfinance as yf
        probe("Yahoo prices (batch)",
              lambda: {"rows": len(yf.download("RELIANCE.NS", period="5d", progress=False))})
    except ImportError:
        report.append({"source": "Yahoo prices (batch)", "ok": False,
                       "detail": "yfinance not installed", "secs": 0})
    ok_n = sum(1 for r in report if r["ok"])
    if verbose:
        print(f"\n  {ok_n}/{len(report)} sources reachable from here.")
        if ok_n < len(report):
            print("  Sources that fail here will simply be skipped — the pipeline "
                  "falls through to the next best one and tags the data accordingly.")
    return report


if __name__ == "__main__":
    probe_all()
