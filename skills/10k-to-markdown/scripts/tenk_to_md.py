"""tenk_to_md.py - download one original Form 10-K from SEC EDGAR and convert it to Markdown.

Usage:
    python tenk_to_md.py TICKER FISCAL_YEAR [--out-root DIR] [--force]

Writes <out-root>/<TICKER>/FY<year>/{filing.<ext>, 10k.md, meta.json} and prints
a few lines of metadata. Never prints filing text. Stdlib only.

Fiscal year = dei:DocumentFiscalYearFocus from the filing's inline XBRL when
present, else the year of the period end date (reportDate).
"""
import argparse
import bisect
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

VERSION = "14"
UA = "10k-to-markdown youremail@gmail.com"
FORMS = {"10-K", "10-K405"}
DEFAULT_ROOT = Path.home() / "sec-filings" / "10k"
WRAP = 1500  # hard-wrap prose lines longer than this (Read truncates very long lines)

ITEM_ORDER = ["1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
              "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16"]
CORE = ["1", "2", "3", "5", "7", "8", "10", "11", "12", "13", "14"]
CORE_MODERN = CORE + ["1A", "7A", "9A", "15"]  # fiscal 2006+

# ---------------------------------------------------------------- network

_last = 0.0


def fetch(url):
    """GET with SEC-required User-Agent, gzip, ~6 req/s pacing, retry on 429/503."""
    global _last
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        wait = 0.17 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            _last = time.time()
            return data
        except urllib.error.HTTPError as e:
            _last = time.time()
            if e.code in (429, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def resolve_cik(ticker, root):
    cache = root / "_company_tickers.json"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
        root.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(fetch("https://www.sec.gov/files/company_tickers.json"))
    for row in json.loads(cache.read_bytes()).values():
        if row["ticker"].upper() == ticker:
            return str(row["cik_str"]).zfill(10), row["title"]
    return None, None


def _rows(block):
    for i, form in enumerate(block["form"]):
        if form in FORMS:
            yield {"form": form, "accession": block["accessionNumber"][i],
                   "doc": block["primaryDocument"][i], "filed": block["filingDate"][i],
                   "period": block["reportDate"][i]}


def period_fy(row):
    """Fiscal-year guess from the period end; filing year - 1 if period is missing."""
    return int(row["period"][:4]) if row["period"] else int(row["filed"][:4]) - 1


def candidates(cik, fy):
    """Original 10-Ks whose period-year is fy or fy+1, best guess first.

    fy+1 covers January/February fiscal year-ends that the company labels by
    the prior calendar year (e.g. period 2025-02-01 = fiscal 2024).
    """
    subs = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    rows = list(_rows(subs["filings"]["recent"]))
    for f in subs["filings"].get("files", []):
        # Older archive pages; skip only those that end before the fiscal year.
        if int(f["filingTo"][:4]) >= fy:
            rows += _rows(json.loads(fetch("https://data.sec.gov/submissions/" + f["name"])))
    rows = [r for r in rows if period_fy(r) in (fy, fy + 1)]
    # Exact period-year first; within that, latest filing first.
    rows.sort(key=lambda r: r["filed"], reverse=True)
    rows.sort(key=lambda r: period_fy(r) != fy)
    return rows


def doc_url(cik, row):
    acc = row["accession"].replace("-", "")
    name = row["doc"] or row["accession"] + ".txt"
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{name}"


def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


DEI_FY = re.compile(r"""name\s*=\s*["']dei:DocumentFiscalYearFocus["'][^>]*>\s*(?:<[^>]+>\s*)*(\d{4})""", re.I)

# ---------------------------------------------------------------- HTML -> Markdown

BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
         "hr", "blockquote", "pre", "center", "section", "article", "dt", "dd"}
VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr"}
SKIP = {"head", "script", "style", "ix:header", "title"}
HIDDEN = re.compile(r"display\s*:\s*none", re.I)
CURRENCY = {"$", "€", "£", "¥", "US$", "C$"}
OPENERS = CURRENCY | {"(", "$(", "($"}
CLOSERS = {")", "%", ")%", "%)"}
NUMSTART = re.compile(r"^[(\-–—]?\s*[\d.,]")
BR = "\x01"  # line break inside a table cell; becomes "\n" (layout tables) or " " (data tables)
SUPER = re.compile(r"vertical-align\s*:\s*super")
SUP_OPEN, SUP_CLOSE = "\x03", "\x04"
ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")


def fix_sup(text):
    """Superscript footnote refs become " [1]" so "100<sup>1</sup>" never reads as 1001; others (®, st) stay inline."""
    def repl(m):
        c = m.group(1).strip()
        if re.fullmatch(r"\(?\d{1,3}\)?|\([a-z]{1,2}\)|[*†‡]{1,3}", c, re.I):
            return " [" + c.strip("()") + "]"
        return c
    return re.sub(r"[\x03\x04]", "", re.sub(r"\x03([^\x03\x04]*)\x04", repl, text))


class Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []      # document text chunks
        self.tables = []   # stack of {"rows": [[(text, span)]], "row": list|None, "cell": list|None, "span": int}
        self.open = []     # stack of (tag, hidden, superscript) for non-void tags
        self.hidden = 0

    def _emit(self, s):
        t = self.tables[-1] if self.tables else None
        if t is not None and t["cell"] is not None:
            t["cell"].append(s)
        elif t is not None:
            pass  # stray text between cells
        else:
            self.out.append(s)

    def _close_cell(self, t):
        if t["cell"] is not None:
            if t["row"] is None:
                t["row"] = []
            text = re.sub(r"\s+", " ", fix_sup("".join(t["cell"])))
            text = re.sub(r" ?\x01[ \x01]*", lambda m: BR * min(2, m.group().count(BR)), text).strip(" " + BR)
            t["row"].append((text, t["span"]))
            t["cell"] = None

    def _close_row(self, t):
        self._close_cell(t)
        if t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            style = (dict(attrs).get("style") or "").lower()
            hide = tag in SKIP or bool(HIDDEN.search(style))
            sup = (tag == "sup" or bool(SUPER.search(style))) and not self.hidden and not hide
            self.open.append((tag, hide, sup))
            self.hidden += hide
            if sup:
                self._emit(SUP_OPEN)
        if self.hidden:
            return
        if not self.tables and (tag == "hr" or "page-break" in (dict(attrs).get("style") or "").lower()):
            self.out.append("\n" + PB + "\n")
        if tag == "table":
            self.tables.append({"rows": [], "row": None, "cell": None, "span": 1})
        elif self.tables and tag == "tr":
            self._close_row(self.tables[-1])
            self.tables[-1]["row"] = []
        elif self.tables and tag in ("td", "th"):
            t = self.tables[-1]
            self._close_cell(t)
            if t["row"] is None:
                t["row"] = []
            try:
                t["span"] = max(1, min(int(dict(attrs).get("colspan") or 1), 50))
            except ValueError:
                t["span"] = 1
            t["cell"] = []
        elif tag in BLOCK:
            self._emit(BR if self.tables else "\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        was_hidden = self.hidden
        if tag not in VOID and any(o[0] == tag for o in self.open):
            while self.open:
                t, h, sup = self.open.pop()
                self.hidden -= h
                if sup:
                    self._emit(SUP_CLOSE)
                if t == tag:
                    break
        if was_hidden:
            return
        if tag == "table" and self.tables:
            t = self.tables.pop()
            self._close_row(t)
            md = render_table(t["rows"])
            if self.tables:
                self._emit(BR * 2 + md.replace("\n", BR) + BR * 2)
            elif md:
                self.out.append("\n\n" + md + "\n\n")
        elif self.tables and tag in ("td", "th"):
            self._close_cell(self.tables[-1])
        elif self.tables and tag == "tr":
            self._close_row(self.tables[-1])
        elif tag in BLOCK:
            self._emit(BR if self.tables else "\n")

    def handle_data(self, data):
        if not self.hidden:
            self._emit(re.sub(r"[\s\xa0]+", " ", ZERO_WIDTH.sub("", data)))


def render_table(rows):
    """Render rows of (text, colspan) as a compact GFM table.

    Merges stray currency/paren/percent cells into the adjacent number (a lone
    "$", "(" or ")" that has no number to join is dropped), lays cells on a
    colspan grid, and drops grid columns that hold no text. One surviving
    column (layout table) -> plain lines; one row -> a single line.
    """
    rows = [[list(c) for c in r] for r in rows if any(c[0] for c in r)]
    if not rows:
        return ""
    for r in rows:
        for i, c in enumerate(r):
            if c[0] in OPENERS:
                nxt = next((x for x in r[i + 1:] if x[0]), None)
                # "(" "$" "100" -> "($100": chain only into the adjacent cell, never "$" into "$".
                if nxt and (NUMSTART.match(nxt[0]) or
                            nxt is r[i + 1] and nxt[0] in OPENERS and nxt[0] != c[0]):
                    nxt[0] = c[0] + nxt[0]
                c[0] = ""
            elif c[0] in CLOSERS:
                prv = next((x for x in reversed(r[:i]) if x[0]), None)
                if prv and re.search(r"[\d)\]]$", prv[0]):
                    prv[0] += c[0]
                    c[0] = ""
                elif c[0] == ")":
                    c[0] = ""
    placed, width = [], 0
    for r in rows:
        col, pr = 0, []
        for text, span in r:
            pr.append((col, span, text))
            col += span
        placed.append(pr)
        width = max(width, col)
    keep = [False] * width
    for pr in placed:
        for col, span, text in pr:
            if text and span == 1:
                keep[col] = True
    # Narrow spans first, so a value spanning 2 columns anchors before the
    # 3-column period header above it chooses its column.
    for col, span, text in sorted((c for pr in placed for c in pr if c[2] and c[1] > 1), key=lambda c: c[1]):
        if not any(keep[col:col + span]):
            keep[col] = True
    index = {c: i for i, c in enumerate(i for i, k in enumerate(keep) if k)}
    grid = []
    for pr in placed:
        cells = [""] * len(index)
        for col, span, text in pr:
            if not text:
                continue
            target = next(index[c] for c in range(col, col + span) if keep[c])
            cells[target] = (cells[target] + " " + text).strip()
        grid.append(cells)
    if len(index) == 1:
        return "\n".join(g[0] for g in grid).replace(BR, "\n")
    # Leading label-only rows (statement title, "(in millions)") become plain lines.
    lead = []
    while len(grid) > 2 and grid[0][0] and not any(grid[0][1:]):
        lead.append(grid.pop(0)[0].replace(BR, " "))
    if len(grid) == 1:
        return "\n".join(lead + [" ".join(c for c in grid[0] if c).replace(BR, "\n")])
    esc = [[c.replace("|", "\\|").replace(BR, " ") for c in g] for g in grid]
    lines = lead + ["|" + "|".join(esc[0]) + "|", "|" + "|".join("-" * len(index)) + "|"]
    lines += ["|" + "|".join(g) + "|" for g in esc[1:]]
    return "\n".join(lines)


# ---------------------------------------------------------------- cleanup + sections

NOISE = re.compile(r"^(?:\d{1,3}|[ivxlc]{1,6}|-\s*\d{1,3}\s*-|page \d{1,3}|table of contents|"
                   r"\(?back to (?:top|contents|table of contents)\)?|</?(?:page|table|caption|s|c|fn)>|"
                   r"[-=_*\s]{10,})$", re.I)
BULLET = {"•", "●", "·", "◦", "▪", "■", "○", "o", "-", "–", "—"}
ITEM = re.compile(r"^(?:part\s+[ivx]+\s*[,.:\-–—]?\s*)?item\s*(\d{1,2}[a-c]?)\b\s*[.:\-–—]?\s*(.*)$",
                  re.I)
# Standard opening words of each Item title, matched at the start of the title.
EXPECT = {"1": "business", "1A": "risk factors", "1B": "unresolved", "1C": "cybersecurity", "2": "propert",
          "3": "legal", "4": "mine|submission|reserved|removed", "5": "market", "6": "selected|reserved|removed",
          "7": "management", "7A": "quantitative", "8": "(?:consolidated )?financial statements",
          "9": "changes in", "9A": "controls", "9B": "other information", "9C": "disclosure regarding",
          "10": "directors", "11": "(?:executive )?compensation", "12": "security ownership",
          "13": "certain relationships", "14": "principal account", "15": "exhibit", "16": "form 10-k summary"}
TOC_ENTRY = re.compile(r"\.{3,}|\s\d{1,3}$")
KEEP_REPEATS = {"none", "not applicable", "n/a"}
UNITS = re.compile(r"^\(?\s*(?:\w+ )?(?:in )?(?:millions|thousands|billions)\b", re.I)
PB = "\x02"  # page-break marker line, emitted by Converter and consumed by clean_lines


def clean_lines(text, strip=True):
    """Normalize lines, drop page noise and repeated running headers, glue bullets, wrap long prose."""
    def norm(x):
        if not strip:
            return x.rstrip()
        return x.strip() if x.startswith("|") else re.sub(r"[ \t​]+", " ", x).strip()

    raw_lines = [norm(x) for x in text.split("\n")]
    # Running headers/footers: short lines repeated 5+ times within 3 lines of a
    # page break. Needs page-break markers, so never applies to .txt filings.
    nonblank = [i for i, x in enumerate(raw_lines) if x and x != PB]
    near = set()
    for m in (i for i, x in enumerate(raw_lines) if x == PB):
        k = bisect.bisect(nonblank, m)
        near.update(nonblank[max(0, k - 3):k + 3])
    counts = {}
    for i in near:
        x = raw_lines[i]
        if len(x) <= 80 and not x.startswith("|") and not x.endswith(".") and not ITEM.match(x) \
                and x.lower() not in KEEP_REPEATS and not UNITS.match(x):
            counts[x] = counts.get(x, 0) + 1
    seen, lines, blank, bullet = set(), [], True, ""
    for i, line in enumerate(raw_lines):
        key = line.strip()
        if i in near and counts.get(line, 0) >= 5:
            if line in seen:  # keep first occurrence only
                continue
            seen.add(line)
        if not key or key == PB or NOISE.match(key):
            if not blank and not bullet:
                lines.append("")
                blank = True
            continue
        if key in BULLET:
            bullet = "- "
            continue
        line, bullet = bullet + line, ""
        while strip and len(line) > WRAP and not line.startswith("|"):
            cut = line.rfind(" ", 0, WRAP)
            cut = cut if cut > 0 else WRAP
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
        blank = False
    return lines


def mark_items(lines):
    """Promote the body heading of each Item to '## Item X. Title'; return {item: line_idx}.

    A candidate is "titled" when its title (same line, or the next short line
    for "Item 1." / "Business" splits) contains the item's standard wording
    and carries no TOC dot leaders or page number. For each item prefer a
    titled candidate, then the one followed by the most text before the next
    candidate. An untitled bare "Item 7" seen 3+ times is a running page
    header and is deleted. "Item 1A of this report..." is a cross-reference.
    """
    cands = []
    for i, line in enumerate(lines):
        if len(line) > 200 or line.startswith("|"):
            continue
        m = ITEM.match(line.strip())
        if not m or m.group(1).upper() not in ITEM_ORDER or m.group(2)[:1].islower():
            continue
        key, title, nxt = m.group(1).upper(), m.group(2).strip(), None
        if not title:
            j = next((k for k in range(i + 1, min(i + 4, len(lines))) if lines[k]), None)
            if j is not None and len(lines[j]) < 120 and not lines[j].startswith("|") \
                    and not ITEM.match(lines[j].strip()):
                title, nxt = lines[j].strip(), j
        titled = bool(re.match(r"[\W_]*(?:" + EXPECT[key] + ")", title, re.I)) and not TOC_ENTRY.search(title)
        cands.append((i, key, title, nxt, titled, not m.group(2).strip()))
    bare = {}
    for c in cands:
        if c[5] and not c[4]:
            bare[c[1]] = bare.get(c[1], 0) + 1
    running = [c for c in cands if c[5] and not c[4] and bare[c[1]] >= 3]
    for c in running:
        lines[c[0]] = ""
    cands = [c for c in cands if c not in running]
    best = {}
    for n, (i, key, title, nxt, titled, _) in enumerate(cands):
        end = cands[n + 1][0] if n + 1 < len(cands) else len(lines)
        score = (titled, sum(len(x) for x in lines[i + 1:end]))
        if key not in best or score > best[key][0]:
            best[key] = (score, i, title, nxt)
    found = {}
    for key, (_, i, title, nxt) in best.items():
        if nxt is not None:
            lines[nxt] = ""
        title = re.sub(r"\s*\.{3,}.*$|\s+\d{1,3}$", "", title).strip(" .")
        lines[i] = f"## Item {key}. {title}".rstrip()
        found[key] = i
    return found


def html_to_lines(html):
    c = Converter()
    c.feed(html)
    c.close()
    return clean_lines(fix_sup("".join(c.out)))


def text_to_lines(txt):
    """Pre-2001 plain-text submissions: first <TEXT> block, layout kept."""
    m = re.search(r"<TEXT>(.*?)</TEXT>", txt, re.S | re.I)
    body = m.group(1) if m else txt
    if re.search(r"<html", body, re.I):
        return html_to_lines(body)
    return clean_lines(body, strip=False)


def convert(raw_text, is_html, header):
    lines = html_to_lines(raw_text) if is_html else text_to_lines(raw_text)
    lines = header + [""] + lines
    found = mark_items(lines)
    # Collapse blanks left behind by merged titles, keeping line numbers consistent.
    final, blank, pos = [], False, {}
    marks = {i: k for k, i in found.items()}
    for i, line in enumerate(lines):
        if not line:
            if not blank:
                final.append("")
            blank = True
            continue
        if i in marks:
            pos[marks[i]] = len(final) + 1  # 1-based
        final.append(line)
        blank = False
    return final, pos


LANDMARKS = [  # (name, heading regex, what must follow within a few lines)
    ("mdna", r"management[’'`]?s discussion and analysis", "prose"),
    ("auditor_report", r"reports? of independent (?:registered public accounting firm|auditors|accountants)", "prose"),
    ("income_statement", r"(?:consolidated )?(?:statements? of (?:operations|income|earnings)|income statements?)"
                         r"|consolidated results of operations", "table"),
    ("balance_sheet", r"(?:consolidated )?(?:balance sheets?|statements? of financial (?:position|condition))", "table"),
    ("cash_flows", r"(?:consolidated )?(?:statements? of cash flows?|cash flows? statements?)|consolidated cash flows",
     "table"),
    ("notes", r"notes to (?:the )?(?:consolidated )?financial statements", "prose"),
]


def landmarks(lines, start):
    """1-based line of the first real heading for each landmark at or after `start`.

    Financial statements can sit after Part IV (F-pages), outside Item 8, so
    these let callers jump straight to them. Prose landmarks take the first
    heading followed by prose. Statement titles must fill the whole line and
    be followed by a table; MD&A reuses those titles, so take the last match
    before the notes begin.
    """
    out, tables = {}, []
    for name, pat, follow in LANDMARKS:
        if follow == "prose":
            rx = re.compile(r"(?:## item \w+\. )?" + pat + r"\b", re.I)
        else:
            rx = re.compile(pat + r"(?:\s*\([^)]*\))?:?$", re.I)
        hits = []
        for i in range(start - 1, len(lines)):
            if len(lines[i]) > 150 or lines[i].startswith("|") or not rx.match(lines[i].strip()):
                continue
            nxt = [x for x in lines[i + 1:i + 15] if x][:8]
            if follow == "table" and any(x.startswith("|") for x in nxt) or \
                    follow == "prose" and sum(len(x) for x in nxt[:6] if not x.startswith("|")) > 300:
                hits.append(i + 1)
                if follow == "prose":
                    break
        if hits and follow == "prose":
            out[name] = hits[0]
        elif hits:
            tables.append((name, hits))
    notes = out.get("notes")
    for name, hits in tables:
        before = [h for h in hits if notes and h < notes]
        if not before:
            out[name] = hits[0]
            continue
        # Walk back over continuation pages ("(Continued)" or a repeated title)
        # so the landmark is the statement's first page.
        k = len(before) - 1
        while k > 0 and before[k] - before[k - 1] <= 150:
            k -= 1
        out[name] = before[k]
    return {n: out[n] for n, _, _ in LANDMARKS if n in out}


def sections(lines, pos):
    order = sorted(pos.items(), key=lambda kv: kv[1])
    out = {}
    if order:
        out["_front"] = {"title": "Cover and table of contents", "start": 1, "end": order[0][1] - 1}
    for n, (key, start) in enumerate(order):
        end = order[n + 1][1] - 1 if n + 1 < len(order) else len(lines)
        title = lines[start - 1].split(". ", 1)[1] if ". " in lines[start - 1] else ""
        out[key] = {"title": title, "start": start, "end": end}
    for v in out.values():
        v["chars"] = sum(len(x) + 1 for x in lines[v["start"] - 1:v["end"]])
    return out


# ---------------------------------------------------------------- main

def summary(meta, d):
    secs = [k for k in meta["sections"] if k != "_front"]
    core = CORE_MODERN if meta["fiscal_year"] >= 2006 else CORE
    missing = [k for k in core if k not in meta["sections"]]
    print(f"OK {meta['ticker']} FY{meta['fiscal_year']} {meta['form']} filed {meta['filed']} "
          f"period {meta['period']} fy_source={meta['fy_source']}{' (cached)' if meta.get('_cached') else ''}")
    print(f"md: {d / '10k.md'} ({meta['lines']} lines, {meta['chars']} chars, ~{meta['chars'] // 4000}k tokens)")
    print(f"meta: {d / 'meta.json'}")
    print("items: " + " ".join(secs) + (f" | MISSING core: {' '.join(missing)}" if missing else ""))
    lm_missing = [n for n, _, _ in LANDMARKS if n not in meta["landmarks"]]
    print(f"landmarks: {len(meta['landmarks'])}/{len(LANDMARKS)}"
          + (f" | missing: {' '.join(lm_missing)}" if lm_missing else ""))
    if meta["fy_source"] == "period" and meta["period"][5:7] in ("01", "02"):
        print(f"NOTE: Jan/Feb year-end without XBRL fiscal-year tag; the company may call this fiscal {meta['fiscal_year'] - 1}.")


def write_outputs(d, meta, raw, ext):
    """Convert the saved filing and write 10k.md + meta.json."""
    text = decode(raw)
    header = [f"# {meta['company']} Form {meta['form']}, fiscal year {meta['fiscal_year']}",
              f"Period {meta['period']} | filed {meta['filed']} | accession {meta['accession']} | {meta['url']}"]
    lines, pos = convert(text, ext != "txt", header)
    body = "\n".join(lines) + "\n"
    (d / "10k.md").write_text(body, encoding="utf-8")
    meta.update(converter_version=VERSION, sha256=hashlib.sha256(raw).hexdigest(),
                lines=len(lines), chars=len(body), sections=sections(lines, pos),
                landmarks=landmarks(lines, min(pos.values(), default=1)))
    if "mdna" not in meta["landmarks"] and "7" in pos:  # Item 7 opens with an index table, not prose
        meta["landmarks"] = {"mdna": pos["7"], **meta["landmarks"]}
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Download an original Form 10-K and convert it to Markdown.")
    ap.add_argument("ticker")
    ap.add_argument("fiscal_year", type=int)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--force", action="store_true", help="re-download and re-convert")
    a = ap.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,10}", a.ticker):
        sys.exit(f"ERROR: invalid ticker {a.ticker!r}")
    ticker = a.ticker.upper().replace(".", "-")  # SEC uses BRK-B
    fy = a.fiscal_year
    if not 1993 <= fy <= time.gmtime().tm_year:
        sys.exit(f"ERROR: fiscal year {fy} out of range (1993-{time.gmtime().tm_year})")
    d = a.out_root / ticker / f"FY{fy}"
    meta_path = d / "meta.json"

    if meta_path.exists() and not a.force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw_path, md_path = d / meta.get("file", "-"), d / "10k.md"
        raw = raw_path.read_bytes() if raw_path.is_file() else b""
        dei = DEI_FY.search(decode(raw)) if raw else None
        # Else fall through and select again: raw missing/changed, or its DEI year disagrees.
        if raw and hashlib.sha256(raw).hexdigest() == meta.get("sha256") and (not dei or int(dei.group(1)) == fy):
            if meta.get("converter_version") != VERSION or not md_path.is_file() \
                    or len(md_path.read_text(encoding="utf-8")) != meta.get("chars"):
                write_outputs(d, meta, raw, meta["file"].rsplit(".", 1)[-1].lower())  # offline re-convert
            meta["_cached"] = True
            summary(meta, d)
            return

    try:
        cik, company = resolve_cik(ticker, a.out_root)
        if cik is None:
            sys.exit(f"ERROR: ticker {ticker} not in SEC company_tickers.json")
        cands = candidates(cik, fy)
        rejected = []
        for row in cands[:3]:
            url = doc_url(cik, row)
            raw = fetch(url)
            m = DEI_FY.search(decode(raw))
            if m:
                fy_source, doc_fy = "dei", int(m.group(1))
            else:
                fy_source, doc_fy = "period", period_fy(row)
            if doc_fy == fy:
                break
            rejected.append(f"{row['accession']} period {row['period']} = FY{doc_fy} ({fy_source})")
        else:
            near = ", ".join(rejected) or "no 10-K with period year in range"
            sys.exit(f"ERROR: no original 10-K for {ticker} FY{fy}. Checked: {near}. "
                     f"Foreign filers file 20-F/40-F.")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: SEC request failed: {e}")

    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("htm", "html", "txt"):
        sys.exit(f"ERROR: unsupported primary document type .{ext}: {url}")
    d.mkdir(parents=True, exist_ok=True)
    fname = f"filing.{ext}"
    (d / fname).write_bytes(raw)
    meta = {"ticker": ticker, "company": company, "cik": cik, "form": row["form"],
            "fiscal_year": fy, "fy_source": fy_source, "period": row["period"], "filed": row["filed"],
            "accession": row["accession"], "url": url, "file": fname}
    write_outputs(d, meta, raw, ext)
    summary(meta, d)


if __name__ == "__main__":
    main()
