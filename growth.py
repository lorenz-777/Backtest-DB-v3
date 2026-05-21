#!/usr/bin/env python3
"""
growth.py – Wachstums-Berechnung v2
=====================================
Berechnet Year-over-Year (YoY) Wachstumsraten für EPS und Revenue.

Quellen-Hierarchie (pro Ticker / period_type):
  1. Macrotrends  (Primary – EPS + Revenue direkt)
  2. StockAnalysis (Fallback Income)
  3. MarketBeat   (Fallback Income)
  4. Lokale DB (fundamentals-Tabelle) – letzter Ausweg

Felder:
    earningsGrowth   = (eps_current  - eps_prior_year)  / |eps_prior_year|
    revenueGrowth    = (rev_current  - rev_prior_year)  / |rev_prior_year|

Methode (quarterly):  Q1 2025 vs Q1 2024 (gleicher Kalenderquartal, ±46 Tage)
Methode (annual):     FY 2024 vs FY 2023 (Vorjahr, gleiche YYYY)

Limit: 8 Quartale / 4 Jahreswerte

Verwendung:
    python growth.py              # alle Ticker aus tickers.txt
    python growth.py AAPL
    python growth.py --tickers-file tickers.txt
    python growth.py --debug
"""

import sys
import re
import json
import time
import calendar
import argparse
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from db import DB

console = Console()
TICKERS_FILE = "tickers.txt"

MAX_QUARTERLY = 8
MAX_ANNUAL    = 4


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

def get_html(url: str, debug: bool = False) -> tuple[int, str]:
    """Cloudflare-resistenter HTTP-Client: curl_cffi → cloudscraper → requests."""
    try:
        from curl_cffi import requests as cf
        if debug:
            console.print("[dim]  HTTP: curl_cffi[/dim]")
        session = cf.Session(impersonate="chrome124")
        hdrs = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
        session.get("https://www.marketbeat.com/", headers=hdrs, timeout=12)
        time.sleep(0.8)
        r = session.get(url, headers={**hdrs, "Referer": "https://www.marketbeat.com/"}, timeout=18)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} chars[/dim]")
        if r.status_code == 200 and len(r.text) > 3000:
            return r.status_code, r.text
    except ImportError:
        pass
    except Exception as e:
        if debug:
            console.print(f"[dim]  curl_cffi: {e}[/dim]")

    time.sleep(1.0)

    try:
        import cloudscraper
        if debug:
            console.print("[dim]  HTTP: cloudscraper[/dim]")
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=4,
        )
        scraper.get("https://www.marketbeat.com/", timeout=12)
        time.sleep(1.0)
        r = scraper.get(url, timeout=20)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} chars[/dim]")
        if r.status_code == 200 and len(r.text) > 3000:
            return r.status_code, r.text
    except ImportError:
        pass
    except Exception as e:
        if debug:
            console.print(f"[dim]  cloudscraper: {e}[/dim]")

    time.sleep(0.8)

    try:
        import requests
        if debug:
            console.print("[dim]  HTTP: requests[/dim]")
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        }
        r = requests.Session().get(url, headers=hdrs, timeout=18, allow_redirects=True)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} chars[/dim]")
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


def get_html_sa(url: str, debug: bool = False) -> tuple[int, str]:
    """HTTP-Client für StockAnalysis.com."""
    try:
        import requests
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://stockanalysis.com/",
        }
        r = requests.Session().get(url, headers=hdrs, timeout=18, allow_redirects=True)
        if debug:
            console.print(f"[dim]  SA → {r.status_code}, {len(r.text)} chars[/dim]")
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def to_float(v: str) -> float | None:
    if not v or v in ("–", "-", "N/A", ""):
        return None
    v = str(v).replace("$", "").replace(",", "").replace("+", "").replace("%", "").strip()
    if v.startswith("(") and v.endswith(")"):
        v = "-" + v[1:-1]
    mult = 1.0
    if   v.upper().endswith("T"): mult, v = 1e12, v[:-1]
    elif v.upper().endswith("B"): mult, v = 1e9,  v[:-1]
    elif v.upper().endswith("M"): mult, v = 1e6,  v[:-1]
    elif v.upper().endswith("K"): mult, v = 1e3,  v[:-1]
    try:
        return float(v) * mult
    except ValueError:
        return None


MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10,"nov": 11, "dec": 12,
}


def parse_mb_period(raw: str) -> str:
    raw = raw.strip().replace(".", "")
    m = re.match(r"Q(\d)\s+(20\d{2})", raw, re.I)
    if m:
        q, yr = int(m.group(1)), int(m.group(2))
        month_last = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
        return f"{yr}-{month_last[0]:02d}-{month_last[1]:02d}"
    m = re.match(r"([A-Za-z]{3})\s+(20\d{2})", raw)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower()[:3])
        yr  = int(m.group(2))
        if mon:
            last = calendar.monthrange(yr, mon)[1]
            return f"{yr}-{mon:02d}-{last:02d}"
    m = re.match(r"(\d{1,2})/(20\d{2})", raw)
    if m:
        mon, yr = int(m.group(1)), int(m.group(2))
        last = calendar.monthrange(yr, mon)[1]
        return f"{yr}-{mon:02d}-{last:02d}"
    m = re.match(r"^(20\d{2})$", raw)
    if m:
        return f"{m.group(1)}-12-31"
    return raw


def normalize_period(raw: str) -> str:
    raw = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    return parse_mb_period(raw)


def _extract_year(period: str) -> str | None:
    m = re.search(r"(20\d{2})", period)
    return m.group(1) if m else None


# ═══════════════════════════════════════════════════════════════════════════════
# QUELLE 1: MACROTRENDS (PRIMARY)
# ═══════════════════════════════════════════════════════════════════════════════

MT_INCOME_METRICS = {
    "trailing_eps":  "eps-earnings-per-share-diluted",
    "total_revenue": "revenue",
}


def find_mt_slug(ticker: str, debug: bool = False) -> str:
    try:
        import requests
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}
        r    = requests.get(
            f"https://www.macrotrends.net/assets/php/typeahead.php?term={ticker}",
            headers=hdrs, timeout=10,
        )
        data = r.json()
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, list) and len(first) >= 3:
                return first[2].strip().lower()
            if isinstance(first, dict):
                return (first.get("slug") or first.get("n", "")).lower().replace(" ", "-")
    except Exception as e:
        if debug:
            console.print(f"[dim]  MT typeahead: {e}[/dim]")

    try:
        import requests
        r = requests.get(
            f"https://www.macrotrends.net/stocks/charts/{ticker}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10, allow_redirects=True,
        )
        m = re.search(rf"/stocks/charts/{ticker}/([^/]+)/", r.url, re.I)
        if m:
            return m.group(1).lower()
    except Exception as e:
        if debug:
            console.print(f"[dim]  MT redirect: {e}[/dim]")

    return ticker.lower()


def _extract_mt_json(html: str) -> list[dict]:
    for var_name in ["originalData", "chartData", "rawData"]:
        m = re.search(rf'var\s+{var_name}\s*=\s*(\[.*?\])\s*;', html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return []


def fetch_mt_metric(ticker: str, slug: str, metric_slug: str, freq: str,
                    debug: bool = False) -> dict[str, float | None]:
    url = f"https://www.macrotrends.net/stocks/charts/{ticker}/{slug}/{metric_slug}?freq={freq}"
    if debug:
        console.print(f"[dim]  MT: {url}[/dim]")
    status, html = get_html(url, debug=debug)
    if status != 200 or len(html) < 3000:
        return {}
    rows   = _extract_mt_json(html)
    result = {}
    for row in rows:
        date = row.get("date") or row.get("Date")
        val  = row.get("value") or row.get("val")
        if date and val is not None:
            try:
                result[str(date)] = float(str(val).replace(",", ""))
            except ValueError:
                pass
    if debug:
        console.print(f"[dim]  MT datapoints: {len(result)}[/dim]")
    return result


def scrape_macrotrends(ticker: str, period_type: str, debug: bool = False) -> dict[str, dict]:
    """Scrapt EPS + Revenue von Macrotrends. Gibt {period: {trailing_eps, total_revenue}} zurück."""
    freq   = "Q" if period_type == "quarterly" else "A"
    slug   = find_mt_slug(ticker, debug=debug)
    merged: dict[str, dict] = {}

    for metric_key, metric_slug in MT_INCOME_METRICS.items():
        time.sleep(0.8)
        data = fetch_mt_metric(ticker, slug, metric_slug, freq, debug=debug)
        for date, val in data.items():
            merged.setdefault(date, {})[metric_key] = val

    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# QUELLE 2: STOCKANALYSIS (FALLBACK)
# ═══════════════════════════════════════════════════════════════════════════════

SA_INCOME_FIELDS = {
    "revenue":   "total_revenue",
    "netIncome": None,
    "eps":       "trailing_eps",
    "epsDiluted":"trailing_eps",
}
SA_INCOME_LABELS = {
    "revenue":       "total_revenue",
    "total revenue": "total_revenue",
    "eps (diluted)": "trailing_eps",
    "diluted eps":   "trailing_eps",
    "eps":           "trailing_eps",
}


def _parse_sa_income_table(html: str, debug: bool = False) -> dict[str, dict]:
    from bs4 import BeautifulSoup
    soup   = BeautifulSoup(html, "html.parser")
    result: dict[str, dict] = {}

    table = soup.find("table")
    if not table:
        return result

    thead     = table.find("thead")
    first_row = (thead.find("tr") if thead else None) or table.find("tr")
    if not first_row:
        return result

    header_cells = first_row.find_all(["th", "td"])
    periods: list[str | None] = []
    for cell in header_cells:
        txt = cell.get_text(" ", strip=True).strip()
        if not txt or txt.lower() in ("", "fiscal year", "period ending", "quarter ending"):
            periods.append(None)
            continue
        if txt.upper() == "TTM":
            periods.append(None)
            continue
        parsed = normalize_period(txt)
        periods.append(parsed if re.search(r"20\d{2}", parsed) else None)

    if debug:
        console.print(f"[dim]  SA periods: {periods}[/dim]")

    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        metric_key = None
        first_cell = cells[0]
        data_field = first_cell.get("data-field", "").strip()
        if data_field and data_field in SA_INCOME_FIELDS:
            metric_key = SA_INCOME_FIELDS[data_field]
        if metric_key is None:
            label = first_cell.get_text(" ", strip=True).lower().strip()
            metric_key = SA_INCOME_LABELS.get(label)
            if metric_key is None:
                for lbl, key in SA_INCOME_LABELS.items():
                    if lbl in label:
                        metric_key = key
                        break

        if metric_key is None:
            continue

        for i, cell in enumerate(cells[1:], start=1):
            if i >= len(periods) or not periods[i]:
                continue
            val = to_float(cell.get_text(" ", strip=True))
            result.setdefault(periods[i], {})
            if result[periods[i]].get(metric_key) is None and val is not None:
                result[periods[i]][metric_key] = val

    return result


def scrape_stockanalysis(ticker: str, period_type: str, debug: bool = False) -> dict[str, dict]:
    p   = "quarterly" if period_type == "quarterly" else "annual"
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/?p={p}"
    if debug:
        console.print(f"[dim]  SA income: {url}[/dim]")
    status, html = get_html_sa(url, debug=debug)
    if status == 200 and len(html) > 3000:
        return _parse_sa_income_table(html, debug=debug)
    if debug:
        console.print(f"[dim]  SA HTTP {status}[/dim]")
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# QUELLE 3: MARKETBEAT (FALLBACK)
# ═══════════════════════════════════════════════════════════════════════════════

MB_INCOME_ALIASES = {
    "total_revenue": [
        "total revenue", "net revenue", "revenues", "revenue",
        "net sales", "total net revenue", "sales",
    ],
    "trailing_eps": [
        "eps (diluted)", "diluted eps", "earnings per share diluted",
        "earnings per share", "basic eps", "eps",
    ],
}


def find_exchange_mb(ticker: str, debug: bool = False) -> tuple[str, str]:
    for ex in ["NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEMKT"]:
        url    = f"https://www.marketbeat.com/stocks/{ex}/{ticker}/financials/"
        status, html = get_html(url, debug=debug)
        if status == 200 and len(html) > 5000:
            return ex, url
        time.sleep(0.4)
    return "", ""


def _parse_mb_income_table(html: str, debug: bool = False) -> dict[str, dict]:
    from bs4 import BeautifulSoup
    soup   = BeautifulSoup(html, "html.parser")
    result: dict[str, dict] = {}

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_cells = header_row.find_all(["th", "td"])
        periods: list[str | None] = []
        for cell in header_cells:
            txt    = cell.get_text(" ", strip=True).strip()
            parsed = parse_mb_period(txt)
            periods.append(parsed if re.search(r"20\d{2}", parsed) else None)

        if not any(periods):
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(" ", strip=True).lower().strip()

            metric_key = None
            for key, aliases in MB_INCOME_ALIASES.items():
                for alias in aliases:
                    if alias in label:
                        metric_key = key
                        break
                if metric_key:
                    break

            if not metric_key:
                continue

            for i, cell in enumerate(cells[1:], start=1):
                if i >= len(periods) or not periods[i]:
                    continue
                val = to_float(cell.get_text(" ", strip=True))
                result.setdefault(periods[i], {})
                if result[periods[i]].get(metric_key) is None and val is not None:
                    result[periods[i]][metric_key] = val

    return result


def scrape_marketbeat(ticker: str, exchange: str, period_type: str,
                      debug: bool = False) -> dict[str, dict]:
    suffix = "?type=quarterly" if period_type == "quarterly" else ""
    url    = f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/financials/{suffix}"
    if debug:
        console.print(f"[dim]  MB: {url}[/dim]")
    status, html = get_html(url, debug=debug)
    if status == 200 and len(html) > 5000:
        return _parse_mb_income_table(html, debug=debug)
    if debug:
        console.print(f"[dim]  MB HTTP {status}[/dim]")
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# QUELLE 4: DB FUNDAMENTALS (LETZTER AUSWEG)
# ═══════════════════════════════════════════════════════════════════════════════

def load_from_db(ticker: str, period_type: str, db: DB) -> dict[str, dict]:
    """Lädt EPS + Revenue aus der lokalen fundamentals-Tabelle."""
    rows = db.get_fundamentals(ticker, period_type=period_type, limit=MAX_QUARTERLY * 3)
    result: dict[str, dict] = {}
    for row in rows:
        row = dict(row)
        period = row["period_end"]
        result[period] = {
            "trailing_eps":  row.get("trailing_eps"),
            "total_revenue": row.get("total_revenue"),
        }
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MERGE HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _merge(base: dict[str, dict], supplement: dict[str, dict],
           annual: bool = False) -> dict[str, dict]:
    """Ergänzt base mit Werten aus supplement (nie überschreiben)."""
    def _best_key_q(period: str, cands: list[str]) -> str | None:
        ym = period[:7]
        for c in cands:
            if c[:7] == ym:
                return c
        try:
            target = datetime.strptime(ym, "%Y-%m")
        except ValueError:
            return None
        best, best_diff = None, float("inf")
        for c in cands:
            try:
                d    = datetime.strptime(c[:7], "%Y-%m")
                diff = abs((d - target).days)
                if diff < best_diff and diff <= 46:
                    best_diff, best = diff, c
            except ValueError:
                pass
        return best

    def _best_key_a(period: str, cands: list[str]) -> str | None:
        yr = _extract_year(period)
        if not yr:
            return None
        for c in cands:
            if _extract_year(c) == yr:
                return c
        return None

    _best = _best_key_a if annual else _best_key_q
    sup_keys = list(supplement.keys())

    for period, vals in base.items():
        sk = _best(period, sup_keys)
        if not sk:
            continue
        for k, v in supplement[sk].items():
            if vals.get(k) is None and v is not None:
                vals[k] = v

    # Neue Perioden aus supplement hinzufügen
    if annual:
        base_years = {_extract_year(p) for p in base}
        for period, vals in supplement.items():
            if _extract_year(period) not in base_years:
                if any(v is not None for v in vals.values()):
                    base[period] = dict(vals)
    else:
        base_yms = {p[:7] for p in base}
        for period, vals in supplement.items():
            if period[:7] not in base_yms:
                if any(v is not None for v in vals.values()):
                    base[period] = dict(vals)

    return base


# ═══════════════════════════════════════════════════════════════════════════════
# DATENBESCHAFFUNG (alle Quellen)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_eps_rev(
    ticker:      str,
    period_type: str,
    db:          DB,
    exchange:    str  = "",
    debug:       bool = False,
) -> tuple[dict[str, dict], str]:
    """
    Sammelt EPS + Revenue aus allen verfügbaren Quellen.
    Reihenfolge: Macrotrends → StockAnalysis → MarketBeat → DB
    Gibt (combined_data, source_label) zurück.
    """
    is_annual = (period_type == "annual")
    combined:     dict[str, dict] = {}
    sources_used: list[str]       = []

    def _has_data(d: dict[str, dict], key: str) -> int:
        return sum(1 for v in d.values() if v.get(key) is not None)

    # ── 1. Macrotrends (Primary) ─────────────────────────────────────────────
    console.print(f"    [bold]MT {period_type}[/bold] …")
    mt_data = scrape_macrotrends(ticker, period_type, debug=debug)
    if mt_data:
        eps_cnt = _has_data(mt_data, "trailing_eps")
        rev_cnt = _has_data(mt_data, "total_revenue")
        console.print(
            f"      [green]✓ Macrotrends: {len(mt_data)} Perioden "
            f"(EPS:{eps_cnt} Rev:{rev_cnt})[/green]"
        )
        sources_used.append("macrotrends")
        combined = mt_data
    else:
        console.print(f"      [yellow]⚠ Macrotrends leer[/yellow]")

    time.sleep(1.0)

    # ── 2. StockAnalysis (Fallback wenn EPS oder Revenue fehlt) ─────────────
    eps_ok = _has_data(combined, "trailing_eps")
    rev_ok = _has_data(combined, "total_revenue")
    needs_sa = eps_ok == 0 or rev_ok == 0

    if needs_sa:
        console.print(f"    [bold]SA {period_type}[/bold] (Fallback) …")
        sa_data = scrape_stockanalysis(ticker, period_type, debug=debug)
        if sa_data:
            console.print(
                f"      [green]✓ StockAnalysis: {len(sa_data)} Perioden "
                f"(EPS:{_has_data(sa_data,'trailing_eps')} "
                f"Rev:{_has_data(sa_data,'total_revenue')})[/green]"
            )
            sources_used.append("sa")
            combined = _merge(combined, sa_data, annual=is_annual)
        else:
            console.print(f"      [yellow]⚠ StockAnalysis leer[/yellow]")
        time.sleep(0.8)

    # ── 3. MarketBeat (Fallback) ──────────────────────────────────────────────
    eps_ok = _has_data(combined, "trailing_eps")
    rev_ok = _has_data(combined, "total_revenue")
    needs_mb = eps_ok == 0 or rev_ok == 0

    if needs_mb:
        if not exchange:
            with console.status(f"  {ticker}: MB Exchange suchen …"):
                exchange, _ = find_exchange_mb(ticker, debug=debug)
        if exchange:
            console.print(f"    [bold]MB {period_type}[/bold] (Fallback) …")
            mb_data = scrape_marketbeat(ticker, exchange, period_type, debug=debug)
            if mb_data:
                console.print(
                    f"      [green]✓ MarketBeat: {len(mb_data)} Perioden "
                    f"(EPS:{_has_data(mb_data,'trailing_eps')} "
                    f"Rev:{_has_data(mb_data,'total_revenue')})[/green]"
                )
                sources_used.append("mb")
                combined = _merge(combined, mb_data, annual=is_annual)
            else:
                console.print(f"      [yellow]⚠ MarketBeat leer[/yellow]")
            time.sleep(0.8)

    # ── 4. DB Fundamentals (letzter Ausweg) ───────────────────────────────────
    eps_ok = _has_data(combined, "trailing_eps")
    rev_ok = _has_data(combined, "total_revenue")
    if eps_ok == 0 or rev_ok == 0:
        console.print(f"    [bold]DB Fundamentals[/bold] (letzter Ausweg) …")
        db_data = load_from_db(ticker, period_type, db)
        if db_data:
            console.print(
                f"      [green]✓ DB: {len(db_data)} Perioden "
                f"(EPS:{_has_data(db_data,'trailing_eps')} "
                f"Rev:{_has_data(db_data,'total_revenue')})[/green]"
            )
            sources_used.append("db")
            combined = _merge(combined, db_data, annual=is_annual)
        else:
            console.print(f"      [dim]DB ebenfalls leer[/dim]")

    source_label = "+".join(sources_used) if sources_used else "none"
    return combined, source_label


# ═══════════════════════════════════════════════════════════════════════════════
# YoY BERECHNUNG
# ═══════════════════════════════════════════════════════════════════════════════

def _yoy_growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    if prior == 0:
        return None
    return round((current - prior) / abs(prior), 6)


def _find_prior_quarterly(period_end: str, candidates: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    try:
        target = datetime.strptime(period_end[:10], "%Y-%m-%d")
    except ValueError:
        return None
    prior_year_target = target.replace(year=target.year - 1)
    best, best_key, best_diff = None, None, float("inf")
    for k, v in candidates:
        try:
            d    = datetime.strptime(k[:10], "%Y-%m-%d")
            diff = abs((d - prior_year_target).days)
            if diff < best_diff and diff <= 46:
                best_diff, best_key, best = diff, k, v
        except ValueError:
            pass
    return (best_key, best) if best is not None else None


def _find_prior_annual(period_end: str, candidates: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    yr = _extract_year(period_end)
    if not yr:
        return None
    prior_yr = str(int(yr) - 1)
    for k, v in candidates:
        if _extract_year(k) == prior_yr:
            return k, v
    return None


def compute_growth_from_data(
    ticker:      str,
    period_type: str,
    data:        dict[str, dict],
    source:      str,
    debug:       bool = False,
) -> list[dict]:
    """Berechnet YoY-Wachstum aus den gescrapten Rohdaten."""
    limit      = MAX_QUARTERLY if period_type == "quarterly" else MAX_ANNUAL
    find_prior = _find_prior_quarterly if period_type == "quarterly" else _find_prior_annual

    # Sortiert (neueste zuerst)
    sorted_periods = sorted(data.keys(), reverse=True)
    current_periods = sorted_periods[:limit]
    all_pairs       = [(p, data[p]) for p in sorted_periods]

    records = []
    for period_end in current_periods:
        cur_vals = data[period_end]
        eps_cur  = cur_vals.get("trailing_eps")
        rev_cur  = cur_vals.get("total_revenue")

        candidates = [(p, v) for p, v in all_pairs if p != period_end]
        prior_result = find_prior(period_end, candidates)

        if prior_result is None:
            if debug:
                console.print(f"[dim]  {ticker} {period_type} {period_end}: kein Vorjahr[/dim]")
            records.append({
                "ticker":           ticker,
                "period_end":       period_end,
                "period_type":      period_type,
                "form":             "10-Q" if period_type == "quarterly" else "10-K",
                "earningsGrowth":   None,
                "revenueGrowth":    None,
                "eps_current":      eps_cur,
                "eps_prior_year":   None,
                "rev_current":      rev_cur,
                "rev_prior_year":   None,
                "prior_period_end": None,
                "source":           source,
            })
            continue

        prior_period_end, prior_vals = prior_result
        eps_prior = prior_vals.get("trailing_eps")
        rev_prior = prior_vals.get("total_revenue")

        eg = _yoy_growth(eps_cur, eps_prior)
        rg = _yoy_growth(rev_cur, rev_prior)

        if debug:
            console.print(
                f"[dim]  {ticker} {period_type} {period_end} vs {prior_period_end}: "
                f"EPS {eps_cur} vs {eps_prior} → {eg}  |  "
                f"Rev {rev_cur} vs {rev_prior} → {rg}[/dim]"
            )

        records.append({
            "ticker":           ticker,
            "period_end":       period_end,
            "period_type":      period_type,
            "form":             "10-Q" if period_type == "quarterly" else "10-K",
            "earningsGrowth":   eg,
            "revenueGrowth":    rg,
            "eps_current":      eps_cur,
            "eps_prior_year":   eps_prior,
            "rev_current":      rev_cur,
            "rev_prior_year":   rev_prior,
            "prior_period_end": prior_period_end,
            "source":           source,
        })

    return records


# ═══════════════════════════════════════════════════════════════════════════════
# RENDERING
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_pct(v: float | None, good_positive: bool = True) -> Text:
    if v is None:
        return Text("–", style="dim")
    pct = v * 100
    s   = f"{pct:+.1f}%"
    if good_positive:
        style = "bold green" if pct >= 0 else "bold red"
    else:
        style = "bold red" if pct >= 0 else "bold green"
    return Text(s, style=style)


def _fmt_float(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "–"
    if abs(v) >= 1e12: return f"{v/1e12:.{decimals}f}T"
    if abs(v) >= 1e9:  return f"{v/1e9:.{decimals}f}B"
    if abs(v) >= 1e6:  return f"{v/1e6:.{decimals}f}M"
    return f"{v:.{decimals}f}"


def render_growth(records: list[dict], title: str) -> None:
    if not records:
        console.print(f"  [dim]Keine {title}-Daten.[/dim]\n")
        return

    tbl = Table(
        title=f"[bold]{title}[/bold]",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="bright_black",
        show_lines=True,
        expand=False,
        padding=(0, 1),
    )
    tbl.add_column("Periode",     style="bold white", min_width=12)
    tbl.add_column("Vorjahr",     style="dim",        min_width=12)
    tbl.add_column("EPS aktuell", justify="right",    min_width=11)
    tbl.add_column("EPS Vorjahr", justify="right",    min_width=11)
    tbl.add_column("EPS YoY",     justify="center",   min_width=10)
    tbl.add_column("Rev aktuell", justify="right",    min_width=11)
    tbl.add_column("Rev Vorjahr", justify="right",    min_width=11)
    tbl.add_column("Rev YoY",     justify="center",   min_width=10)
    tbl.add_column("Quelle",      justify="left",     min_width=8)

    for r in records:
        tbl.add_row(
            r["period_end"],
            r["prior_period_end"] or "–",
            _fmt_float(r["eps_current"]),
            _fmt_float(r["eps_prior_year"]),
            _fmt_pct(r["earningsGrowth"]),
            _fmt_float(r["rev_current"]),
            _fmt_float(r["rev_prior_year"]),
            _fmt_pct(r["revenueGrowth"]),
            r.get("source", "")[:16],
        )

    console.print(tbl)
    console.print()


def render_summary(ticker: str, records: list[dict]) -> None:
    q_recs = [r for r in records if r["period_type"] == "quarterly"]
    a_recs = [r for r in records if r["period_type"] == "annual"]

    def _avg(lst, key):
        vals = [r[key] for r in lst if r.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    lines = []
    for label, recs in [("Quarterly", q_recs), ("Annual", a_recs)]:
        if not recs:
            continue
        avg_eg = _avg(recs, "earningsGrowth")
        avg_rg = _avg(recs, "revenueGrowth")
        eg_str = f"{avg_eg*100:+.1f}%" if avg_eg is not None else "–"
        rg_str = f"{avg_rg*100:+.1f}%" if avg_rg is not None else "–"
        eg_col = "green" if (avg_eg or 0) >= 0 else "red"
        rg_col = "green" if (avg_rg or 0) >= 0 else "red"
        lines.append(
            f"[bold]{label}[/bold]  "
            f"Ø EPS-Wachstum: [{eg_col}]{eg_str}[/{eg_col}]  │  "
            f"Ø Rev-Wachstum: [{rg_col}]{rg_str}[/{rg_col}]"
        )

    if lines:
        console.print(Panel(
            "\n".join(lines),
            title=f"📈 {ticker} – Wachstums-Zusammenfassung",
            border_style="bright_black",
        ))
        console.print()


# ═══════════════════════════════════════════════════════════════════════════════
# TICKER-VERARBEITUNG
# ═══════════════════════════════════════════════════════════════════════════════

def process_ticker(
    ticker:   str,
    db:       DB,
    exchange: str  = "",
    debug:    bool = False,
) -> list[dict]:
    """Scrapt Daten, berechnet Growth-Records und gibt sie zurück."""
    all_records: list[dict] = []

    for period_type in ("quarterly", "annual"):
        console.print(f"  [cyan]{period_type.capitalize()}[/cyan]")
        data, source = fetch_eps_rev(ticker, period_type, db, exchange=exchange, debug=debug)

        if not data:
            console.print(f"    [yellow]⚠ Keine Daten für {ticker} {period_type}[/yellow]")
            continue

        eps_cnt = sum(1 for v in data.values() if v.get("trailing_eps") is not None)
        rev_cnt = sum(1 for v in data.values() if v.get("total_revenue") is not None)
        console.print(
            f"    [dim]Gesamt: {len(data)} Perioden "
            f"| EPS: {eps_cnt} | Rev: {rev_cnt} | Quelle: {source}[/dim]"
        )

        records = compute_growth_from_data(ticker, period_type, data, source, debug=debug)
        all_records.extend(records)

        time.sleep(1.0)

    return all_records


def display_ticker(ticker: str, records: list[dict]) -> None:
    console.print(Panel(f"[bold cyan]{ticker}[/bold cyan]", expand=False, border_style="cyan"))
    q_recs = [r for r in records if r["period_type"] == "quarterly"]
    a_recs = [r for r in records if r["period_type"] == "annual"]
    render_growth(q_recs, f"📅 Quarterly Growth ({len(q_recs)} Perioden)")
    render_growth(a_recs, f"📆 Annual Growth ({len(a_recs)} Perioden)")
    if records:
        render_summary(ticker, records)


# ═══════════════════════════════════════════════════════════════════════════════
# TICKERS.TXT
# ═══════════════════════════════════════════════════════════════════════════════

def load_tickers(path: str) -> list[tuple[str, str]]:
    tickers: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    t, ex = line.split(":", 1)
                    tickers.append((t.strip().upper(), ex.strip().upper()))
                else:
                    tickers.append((line.upper(), ""))
    except FileNotFoundError:
        console.print(f"[red]❌ Datei nicht gefunden: {path}[/red]")
        sys.exit(1)
    return tickers


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Growth Scraper v2 – YoY EPS & Revenue Wachstum")
    p.add_argument("ticker",               nargs="?",           help="Einzelner Ticker")
    p.add_argument("--exchange",    "-e",  default="",          help="Exchange (NASDAQ, NYSE …)")
    p.add_argument("--tickers-file", "-f", default=TICKERS_FILE,
                   help=f"Ticker-Datei (Standard: {TICKERS_FILE})")
    p.add_argument("--db",                 default="data.db",   help="DB-Pfad")
    p.add_argument("--delay",              type=float, default=3.0,
                   help="Pause zwischen Tickern in Sekunden")
    p.add_argument("--debug",       "-d",  action="store_true")
    args = p.parse_args()

    console.print()
    console.print(Panel(
        "[bold cyan]Growth Scraper v2[/bold cyan]\n"
        "[dim]YoY EPS & Revenue Wachstum  (Quarterly + Annual)[/dim]\n"
        "[dim]Quellen: Macrotrends (Primary) → StockAnalysis → MarketBeat → DB[/dim]\n"
        f"[dim]Limit: {MAX_QUARTERLY} Quartale / {MAX_ANNUAL} Jahreswerte pro Ticker[/dim]",
        border_style="cyan", expand=False,
    ))
    console.print()

    db = DB(args.db)
    console.print(f"  [dim]DB: {db.path}[/dim]\n")

    # ── Einzelner Ticker ──────────────────────────────────────────────────────
    if args.ticker:
        ticker   = args.ticker.upper()
        exchange = args.exchange.upper().strip()
        console.rule(f"[bold cyan]{ticker}[/bold cyan]")
        console.print()

        records = process_ticker(ticker, db, exchange=exchange, debug=args.debug)
        if records:
            display_ticker(ticker, records)
            ins, upd = db.upsert_growth(records)
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]"
            )
        else:
            console.print(f"  [yellow]⚠ Keine Daten für {ticker}[/yellow]")

        db.close()
        console.print()
        return

    # ── Batch ─────────────────────────────────────────────────────────────────
    import os
    if os.path.exists(args.tickers_file):
        tickers = load_tickers(args.tickers_file)
        source  = args.tickers_file
    else:
        raw_tickers = db.get_all_tickers()
        tickers = [(t, "") for t in raw_tickers]
        source  = "DB"

    if not tickers:
        console.print(f"[yellow]⚠ Keine Ticker gefunden (Quelle: {source}).[/yellow]")
        db.close()
        sys.exit(0)

    console.print(
        f"[bold]Batch-Modus:[/bold] [cyan]{len(tickers)} Ticker[/cyan] "
        f"aus [dim]{source}[/dim]\n"
    )

    total_ins = total_upd = 0
    ok: list[str]     = []
    failed: list[str] = []

    for i, (ticker, exchange) in enumerate(tickers, 1):
        console.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        console.print()

        records = process_ticker(ticker, db, exchange=exchange, debug=args.debug)
        if records:
            display_ticker(ticker, records)
            ins, upd = db.upsert_growth(records)
            total_ins += ins
            total_upd += upd
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]\n"
            )
            ok.append(ticker)
        else:
            failed.append(ticker)

        console.print()
        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    console.rule("[bold]Batch-Ergebnis[/bold]")
    console.print()
    lines = (
        f"[bold]Verarbeitet:[/bold] [green]{len(ok)} ✅[/green]  [red]{len(failed)} ❌[/red]\n"
        f"[bold]DB:[/bold] [green]+{total_ins} neu[/green]  [yellow]~{total_upd} aktualisiert[/yellow]"
    )
    if failed:
        lines += f"\n[red]Keine Daten:[/red] {', '.join(failed)}"
    console.print(Panel(lines, title="📦 Batch-Zusammenfassung", border_style="bright_black"))
    console.print()


if __name__ == "__main__":
    main()