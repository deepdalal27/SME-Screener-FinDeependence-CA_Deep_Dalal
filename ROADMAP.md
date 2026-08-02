# What to add next — prioritised

Written for the FinDeependence SME Screener, from the perspective of what an SME
investor actually needs and what your subscribers would pay to renew for.
Ordered by **value per hour of work**, not by how impressive it sounds.

---

## Tier 1 — do these first (high value, low effort)

### 1. Promoter pledge & shareholding trend
Pledged promoter shares are the single loudest red flag in SME land, and a falling
promoter stake is usually the second. Both come free in the exchange's quarterly
shareholding-pattern filing.
**Add:** `pledged_pct`, `promoter_change_qoq`, and a "Promoter reduced stake" screen.
**Why it matters more here than in large caps:** SME promoters typically hold 60–75%,
so even a 2% drop is a real signal.

### 2. Auditor & compliance red flags
Parse the exchange announcement feed for: auditor resignation, qualified opinion,
delayed results, ASM/GSM surveillance stage, trading suspension.
**Add:** a red-flag badge on the row and a "clean only" toggle.
**This is the highest-value item on the list** — in SME investing, avoiding the
disasters matters more than finding the winners.

### 3. Liquidity filter
Half of SME listings barely trade, so a great-looking screen result may be
un-buyable in size. You already capture traded volume.
**Add:** 20-day average traded value (₹ lakh/day), a "min liquidity" filter, and
"days to exit ₹X lakh at 10% of daily volume".

### 4. Watchlist + change alerts
Star companies, then get a note on what changed since you last looked — new results
filed, price move beyond a threshold, pledge change, fresh red flag.
Works with what you already have (localStorage + the daily rebuild).
**Turns the app from a lookup tool into something they open every morning** —
which is what makes a subscription renew.

---

## Tier 2 — strong differentiators (medium effort)

### 5. Peer comparison view
Pick a company, see the 8 closest peers by sector and size, side by side on every
metric, with the company's percentile rank. One screen answers "is this cheap, or
is the whole sector cheap?"

### 6. Quarterly / half-yearly trend charts
You store one period per company today. Store the last 8 and you can show revenue,
margin and PAT trend sparklines — the difference between "PAT is ₹15 Cr" and
"PAT has compounded for six straight halves".

### 7. Migration-to-mainboard tracker
SME companies moving to the main board is a well-known re-rating event with published
eligibility criteria (listed 2+ years, paid-up capital, profitability, market cap).
**Add:** a computed "migration eligibility" score. This is genuinely differentiated —
no free tool does it, and it's exactly what your audience cares about.

### 8. Anchor investor & lock-in expiry calendar
For recent listings: when does the pre-IPO / anchor lock-in expire? Supply hits and
prices often move. You already have listing dates, so the dates are computable.

### 9. Your IPO Tracker, joined up
The tracker knows what's listing; the screener knows what happened to everything
that listed. Link them: "how have this lead manager's last 20 SME IPOs performed?"
**Lead-manager track record is a real, defensible edge** — and you have the data
in both apps already.

---

## Tier 3 — nice to have

10. **Saved screen alerts by email** — "3 new companies entered your Quality screen this week"
11. **Excel export of a full company sheet**, not just the visible table
12. **Dark mode** — you'll get asked for it
13. **Notes per company** — your own thesis, stored on device
14. **Sector heatmap** — median OPM/ROCE/valuation by sector, to spot where value sits
15. **Comparison basket** — tick 5 companies, get a side-by-side table

---

## Two things I'd deliberately NOT add

**Buy/sell calls or a composite "score".** The moment the app tells people what to
buy, you carry SEBI research-analyst obligations and the liability that comes with
them. Keep it a screening tool that shows the data and let the user conclude. Your
verdict-score in the IPO tracker is framed as indicative — keep that discipline here.

**Intraday tick data or charting.** Free sources for SME intraday are unreliable and
your users already have a broker terminal for it. It would be a lot of work to be
worse than what they have.

---

## On data completeness, honestly

The single biggest improvement to this app isn't a feature — it's coverage. Once the
full build has run and you can see, per company, what's ✓ exchange-filed vs ≈
third-party vs blank, the gaps become obvious and specific. Send me that picture and
I can target the sources that fix the largest number of blanks, rather than guessing.
