# Drop your Screener.in / Trendlyne exports in this folder

Anything you put here gets merged into the screener on the next build, labelled
**"Screener/your export"** so you always know which numbers came from where.

## Why this way (and not scraping)

Screener and Trendlyne have no public API, and their terms don't allow scraping.
A scraper would also break every time they change their HTML, and could get your
subscribers' IPs blocked. Using **your own export from your own logged-in account**
is legitimate, stable, and gives you exactly the data you trust.

## How to get the file from Screener.in (2 minutes)

1. Log in at screener.in
2. Go to **Screens → Create new screen**
3. Paste a query that returns your SME universe, e.g.

   ```
   Market Capitalization < 2000 AND
   Current Price > 0
   ```

4. Add the columns you want on the screen page — the useful ones are:
   **Sales, OPM %, Profit after tax, EPS, ROCE %, ROE %, Debt to equity,
   CFO, Promoter holding, Pledged percentage, Market Capitalization,
   Current Price, NSE Code, BSE Code, Industry**
5. Click **Export to Excel**
6. Drop the downloaded `.xlsx` into this folder
7. Run `2-UPDATE-ALL-DATA.bat` (or the GitHub Action)

Screener caps a screen at a few hundred rows per page — export each page and
drop **all** the files here. Multiple files are fine; they're merged together.

## Trendlyne / Tickertape / your own spreadsheet

Any `.csv`, `.tsv` or `.xlsx` works. Column headers are matched loosely, so
"Profit after tax", "Net Profit" and "PAT" all land in the same field, and
"Market Capitalization (Rs. Cr.)" is understood. You don't need to rename
anything. Columns that aren't recognised are ignored — never guessed at.

To check a file is being read correctly before a long build:

```
python pipeline\import_external.py
```

It prints how many rows it found in each file.

## What takes priority

1. **XBRL filing** — the company's own tagged submission to NSE/BSE (what Screener parses)
2. **Your export** — this folder
3. BSE results API → 4. NSE results API → 5. Moneycontrol → 6. Yahoo

Each *field* is taken from the best source that has it. Your export fills the
gaps the exchanges leave, and where both exist and disagree by more than 5%,
the company gets flagged in the app with both figures shown.

Ratios in your export (ROCE, ROE, D/E, P/E) are used **only** where the pipeline
can't compute them from raw figures itself — a computed number always wins.
