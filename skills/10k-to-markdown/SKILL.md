---
name: 10k-to-markdown
surface: cli
description: Downloads one original Form 10-K from SEC EDGAR for a ticker and fiscal year and converts it to a Markdown file on disk, plus a meta.json index of line ranges for each Item (1, 1A, 7, 8, ...) and landmarks (MD&A, auditor report, income statement, balance sheet, cash flows, notes). A baseline primitive other skills build on. Use when asked to download, fetch, or convert a 10-K (annual report) to markdown or text for a ticker and year, or when another skill needs a 10-K's text on disk. Never reads the whole filing into context.
---

# 10k-to-markdown

Run (Bash; one call, prints at most 6 lines). Use `python3` on macOS and
Linux, `python` on Windows:

    python3 -I ~/.claude/skills/10k-to-markdown/scripts/tenk_to_md.py TICKER FISCAL_YEAR [--out-root DIR] [--force]

- `TICKER` as given (`BRK.B` and `brk-b` both work). `FISCAL_YEAR` is the
  company's fiscal year: `dei:DocumentFiscalYearFocus` from the filing's
  inline XBRL (2019+), else the year of the period end. Originals only
  (`10-K`, `10-K405`); amendments, `10-KT`, 20-F and 40-F are out of scope.
- Default output: `~/sec-filings/10k/<TICKER>/FY<year>/` (ticker in SEC's
  hyphen form, e.g. `BRK-B`). A re-run uses the cache with no network;
  `--force` downloads again.
- Exit code nonzero plus one `ERROR:` line on failure. Report it; don't retry
  with other tools.

## Output contract

| File | Content |
|---|---|
| `10k.md` | Full filing as Markdown. Line 1 is the title, line 2 holds period, filing date, accession, and URL. Each detected Item heading is `## Item <N>. <Title>`. Tables are compact GFM (`\|a\|b\|`). |
| `meta.json` | `ticker, company, cik, form, fiscal_year, fy_source (dei\|period), period, filed, accession, url, lines, chars, sections, landmarks` |
| `filing.htm` / `filing.txt` | Original primary document, kept so re-conversion needs no network. |

- `sections`: `{"_front"|"1"|"1A"|...: {title, start, end, chars}}`. Lines are
  1-based and inclusive, contiguous, and run to EOF. The last Item also holds
  signatures and anything after it.
- `landmarks`: `{mdna, auditor_report, income_statement, balance_sheet,
  cash_flows, notes: line}`, only for those found. Use these for financials.
  Many filers put the statements on F-pages after Part IV, so `sections["8"]`
  may be only a cross-reference. A landmark has no end line: read from it to
  the next larger landmark line (statement order varies by filer).

## Reading the output (context discipline)

A 10-K runs 50k-300k tokens. **Never Read `10k.md` whole.**

1. Read `meta.json` (small).
2. Budget each read by `chars`, not by lines (about 4 chars per token). If the
   span's `chars` is 40,000 or less, read it whole:
   `Read(file, offset=start, limit=end-start+1)`. If it is larger, Grep inside
   the span first, or read windows of about 150 lines and stop once you have
   what you need.
3. To find a topic, Grep `10k.md` with `-n` and a narrow pattern, then read
   around the hit.

## Known limits (reported, not fixed)

- `MISSING core:` on stdout means a standard Item wasn't detected (for
  example, Berkshire's Part III is a page-reference table). Fall back to Grep.
- Pre-~2005 filings often put MD&A and financials in Exhibit 13, which isn't
  downloaded, so landmarks show as missing.
- A `NOTE:` line flags January/February year-ends without the XBRL tag. Those
  companies may label the year one lower; `period` is authoritative.
- Old `.txt` filings keep their fixed-width layout; their tables are not
  converted to GFM.
