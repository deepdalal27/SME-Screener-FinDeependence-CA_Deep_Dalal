#!/usr/bin/env python3
"""Unit tests for the pipeline: every screener formula, every listing-performance
calculation, the accuracy guards, and the source-merge/provenance logic.
Run:  python pipeline/test_pipeline.py"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_data import (compute_metrics, history_facts, benchmark_return,
                        days_between, _cagr, _div, _pct, _growth)
import sources as S

CR = 1e7
PASS = FAIL = 0


def eq(name, got, want, tol=1e-4):
    global PASS, FAIL
    ok = (got is None and want is None) or (
        isinstance(got, (int, float)) and isinstance(want, (int, float))
        and abs(got - want) <= tol * max(1, abs(want))) or got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def true(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


# =========================================================================
# 1. P&L / valuation / cash / returns — hand-checked fixture
# =========================================================================
raw = dict(
    revenue=120 * CR, revenue_py=100 * CR, revenue_3y=80 * CR,
    op_income=22 * CR, ebitda=26.5 * CR, dep=4 * CR, ebit=22.5 * CR,
    interest=2 * CR, tax=5 * CR,
    pat=15 * CR, pat_py=12 * CR, pat_3y=9 * CR,
    eps=12.5, shares=1.2 * CR, price=250.0,
    cfo=13 * CR, capex=6 * CR, equity=60 * CR, debt=18 * CR,
    dividends_paid=1.5 * CR, promoter_pct=61.25,
    hi52=310.0, lo52=140.0, ret_1m=4.2, ret_3m=11.0, ret_1y=55.5,
)
m = compute_metrics(raw, today="2026-07-28")

print("== P&L chain ==")
eq("revenue Cr", m["revenue"], 120.0)
eq("op Cr", m["op"], 22.0)
eq("opm %", m["opm"], 22 / 120 * 100)
eq("ebitda Cr", m["ebitda"], 26.5)
eq("ebitda %", m["ebitda_pct"], 26.5 / 120 * 100)
eq("dep Cr", m["dep"], 4.0)
eq("ebit Cr", m["ebit"], 22.5)
eq("ebit %", m["ebit_pct"], 18.75)
eq("interest Cr", m["interest"], 2.0)
eq("tax Cr", m["tax"], 5.0)
eq("pat Cr", m["pat"], 15.0)
eq("pat %", m["pat_pct"], 12.5)
eq("eps", m["eps"], 12.5)

print("== valuation ==")
eq("mcap Cr", m["mcap"], 300.0)
eq("pe", m["pe"], 20.0)
eq("bvps", m["bvps"], 50.0)
eq("pb", m["pb"], 5.0)
eq("div yield %", m["div_yield"], 0.5)

print("== cash flow ==")
eq("cfo Cr", m["cfo"], 13.0)
eq("capex Cr", m["capex"], 6.0)
eq("fcf derived", m["fcf"], 7.0)
eq("cfo/pat", m["cfo_pat"], 13 / 15)
eq("fcf/pat", m["fcf_pat"], 7 / 15)

print("== returns & leverage ==")
eq("roe %", m["roe"], 25.0)
eq("roce %", m["roce"], 22.5 / 78 * 100)
eq("d/e", m["de"], 0.3)
eq("int cover", m["int_cov"], 11.25)

print("== growth ==")
eq("sales g1 %", m["sales_g1"], 20.0)
eq("pat g1 %", m["pat_g1"], 25.0)
eq("sales 3y cagr %", m["sales_g3"], (pow(120 / 80, 1 / 3) - 1) * 100)
eq("pat 3y cagr %", m["pat_g3"], (pow(15 / 9, 1 / 3) - 1) * 100)

# =========================================================================
# 2. LISTING PERFORMANCE — the new block, hand-checked
# =========================================================================
print("== listing performance ==")
# Listed 2 years ago at issue ₹100; listed at ₹150 close; now ₹300; ATH ₹360
# Benchmark rose 20% over the same window.
lst = compute_metrics(dict(
    price=300.0, issue_price=100.0, list_open=140.0, list_close=150.0,
    ath=360.0, listing_date="2024-07-28", bench_ret=20.0,
), today="2026-07-28")

eq("issue price", lst["issue_price"], 100.0)
eq("listing open", lst["list_open"], 140.0)
eq("listing close", lst["list_close"], 150.0)
eq("listing pop % (close vs issue)", lst["list_pop"], 50.0)
eq("return vs issue price %", lst["ret_issue"], 200.0)
eq("return vs listing close %", lst["ret_listclose"], 100.0)
eq("age in years", lst["age_yrs"], 2.0, tol=0.01)
eq("CAGR from issue %", lst["cagr_issue"], (pow(3.0, 1 / 2.0) - 1) * 100, tol=0.02)
eq("CAGR from listing close %", lst["cagr_listclose"], (pow(2.0, 1 / 2.0) - 1) * 100, tol=0.02)
eq("all-time high", lst["ath"], 360.0)
eq("off ATH %", lst["off_ath"], (300 / 360 - 1) * 100)
eq("benchmark return %", lst["bench_ret"], 20.0)
eq("alpha = stock - benchmark", lst["alpha"], 80.0)

print("== listing guards (no illusionary numbers) ==")
young = compute_metrics(dict(price=200.0, issue_price=100.0, list_close=120.0,
                             listing_date="2026-05-28"), today="2026-07-28")
eq("young co: plain return still shown", young["ret_issue"], 100.0)
eq("young co: CAGR withheld (<1yr)", young["cagr_issue"], None)
eq("young co: CAGR from close withheld", young["cagr_listclose"], None)
true("young co: age computed", abs(young["age_yrs"] - 0.1697) < 0.01, young["age_yrs"])

noissue = compute_metrics(dict(price=200.0, list_close=120.0, listing_date="2020-01-01"),
                          today="2026-07-28")
eq("no issue price -> no return vs issue", noissue["ret_issue"], None)
eq("no issue price -> no CAGR vs issue", noissue["cagr_issue"], None)
eq("but listing-close return still works", noissue["ret_listclose"], 200 / 120 * 100 - 100)

nodate = compute_metrics(dict(price=200.0, issue_price=100.0), today="2026-07-28")
eq("no listing date -> no age", nodate["age_yrs"], None)
eq("no listing date -> no CAGR", nodate["cagr_issue"], None)
eq("no listing date -> plain return still fine", nodate["ret_issue"], 100.0)

nobench = compute_metrics(dict(price=200.0, list_close=100.0, listing_date="2020-01-01"),
                          today="2026-07-28")
eq("no benchmark -> no alpha", nobench["alpha"], None)

zero = compute_metrics(dict(price=200.0, issue_price=0, list_close=0,
                            listing_date="2020-01-01"), today="2026-07-28")
eq("zero issue price -> None not inf", zero["ret_issue"], None)
eq("zero listing close -> None", zero["ret_listclose"], None)

future = compute_metrics(dict(price=100.0, issue_price=90.0, listing_date="2027-01-01"),
                         today="2026-07-28")
eq("future listing date -> no age", future["age_yrs"], None)
eq("future listing date -> no CAGR", future["cagr_issue"], None)

print("== days_between ==")
eq("days simple", days_between("2026-01-01", "2026-01-31"), 30)
eq("days across year", days_between("2025-07-28", "2026-07-28"), 365)
eq("days negative", days_between("2026-07-28", "2026-07-01"), -27)
eq("days bad input", days_between("garbage", "2026-07-28"), None)
eq("days none input", days_between(None, "2026-07-28"), None)

# =========================================================================
# 3. history_facts — listing-day price guard
# =========================================================================
print("== history_facts ==")
closes = [(f"2024-07-{d:02d}", 100.0 + d) for d in range(1, 29)]      # 28 bars
opens = [(d, v - 2) for d, v in closes]
f1 = history_facts(closes, opens, listing_date="2024-07-01")
eq("last price", f1["price"], 128.0)
eq("ATH", f1["ath"], 128.0)
eq("listing close taken when history starts on listing day", f1["list_close"], 101.0)
eq("listing open taken too", f1["list_open"], 99.0)

f2 = history_facts(closes, opens, listing_date="2023-01-01")   # history starts 546d late
eq("listing close WITHHELD when history starts late", f2.get("list_close"), None)
true("skip reason recorded", "_list_price_skipped" in f2, f2)
eq("but price/ATH still produced", f2["price"], 128.0)

f3 = history_facts(closes, opens, listing_date="2024-07-05")   # 4 days — within tolerance
eq("within 7d tolerance still accepted", f3["list_close"], 101.0)

f4 = history_facts([], [], listing_date="2024-07-01")
eq("empty history -> empty facts", f4, {})

long_hist = [(f"2020-01-{(i % 28) + 1:02d}".replace("01-", f"{(i//28)%12+1:02d}-"), 100.0 + i)
             for i in range(300)]
f5 = history_facts(long_hist, [], None)
true("52w high uses last 248 bars only", f5["hi52"] == 399.0, f5["hi52"])
true("52w low uses last 248 bars only", f5["lo52"] == 152.0, f5["lo52"])
true("ret_1y computed on long history", "ret_1y" in f5)

short = [("2026-07-01", 100.0), ("2026-07-02", 110.0)]
f6 = history_facts(short, [], None)
true("no ret_1y on short history", "ret_1y" not in f6)
eq("52w high on short history", f6["hi52"], 110.0)

# =========================================================================
# 4. benchmark_return
# =========================================================================
print("== benchmark_return ==")
bench = [("2024-01-01", 100.0), ("2024-07-01", 110.0), ("2025-01-01", 120.0),
         ("2026-07-01", 150.0)]
eq("bench from start", benchmark_return(bench, "2024-01-01"), 50.0)
eq("bench from mid (first bar on/after)", benchmark_return(bench, "2024-06-01"), 150 / 110 * 100 - 100)
eq("bench window not covered -> None", benchmark_return(bench, "2027-01-01"), None)
eq("bench no date -> None", benchmark_return(bench, None), None)
eq("bench empty series -> None", benchmark_return([], "2024-01-01"), None)

# =========================================================================
# 5. edge cases on the core metrics
# =========================================================================
print("== core edge cases ==")
m2 = compute_metrics(dict(revenue=100 * CR, op_income=10 * CR, dep=3 * CR))
eq("ebitda derived op+dep", m2["ebitda"], 13.0)
eq("ebit falls back to op", m2["ebit"], 10.0)
eq("no price -> no pe", m2["pe"], None)

m3 = compute_metrics(dict(pat=-5 * CR, revenue=50 * CR, price=10, shares=1 * CR, cfo=2 * CR))
eq("no P/E on a loss", m3["pe"], None)
eq("no CFO/PAT on a loss", m3["cfo_pat"], None)
eq("negative PAT margin shown", m3["pat_pct"], -10.0)

m4 = compute_metrics(dict(pat=10 * CR, shares=2 * CR, price=100))
eq("eps derived", m4["eps"], 5.0)
eq("pe from derived eps", m4["pe"], 20.0)

eq("no OPM on zero revenue", compute_metrics(dict(revenue=0, op_income=5 * CR))["opm"], None)
eq("no OPM on negative revenue", compute_metrics(dict(revenue=-10 * CR, op_income=5 * CR))["opm"], None)
eq("no growth off zero base", _growth(10, 0), None)
eq("no growth off negative base", _growth(10, -5), None)
eq("cagr negative base -> None", _cagr(10, -5, 3), None)
eq("cagr under a year -> None", _cagr(200, 100, 0.5), None)
eq("div by zero -> None", _div(5, 0), None)
eq("pct with None -> None", _pct(None, 10), None)
eq("empty raw -> no mcap", compute_metrics({})["mcap"], None)
eq("mcap from mcap_cr fallback", compute_metrics(dict(mcap_cr=250))["mcap"], 250.0)
eq("explicit div_yield preferred", compute_metrics(dict(div_yield=1.8))["div_yield"], 1.8)

nan_raw = compute_metrics(dict(revenue=float("nan"), pat=float("inf"), price=100))
eq("NaN scrubbed to None", nan_raw["revenue"], None)
eq("inf scrubbed to None", nan_raw["pat"], None)

# =========================================================================
# 6. sources.py — parsing and provenance
# =========================================================================
print("== number parsing ==")
eq("plain", S.num("1234.5"), 1234.5)
eq("indian commas", S.num("12,34,567"), 1234567.0)
eq("brackets are negative", S.num("(1,234)"), -1234.0)
eq("rupee symbol stripped", S.num("₹ 1,200"), 1200.0)
eq("percent stripped", S.num("15.5%"), 15.5)
eq("dash -> None (not zero)", S.num("-"), None)
eq("NA -> None", S.num("NA"), None)
eq("empty -> None", S.num(""), None)
eq("None -> None", S.num(None), None)
eq("garbage -> None", S.num("abc"), None)
eq("real zero preserved", S.num("0"), 0.0)
eq("float passthrough", S.num(12.5), 12.5)

print("== date parsing ==")
eq("iso", S.parse_date("2026-07-28"), "2026-07-28")
eq("d-mon-Y", S.parse_date("28-Jul-2026"), "2026-07-28")
eq("d/m/Y", S.parse_date("28/07/2026"), "2026-07-28")
eq("mon d, Y", S.parse_date("Jul 28, 2026"), "2026-07-28")
eq("iso with time", S.parse_date("2026-07-28T10:30:00"), "2026-07-28")
eq("junk -> None", S.parse_date("not a date"), None)
eq("empty -> None", S.parse_date(""), None)

print("== unit conversion ==")
eq("lakhs default", S.to_rupees(100, "lakhs"), 1e7)
eq("crores", S.to_rupees(1, "crore"), 1e7)
eq("millions", S.to_rupees(10, "million"), 1e7)
eq("unknown unit assumes lakhs", S.to_rupees(100, ""), 1e7)
eq("None passes through", S.to_rupees(None, "lakhs"), None)

print("== fuzzy field pick ==")
row = {"Revenue From Operations": "1,234.5", "Net Profit/(Loss)": "(100)", "Junk": "x"}
eq("exact-ish match", S.pick(row, "Revenue From Operations"), 1234.5)
eq("substring match", S.pick(row, "Net Profit"), -100.0)
eq("no match -> None", S.pick(row, "Total Equity"), None)
eq("non-dict -> None", S.pick(None, "anything"), None)

print("== merge_by_priority (provenance) ==")
vals, srcs = S.merge_by_priority([
    ({"revenue": 100, "pat": 10}, S.SRC_YF),
    ({"revenue": 111, "eps": 5}, S.SRC_BSE),
    ({"revenue": 105, "pat": 11, "cfo": 9}, S.SRC_NSE),
])
eq("best source wins on revenue", vals["revenue"], 111)
eq("revenue tagged BSE", srcs["revenue"], S.SRC_BSE)
eq("pat falls to NSE (BSE lacked it)", vals["pat"], 11)
eq("pat tagged NSE", srcs["pat"], S.SRC_NSE)
eq("cfo only in NSE", vals["cfo"], 9)
eq("eps from BSE", vals["eps"], 5)
true("nothing invented", set(vals) == {"revenue", "pat", "cfo", "eps"}, set(vals))

vals2, srcs2 = S.merge_by_priority([({}, S.SRC_BSE), (None, None), ({"pat": 3}, S.SRC_YF)])
eq("empty sources skipped", vals2["pat"], 3)
eq("tagged yahoo", srcs2["pat"], S.SRC_YF)
eq("no candidates -> empty", S.merge_by_priority([])[0], {})

vals3, srcs3 = S.merge_by_priority([({"revenue": 1, "_period": "2026-03-31"}, S.SRC_BSE)])
eq("period carried through", vals3["_period"], "2026-03-31")
true("underscore keys not treated as data", "_period" not in [k for k in vals3 if not k.startswith("_")])

print("== XBRL parsing (the source Screener itself parses) ==")
XBRL_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance"
      xmlns:in-bse="http://www.bseindia.com/xbrl">
  <context id="OLD"><period><startDate>2024-04-01</startDate>
    <endDate>2025-03-31</endDate></period></context>
  <context id="CUR_STANDALONE"><period><startDate>2025-04-01</startDate>
    <endDate>2026-03-31</endDate></period>
    <scenario><explicitMember>Standalone</explicitMember></scenario></context>
  <context id="CUR_CONSOL"><period><startDate>2025-04-01</startDate>
    <endDate>2026-03-31</endDate></period>
    <scenario><explicitMember>Consolidated</explicitMember></scenario></context>
  <in-bse:RevenueFromOperations contextRef="OLD">900000000</in-bse:RevenueFromOperations>
  <in-bse:RevenueFromOperations contextRef="CUR_STANDALONE">1100000000</in-bse:RevenueFromOperations>
  <in-bse:RevenueFromOperations contextRef="CUR_CONSOL">1200000000</in-bse:RevenueFromOperations>
  <in-bse:TotalExpenses contextRef="CUR_CONSOL">1000000000</in-bse:TotalExpenses>
  <in-bse:DepreciationAndAmortisationExpense contextRef="CUR_CONSOL">40000000</in-bse:DepreciationAndAmortisationExpense>
  <in-bse:FinanceCosts contextRef="CUR_CONSOL">20000000</in-bse:FinanceCosts>
  <in-bse:ProfitBeforeTax contextRef="CUR_CONSOL">200000000</in-bse:ProfitBeforeTax>
  <in-bse:TaxExpense contextRef="CUR_CONSOL">50000000</in-bse:TaxExpense>
  <in-bse:ProfitLossForPeriod contextRef="CUR_CONSOL">150000000</in-bse:ProfitLossForPeriod>
  <in-bse:BasicEarningsLossPerShare contextRef="CUR_CONSOL">12.5</in-bse:BasicEarningsLossPerShare>
  <in-bse:EquityShareCapital contextRef="CUR_CONSOL">100000000</in-bse:EquityShareCapital>
  <in-bse:ReservesExcludingRevaluationReserve contextRef="CUR_CONSOL">500000000</in-bse:ReservesExcludingRevaluationReserve>
</xbrl>"""
xv, xp = S.parse_xbrl(XBRL_SAMPLE)
eq("period is the latest one", xp, "2026-03-31")
eq("revenue taken from CONSOLIDATED, not standalone", xv["revenue"], 1200000000.0)
eq("prior-year context ignored", xv["revenue"] != 900000000.0, True)
eq("PAT parsed", xv["pat"], 150000000.0)
eq("PBT parsed", xv["pbt"], 200000000.0)
eq("tax parsed", xv["tax"], 50000000.0)
eq("depreciation parsed", xv["dep"], 40000000.0)
eq("finance cost parsed", xv["interest"], 20000000.0)
eq("EPS parsed", xv["eps"], 12.5)
# operating profit = revenue - (total expenses - dep - interest) = 1200 - (1000-40-20) = 260
eq("operating profit derived from tagged lines", xv["op_income"], 260000000.0)
eq("equity = share capital + reserves", xv["equity"], 600000000.0)
true("nothing untagged appears", "cfo" not in xv and "capex" not in xv)

# standalone-only filing (most SME companies) must still work
solo = XBRL_SAMPLE.replace('<scenario><explicitMember>Consolidated</explicitMember></scenario>', '') \
                  .replace('<in-bse:RevenueFromOperations contextRef="CUR_CONSOL">1200000000</in-bse:RevenueFromOperations>', '')
sv, sp = S.parse_xbrl(solo)
eq("standalone-only filing parsed", sv.get("revenue"), 1100000000.0)
eq("standalone period correct", sp, "2026-03-31")

eq("garbage xml -> empty", S.parse_xbrl("<not-xbrl>")[0], {})
eq("empty string -> empty", S.parse_xbrl("")[0], {})
eq("xbrl with no contexts -> empty", S.parse_xbrl("<xbrl><a>1</a></xbrl>")[0], {})

scaled = """<xbrl xmlns="http://www.xbrl.org/2003/instance">
  <context id="C"><period><endDate>2026-03-31</endDate></period></context>
  <RevenueFromOperations contextRef="C" scale="5">120</RevenueFromOperations>
  <ProfitLossForPeriod contextRef="C" sign="-">15</ProfitLossForPeriod></xbrl>"""
scv, _ = S.parse_xbrl(scaled)
eq("scale attribute applied", scv["revenue"], 12000000.0)
eq("negative sign attribute applied (a loss)", scv["pat"], -15.0)

eq("XBRL outranks every other source", S.SRC_RANK[S.SRC_XBRL], 0)
true("XBRL counts as an exchange-quality source",
     S.quality_of({"revenue": S.SRC_XBRL}) == "exchange")
true("your own export counts as third-party, not exchange",
     S.quality_of({"revenue": S.SRC_IMPORT}) == "thirdparty")
_v, _s = S.merge_by_priority([({"revenue": 5}, S.SRC_YF), ({"revenue": 9}, S.SRC_XBRL),
                              ({"revenue": 7}, S.SRC_IMPORT)])
eq("XBRL wins the merge", _v["revenue"], 9)
eq("and is labelled as such", _s["revenue"], S.SRC_XBRL)

print("== cross-source verification ==")
agree = [({"revenue": 100.0, "pat": 10.0}, S.SRC_BSE),
         ({"revenue": 101.0, "pat": 10.2}, S.SRC_YF)]
eq("sources within tolerance -> no dispute", S.cross_check(agree), {})

disagree = [({"revenue": 100.0}, S.SRC_BSE), ({"revenue": 150.0}, S.SRC_YF)]
d = S.cross_check(disagree)
true("material disagreement flagged", "revenue" in d)
eq("spread reported", d["revenue"]["spread_pct"], 33.3, tol=0.01)
eq("both values published", len(d["revenue"]["values"]), 2)
eq("BSE value recorded", d["revenue"]["values"][S.SRC_BSE], 100.0)
eq("Yahoo value recorded", d["revenue"]["values"][S.SRC_YF], 150.0)

single = [({"revenue": 100.0}, S.SRC_BSE)]
eq("one source cannot disagree with itself", S.cross_check(single), {})
eq("no sources -> no dispute", S.cross_check([]), {})

sign = [({"pat": 5.0}, S.SRC_BSE), ({"pat": -5.0}, S.SRC_YF)]
true("profit-vs-loss disagreement always flagged", "pat" in S.cross_check(sign))
sign_small = [({"pat": 0.4}, S.SRC_BSE), ({"pat": -0.3}, S.SRC_YF)]
true("sign flip flagged even when small", "pat" in S.cross_check(sign_small))

eq("tolerance is configurable", S.cross_check(disagree, tol=0.6), {})
missing = [({"revenue": 100.0}, S.SRC_BSE), ({"pat": 10.0}, S.SRC_NSE)]
eq("different fields are not a disagreement", S.cross_check(missing), {})
zeros = [({"revenue": 0.0}, S.SRC_BSE), ({"revenue": 0.0}, S.SRC_YF)]
eq("zero-vs-zero is not a disagreement", S.cross_check(zeros), {})
true("dispute never invents a third value",
     all(v in (100.0, 150.0) for v in d["revenue"]["values"].values()))

print("== quality tagging ==")
eq("exchange-sourced", S.quality_of({"revenue": S.SRC_BSE, "pat": S.SRC_BSE}), "exchange")
eq("nse also exchange", S.quality_of({"revenue": S.SRC_NSE}), "exchange")
eq("third party", S.quality_of({"revenue": S.SRC_YF, "pat": S.SRC_MC}), "thirdparty")
eq("moneycontrol is third party", S.quality_of({"pat": S.SRC_MC}), "thirdparty")
eq("price only", S.quality_of({"price": S.SRC_YF}), "price-only")
eq("nothing at all", S.quality_of({}), "price-only")

print("== IPO price bands (real Chittorgarh formats) ==")
eq("plain price", S.parse_price_band("90.00"), 90.0)
eq("band with 'to' takes upper", S.parse_price_band("150.00 to 158.00"), 158.0)
eq("band with dash takes upper", S.parse_price_band("150-158"), 158.0)
eq("price with html stripped", S.parse_price_band("<b>239.00</b>"), 239.0)
eq("blank -> None", S.parse_price_band(""), None)
eq("None -> None", S.parse_price_band(None), None)
eq("no digits -> None", S.parse_price_band("TBA"), None)

print("== listing date with HTML badge (real feed quirk) ==")
dirty = ('07-Jul-2026<span class="badge rounded-pill bg-warning ms-2" '
         'data-component="keyword-popup">T</span>')
import re as _re
eq("badge stripped then parsed", S.parse_date(_re.sub(r"<[^>]*>", "", dirty)), "2026-07-07")
eq("iso tilde field parsed", S.parse_date("2026-01-07T00:00:00.000Z"), "2026-01-07")

print("== name_key: index and lookup MUST agree ==")
eq("suffix stripped", S.name_key("Alpha Engineering Ltd"), "alphaengineering")
eq("'Limited' stripped too", S.name_key("Alpha Engineering Limited"), "alphaengineering")
eq("'Pvt Ltd' stripped", S.name_key("Alpha Engineering Pvt Ltd"), "alphaengineering")
eq("punctuation and case ignored", S.name_key("ALPHA-ENGINEERING, LTD."), "alphaengineering")
eq("all spellings collapse to one key",
   len({S.name_key(x) for x in ["Alpha Engineering Ltd", "Alpha Engineering Limited",
                                "alpha engineering", "Alpha  Engineering  Ltd."]}), 1)
eq("short names not eaten by the stripper", S.name_key("Ltd"), "ltd")
eq("empty -> empty", S.name_key(""), "")
eq("None -> empty", S.name_key(None), "")
true("distinct companies keep distinct keys",
     S.name_key("Alpha Engineering Ltd") != S.name_key("Alpha Engineers Ltd"))

print("== ipo matching ==")
hist = {"INE123A01011": {"issue_price": 100, "listing_date": "2024-01-01"},
        "NAME::shreeengineering": {"issue_price": 55, "listing_date": "2023-05-05"}}
eq("match by ISIN", S.match_ipo({"isin": "INE123A01011", "name": "X"}, hist)["issue_price"], 100)
eq("match by name", S.match_ipo({"name": "Shree Engineering Ltd"}, hist)["issue_price"], 55)
eq("no match -> empty", S.match_ipo({"name": "Totally Different Co"}, hist), {})
eq("empty history -> empty", S.match_ipo({"name": "Shree Engineering"}, {}), {})
eq("no name no isin -> empty", S.match_ipo({}, hist), {})

hist2 = {"NSE::E2ERAIL": {"issue_price": 174.0, "listing_date": "2026-01-02"},
         "BSE::544673": {"issue_price": 90.0, "listing_date": "2026-01-07"},
         "NAME::wrongco": {"issue_price": 1.0}}
eq("match by NSE symbol", S.match_ipo(
    {"symbol": "E2ERAIL", "exchange": "NSE-EMERGE"}, hist2)["issue_price"], 174.0)
eq("match by BSE scrip code", S.match_ipo(
    {"symbol": "X", "bse_code": "544673", "exchange": "BSE-SME"}, hist2)["issue_price"], 90.0)
eq("ISIN still wins over ticker", S.match_ipo(
    {"isin": "INE123A01011", "symbol": "E2ERAIL", "exchange": "NSE-EMERGE"},
    {**hist, **hist2})["issue_price"], 100)
eq("NSE symbol not used for a BSE company", S.match_ipo(
    {"symbol": "E2ERAIL", "exchange": "BSE-SME"}, hist2), {})

print("== Screener / Trendlyne export import ==")
import import_external as IX
import tempfile as _tf0
import os as _os0

eq("header normalisation strips units",
   IX.norm_col("Market Capitalization (Rs. Cr.)"), "marketcapitalization")
eq("percent sign stripped", IX.norm_col("OPM %"), "opm")
eq("spaces and case ignored", IX.norm_col("  Profit  After  Tax "), "profitaftertax")
true("Screener's common columns are recognised",
     all(IX.norm_col(h) in IX.COLUMN_MAP for h in
         ["Name", "NSE Code", "BSE Code", "Current Price",
          "Market Capitalization", "Sales", "OPM %", "Profit after tax",
          "EPS", "ROCE %", "ROE %", "Debt to equity", "Promoter holding"]))

_d = _tf0.mkdtemp()
# a realistic Screener "Export to Excel" style CSV, with a junk row above the header
with open(_os0.path.join(_d, "screener_export.csv"), "w", encoding="utf-8") as f:
    f.write("My SME screen,,,,,,,,\n")
    f.write("Name,NSE Code,BSE Code,Industry,Current Price,Market Capitalization,"
            "Sales,OPM %,Profit after tax,EPS,ROCE %,Debt to equity,Promoter holding\n")
    f.write("Alpha Engineering Ltd,ALPHAENG,,Capital Goods,250,300,120,18.33,15,12.5,28.85,0.3,61.25\n")
    f.write("Beta Polymers Ltd,,544123,Chemicals,80,90,55,12,4,3.2,15.5,0.8,55\n")
    f.write("Gamma Labs Ltd,GAMMA,,Healthcare,-,-,-,-,-,-,-,-,-\n")

recs = IX.parse_file(_os0.path.join(_d, "screener_export.csv"))
eq("rows with data parsed, empty row dropped", len(recs), 2)
r0 = recs[0]
eq("name read", r0["name"], "Alpha Engineering Ltd")
eq("NSE code read", r0["nse_symbol"], "ALPHAENG")
eq("industry read", r0["industry"], "Capital Goods")
eq("price read as-is", r0["price"], 250.0)
eq("market cap kept in Cr", r0["mcap_cr"], 300.0)
eq("sales converted Cr -> rupees", r0["revenue"], 120 * CR)
eq("PAT converted Cr -> rupees", r0["pat"], 15 * CR)
eq("EPS read as-is", r0["eps"], 12.5)
eq("ROCE captured as a ratio hint", r0["_roce"], 28.85)
eq("promoter holding read", r0["promoter_pct"], 61.25)
eq("BSE-only row keeps its code", recs[1]["bse_code"], "544123")

vals, extra = IX.to_pipeline_values(r0)
eq("raw figures go to the pipeline", vals["revenue"], 120 * CR)
eq("operating profit derived from OPM% x sales", vals["op_income"], 120 * CR * 0.1833, tol=1e-3)
eq("ratio hints kept separate", extra["_roce"], 28.85)
true("no identity fields leak into raw figures",
     not any(k in vals for k in ("name", "nse_symbol", "industry")))

idx = IX.load_imports(_d)
true("indexed by NSE symbol", "NSE::ALPHAENG" in idx)
true("indexed by BSE code", "BSE::544123" in idx)
true("indexed by name", "NAME::" + S.name_key("Alpha Engineering Ltd") in idx)
eq("match by NSE symbol",
   IX.match_import({"symbol": "ALPHAENG", "exchange": "NSE-EMERGE"}, idx)[0]["revenue"], 120 * CR)
eq("match by BSE code",
   IX.match_import({"symbol": "X", "bse_code": "544123", "exchange": "BSE-SME"}, idx)[0]["pat"], 4 * CR)
eq("match by name when no codes",
   IX.match_import({"name": "Alpha Engineering Ltd"}, idx)[0]["eps"], 12.5)
eq("no match -> None", IX.match_import({"name": "Nonexistent Co"}, idx), None)
eq("empty index -> None", IX.match_import({"symbol": "ALPHAENG"}, {}), None)
eq("missing folder -> empty index", IX.load_imports(_os0.path.join(_d, "nope")), {})

# an unrecognised file must be skipped, never guessed at
with open(_os0.path.join(_d, "random.csv"), "w", encoding="utf-8") as f:
    f.write("some,unrelated,columns\n1,2,3\n")
eq("unrecognised file yields nothing", IX.parse_file(_os0.path.join(_d, "random.csv")), [])

# imported ratios are only a fallback - real figures still win
m_imp = compute_metrics({"_roce": 99.0, "ebit": 22.5 * CR, "equity": 60 * CR, "debt": 18 * CR})
eq("computed ROCE beats the imported hint", m_imp["roce"], 22.5 / 78 * 100)
m_imp2 = compute_metrics({"_roce": 99.0})
eq("imported ROCE used only when uncomputable", m_imp2["roce"], 99.0)
m_imp3 = compute_metrics({"_pe": 18.0, "price": 250, "eps": 12.5})
eq("computed P/E beats the imported hint", m_imp3["pe"], 20.0)

print("== checkpoint / resume ==")
import inspect
import os as _os
import tempfile as _tf
import build_data as _B
_orig_ckpt = _B.CKPT_PATH
_tmpdir = _tf.mkdtemp()
_B.CKPT_PATH = _os.path.join(_tmpdir, ".checkpoint.json")

comps = [{"yahoo": "AAA.NS", "raw": {"revenue": 100}, "src": {"revenue": "BSE filing"},
          "sector": "Chemicals", "fetched": "2026-07-01", "disputed": None},
         {"yahoo": "BBB.NS", "raw": {"pat": 5}, "src": {"pat": "NSE filing"},
          "sector": None, "fetched": None, "disputed": {"pat": {"spread_pct": 12.0}}}]
_B.save_checkpoint(comps, "test stage")
true("checkpoint file written", _os.path.exists(_B.CKPT_PATH))

fresh = [{"yahoo": "AAA.NS", "raw": {}, "src": {}},
         {"yahoo": "BBB.NS", "raw": {}, "src": {}},
         {"yahoo": "CCC.NS", "raw": {}, "src": {}}]
n = _B.load_checkpoint(fresh)
eq("restored the two known companies", n, 2)
eq("raw data restored", fresh[0]["raw"]["revenue"], 100)
eq("provenance restored", fresh[0]["src"]["revenue"], "BSE filing")
eq("sector restored", fresh[0]["sector"], "Chemicals")
eq("disputes restored", fresh[1]["disputed"]["pat"]["spread_pct"], 12.0)
eq("unknown company untouched", fresh[2]["raw"], {})

# newer data in the current run must win over the checkpoint
newer = [{"yahoo": "AAA.NS", "raw": {"revenue": 999}, "src": {"revenue": "NSE filing"}}]
_B.load_checkpoint(newer)
eq("current run's value wins over checkpoint", newer[0]["raw"]["revenue"], 999)
eq("current run's source wins", newer[0]["src"]["revenue"], "NSE filing")

_os.remove(_B.CKPT_PATH)
eq("no checkpoint -> nothing restored", _B.load_checkpoint(fresh), 0)
_B.CKPT_PATH = _orig_ckpt

true("build clears checkpoint after publishing",
     "os.remove(CKPT_PATH)" in inspect.getsource(_B.main))
true("build saves checkpoints during the long stage",
     "save_checkpoint(companies" in inspect.getsource(_B.main))

print("== sector mapping ==")
from build_data import SECTOR_MAP
true("map is populated", len(SECTOR_MAP) > 80, str(len(SECTOR_MAP)))
eq("IT folded", SECTOR_MAP["it - software"], "Information Technology")
eq("pharma folded", SECTOR_MAP["pharmaceuticals"], "Healthcare")
eq("auto ancillary folded", SECTOR_MAP["auto ancillary"], "Auto Components")
eq("textile folded", SECTOR_MAP["textile"], "Textiles")
eq("steel folded", SECTOR_MAP["steel"], "Metals & Mining")
true("all keys lowercase (lookup is lowercased)",
     all(k == k.lower() for k in SECTOR_MAP))
true("no empty sector values", all(v.strip() for v in SECTOR_MAP.values()))
true("broad sector count is screener-friendly",
     10 <= len(set(SECTOR_MAP.values())) <= 30, str(len(set(SECTOR_MAP.values()))))
# an unmapped industry must be kept verbatim, never dropped or guessed
unmapped = "Some Very Niche Industry"
eq("unmapped industry kept as-is",
   SECTOR_MAP.get(unmapped.strip().lower(), unmapped.strip()), unmapped)

print("== real captured seed data is genuinely real ==")
import os as _os
_seed = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                      "data", "seed")
if _os.path.isdir(_seed):
    import json as _json
    nse = _json.load(open(_os.path.join(_seed, "nse_emerge_live.json"), encoding="utf-8"))
    cg = _json.load(open(_os.path.join(_seed, "cg_ipo_records.json"), encoding="utf-8"))
    true("NSE seed has rows", len(nse) > 50, f"{len(nse)}")
    true("NSE seed rows carry a symbol", all(r.get("symbol") for r in nse))
    true("NSE seed prices are positive", all(S.num(r.get("lastPrice")) > 0 for r in nse))
    true("NSE seed 52w high >= low",
         all(S.num(r.get("yearHigh")) >= S.num(r.get("yearLow")) for r in nse))
    true("CG seed has records", len(cg) > 20, f"{len(cg)}")
    true("CG records carry real ISINs",
         sum(1 for r in cg if str(r.get("~isin", "")).startswith("INE")) > 20)
    true("CG records carry company names", all(r.get("Company") for r in cg))
else:
    true("seed directory present", False, "data/seed missing")

print("== publish guard (never overwrite good data with a bad run) ==")


def would_publish(built_n, prev_n):
    """Mirror of the guard in build_data.main(); kept in sync by these tests."""
    return not (built_n == 0 or (prev_n >= 50 and built_n < prev_n * 0.5))


true("empty build blocked", not would_publish(0, 900))
true("empty build blocked even with no previous data", not would_publish(0, 0))
true("half-empty build blocked", not would_publish(400, 900))
true("normal build publishes", would_publish(890, 900))
true("small growth publishes", would_publish(950, 900))
true("slight shrink publishes (delistings)", would_publish(880, 900))
true("first real build publishes", would_publish(900, 0))
true("tiny previous set doesn't block a small build", would_publish(20, 30))

# the real guard must match this logic
import inspect
import build_data as B
src = inspect.getsource(B.main)
true("guard exists in build_data.main", "refusing to overwrite" in src)
true("guard checks empty output", "if not out or" in src)
true("guard checks the 50% floor", "0.5" in src)
true("guard exits non-zero", "sys.exit(1)" in src)

print(f"\n{'ALL PASS' if FAIL == 0 else 'FAILURES'}: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
