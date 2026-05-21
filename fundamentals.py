#!/usr/bin/env python3
"""
fundamentals.py – Fundamentaldaten Scraper v2
=============================================
EPS, Revenue, Net Income, Debt, Equity – Quarterly & Annual

Quellen-Hierarchie (pro Metrik-Gruppe):
  Income (EPS / Revenue / Net Income):
    1. MarketBeat  /financials/
    2. StockAnalysis /financials/?p=quarterly
    3. Macrotrends

  Balance (Debt / Equity):
    1. StockAnalysis /financials/balance-sheet/?p=quarterly   ← NEU, zuverlässigste Quelle
    2. MarketBeat  /balance-sheet/
    3. Macrotrends total-long-term-debt + total-shareholder-equity

Berechnete Felder (kein Scraping):
    profit_margins  = net_income_to_common / total_revenue
    debt_to_equity  = total_debt / total_stockholder_equity

Limit: nur die letzten 8 Quartale (quarterly) / 4 Jahre (annual)

Verwendung:
    python fundamentals.py              # alle Ticker aus tickers.txt
    python fundamentals.py AAPL
    python fundamentals.py AAPL -e NASDAQ
    python fundamentals.py AAPL --debug
"""

import sys
import re
import time
import json
import calendar
import argparse
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from db import DB

console = Console()
TICKERS_FILE = "tickers.txt"

# Maximale Perioden die gespeichert werden
MAX_QUARTERLY = 8
MAX_ANNUAL    = 4


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

def get_html(url: str, debug: bool = False) -> tuple[int, str]:
    """Cloudflare-resistenter HTTP-Client: curl_cffi → cloudscraper → requests."""

    # 1) curl_cffi
    try:
        from curl_cffi import requests as cf
        if debug:
            console.print("[dim]  HTTP: curl_cffi[/dim]")
        session = cf.Session(impersonate="chrome124")
        hdrs = {
            "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language":         "en-US,en;q=0.9",
            "Sec-Fetch-Dest":          "document",
            "Sec-Fetch-Mode":          "navigate",
            "Sec-Fetch-Site":          "none",
            "Upgrade-Insecure-Requests": "1",
        }
        # Warm-up Cookie
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

    # 2) cloudscraper
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

    # 3) requests plain
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
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.google.com/",
        }
        r = requests.Session().get(url, headers=hdrs, timeout=18, allow_redirects=True)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} chars[/dim]")
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


def get_html_sa(url: str, debug: bool = False) -> tuple[int, str]:
    """
    Separater HTTP-Client für StockAnalysis.com (kein Cloudflare-Warm-up nötig,
    aber eigener User-Agent + Accept-Header).
    """
    try:
        import requests
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://stockanalysis.com/",
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
    """'$1.23B', '$450M', '1.74', '-23.5%' → float (Millionen/Milliarden aufgelöst)."""
    if not v or v in ("–", "-", "N/A", ""):
        return None
    v = v.replace("$", "").replace(",", "").replace("+", "").replace("%", "").strip()
    # Klammern = negative Zahl: (123) → -123
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
    """MarketBeat-Periode → ISO-Datum (letzter Tag des Monats)."""
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
    """
    Wandelt beliebige Datumsstings aus verschiedenen Quellen in YYYY-MM-DD.
    Akzeptiert: '2024-09-28', 'Sep 2024', 'Q3 2024', '9/28/2024', '2024'
    """
    raw = raw.strip()
    # Bereits ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    # MarketBeat-Formate
    return parse_mb_period(raw)


def cutoff_date(quarters_back: int = MAX_QUARTERLY) -> str:
    """ISO-Datum: heute minus ~(quarters_back * 3) Monate → Cutoff."""
    now = datetime.now()
    # ~2 Jahre zurück = genug Puffer für 8 Quartale
    yr  = now.year - 2
    return f"{yr}-01-01"


# ═══════════════════════════════════════════════════════════════════════════════
# ── QUELLE 1: MARKETBEAT ──────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

INCOME_ALIASES = {
    "total_revenue": [
        "total revenue", "net revenue", "revenues", "revenue",
        "net sales", "total net revenue", "sales",
    ],
    "net_income": [
        "net income to common", "net income (common)",
        "net income available to common", "net income",
        "net profit", "net earnings",
    ],
    "trailing_eps": [
        "eps (diluted)", "diluted eps", "earnings per share diluted",
        "earnings per share", "basic eps", "eps",
    ],
}

BALANCE_ALIASES = {
    "total_debt": [
        "total debt",
        "total long-term debt",
        "long-term debt & capital lease obligation",
        "long-term debt and capital lease",
        "long-term debt",
        "long term debt",
        "total long term debt",
        "lt debt",
        "total borrowings",
        "total financial debt",
        "short-term debt",
        "notes payable",
        "debt",
        "total liabilities",          # letzter Ausweg für Ticker ohne separaten Debt-Ausweis
    ],
    "total_stockholder_equity": [
        "total stockholders' equity",
        "total stockholders equity",
        "total shareholder equity",
        "total shareholders' equity",
        "total shareholders equity",
        "total stockholder equity",
        "shareholders' equity",
        "shareholders equity",
        "shareholder equity",
        "stockholders' equity",
        "stockholders equity",
        "stockholder equity",
        "total equity",
        "equity attributable to common",
        "common stockholders equity",
        "common shareholders equity",
        "net assets",
        "book value",
        "net equity",
    ],
}


def _match_metric(cell_text: str, aliases: dict) -> str | None:
    t = cell_text.lower().strip()
    for key, alias_list in aliases.items():
        for alias in alias_list:
            if alias in t:
                return key
    return None


def parse_mb_table(html: str, aliases: dict, debug: bool = False) -> dict[str, dict]:
    soup   = BeautifulSoup(html, "html.parser")
    result: dict[str, dict] = {}

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_cells = header_row.find_all(["th", "td"])
        periods: list[str | None] = []
        for cell in header_cells:
            txt = cell.get_text(" ", strip=True).strip()
            if not txt or len(txt) < 4:
                periods.append(None)
            else:
                parsed = parse_mb_period(txt)
                periods.append(parsed if re.search(r"20\d{2}", parsed) else None)

        if not any(periods):
            continue

        if debug:
            console.print(f"[dim]  MB table header: {periods}[/dim]")

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label      = cells[0].get_text(" ", strip=True)
            metric_key = _match_metric(label, aliases)
            if not metric_key:
                continue
            if debug:
                console.print(f"[dim]  MB match: '{label}' → {metric_key}[/dim]")
            for i, cell in enumerate(cells[1:], start=1):
                if i >= len(periods) or not periods[i]:
                    continue
                period  = periods[i]
                val_str = cell.get_text(" ", strip=True)
                val     = to_float(val_str)
                result.setdefault(period, {})
                existing = result[period].get(metric_key, "MISSING")
                if existing == "MISSING":
                    result[period][metric_key] = val
                elif existing is None and val is not None:
                    result[period][metric_key] = val
                elif existing == 0 and val is not None and val != 0 and metric_key == "total_stockholder_equity":
                    # Equity-0 (Parsing-Fehler) durch echten Wert ersetzen
                    result[period][metric_key] = val

    return result


def find_exchange_mb(ticker: str, debug: bool = False) -> tuple[str, str]:
    for ex in ["NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEMKT"]:
        url    = f"https://www.marketbeat.com/stocks/{ex}/{ticker}/financials/"
        status, html = get_html(url, debug=debug)
        if status == 200 and len(html) > 5000:
            return ex, url
        time.sleep(0.4)
    return "", ""


def scrape_mb_income(ticker: str, exchange: str, period_type: str,
                     debug: bool = False) -> dict[str, dict]:
    suffix = "?type=quarterly" if period_type == "quarterly" else ""
    url    = f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/financials/{suffix}"
    if debug:
        console.print(f"[dim]  MB income: {url}[/dim]")
    status, html = get_html(url, debug=debug)
    if status == 200 and len(html) > 5000:
        return parse_mb_table(html, INCOME_ALIASES, debug=debug)
    if debug:
        console.print(f"[dim]  MB income HTTP {status}[/dim]")
    return {}


def scrape_mb_balance(ticker: str, exchange: str, period_type: str,
                      debug: bool = False) -> dict[str, dict]:
    suffix = "?type=quarterly" if period_type == "quarterly" else ""
    url    = f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/balance-sheet/{suffix}"
    if debug:
        console.print(f"[dim]  MB balance: {url}[/dim]")
    status, html = get_html(url, debug=debug)
    if status == 200 and len(html) > 5000:
        return parse_mb_table(html, BALANCE_ALIASES, debug=debug)
    if debug:
        console.print(f"[dim]  MB balance HTTP {status}[/dim]")
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# ── QUELLE 2: STOCKANALYSIS.COM ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
#
#  StockAnalysis hat sehr saubere HTML-Tabellen mit data-field Attributen.
#  Spalten:  ttm | 2024-12-31 | 2024-09-30 | …
#  URL-Schema:
#    Income:  https://stockanalysis.com/stocks/{ticker}/financials/?p=quarterly
#    Balance: https://stockanalysis.com/stocks/{ticker}/financials/balance-sheet/?p=quarterly

SA_INCOME_FIELDS = {
    # data-field Wert  →  unser Metrik-Key
    "revenue":            "total_revenue",
    "revenueGrowth":      None,
    "netIncome":          "net_income",
    "eps":                "trailing_eps",
    "epsDiluted":         "trailing_eps",
}

SA_BALANCE_FIELDS = {
    "totalDebt":               "total_debt",
    "longTermDebt":            "total_debt",          # Fallback wenn totalDebt fehlt
    "totalLiabilities":        None,                  # nicht verwendet
    "totalEquity":             "total_stockholder_equity",
    "shareholdersEquity":      "total_stockholder_equity",
    "totalStockholdersEquity": "total_stockholder_equity",
    "stockholdersEquity":      "total_stockholder_equity",
    "netEquity":               "total_stockholder_equity",
}

# Fallback: Row-Labels falls data-field nicht vorhanden
SA_INCOME_LABELS = {
    "revenue":            "total_revenue",
    "total revenue":      "total_revenue",
    "net income":         "net_income",
    "eps (diluted)":      "trailing_eps",
    "diluted eps":        "trailing_eps",
    "eps":                "trailing_eps",
}

SA_BALANCE_LABELS = {
    "total debt":                    "total_debt",
    "long-term debt":                "total_debt",
    "total long-term debt":          "total_debt",
    "long term debt":                "total_debt",
    "lt debt":                       "total_debt",
    "total borrowings":              "total_debt",
    "total stockholders' equity":    "total_stockholder_equity",
    "total shareholders' equity":    "total_stockholder_equity",
    "shareholders' equity":          "total_stockholder_equity",
    "total equity":                  "total_stockholder_equity",
    "stockholders equity":           "total_stockholder_equity",
    "total stockholder equity":      "total_stockholder_equity",
    "net assets":                    "total_stockholder_equity",
    "total net assets":              "total_stockholder_equity",
    "book value":                    "total_stockholder_equity",
}


def _parse_sa_table(html: str, field_map: dict, label_map: dict,
                    debug: bool = False) -> dict[str, dict]:
    """
    Parst eine StockAnalysis-Tabelle.

    StockAnalysis rendert die Tabelle als reguläres <table> mit:
      - Kopfzeile: 'Fiscal Year' | TTM | YYYY-MM-DD | YYYY-MM-DD | …
      - Datenzeilen: <td data-field="revenue">…</td>  oder Klassen-Label

    Rückgabe: { "2024-09-28": {"total_revenue": ..., "net_income": ...}, ... }
    """
    soup   = BeautifulSoup(html, "html.parser")
    result: dict[str, dict] = {}

    # SA rendert in einem <table> mit thead / tbody
    table = soup.find("table")
    if not table:
        # Manchmal in einem div.financial-table
        table = soup.find("div", class_=re.compile(r"financial", re.I))
        if not table:
            if debug:
                console.print("[dim]  SA: keine Tabelle gefunden[/dim]")
            return result

    # ── Header-Zeile → Perioden ───────────────────────────────────────────────
    thead = table.find("thead")
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
            periods.append(None)          # TTM überspringen
            continue
        parsed = normalize_period(txt)
        periods.append(parsed if re.search(r"20\d{2}", parsed) else None)

    if debug:
        console.print(f"[dim]  SA periods: {periods}[/dim]")

    # ── Datenzeilen ───────────────────────────────────────────────────────────
    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # Metrik bestimmen: 1) data-field Attribut, 2) Label-Text
        metric_key = None
        first_cell = cells[0]
        data_field = first_cell.get("data-field", "").strip()
        if data_field and data_field in field_map:
            metric_key = field_map[data_field]
        if metric_key is None:
            label = first_cell.get_text(" ", strip=True).lower().strip()
            metric_key = label_map.get(label)
            if metric_key is None:
                # Partial-Match
                for lbl, key in label_map.items():
                    if lbl in label:
                        metric_key = key
                        break

        if metric_key is None:
            continue

        if debug:
            console.print(f"[dim]  SA row → {metric_key}[/dim]")

        for i, cell in enumerate(cells[1:], start=1):
            if i >= len(periods) or not periods[i]:
                continue
            period  = periods[i]
            val_str = cell.get_text(" ", strip=True)
            val     = to_float(val_str)
            result.setdefault(period, {})
            # total_stockholder_equity = 0 ist nahezu immer ein Parsing-Fehler → verwerfen.
            # total_debt = 0 ist ein valider Wert (schuldenfreie Firma) → NICHT verwerfen.
            # Ausnahme: rohe "0"-Strings ohne Einheit (z.B. Zelle leer aber als 0 geparst)
            # werden nur für Equity verworfen, nicht für Debt.
            if metric_key == "total_stockholder_equity" and val == 0:
                val = None
            # Nur setzen wenn noch nicht vorhanden ODER bisheriger Wert None war
            # (0 bei total_debt ist gültig und darf nicht durch None überschrieben werden)
            existing = result[period].get(metric_key, "MISSING")
            if existing == "MISSING":
                result[period][metric_key] = val
            elif existing is None and val is not None:
                result[period][metric_key] = val
            elif existing == 0 and val is not None and val != 0 and metric_key == "total_stockholder_equity":
                # Equity-0 (Parsing-Fehler) durch echten Wert ersetzen
                result[period][metric_key] = val

    return result


def scrape_sa_income(ticker: str, period_type: str, debug: bool = False) -> dict[str, dict]:
    p   = "quarterly" if period_type == "quarterly" else "annual"
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/?p={p}"
    if debug:
        console.print(f"[dim]  SA income: {url}[/dim]")
    status, html = get_html_sa(url, debug=debug)
    if status == 200 and len(html) > 3000:
        return _parse_sa_table(html, SA_INCOME_FIELDS, SA_INCOME_LABELS, debug=debug)
    if debug:
        console.print(f"[dim]  SA income HTTP {status}[/dim]")
    return {}


def scrape_sa_balance(ticker: str, period_type: str, debug: bool = False) -> dict[str, dict]:
    p   = "quarterly" if period_type == "quarterly" else "annual"
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/financials/balance-sheet/?p={p}"
    if debug:
        console.print(f"[dim]  SA balance: {url}[/dim]")
    status, html = get_html_sa(url, debug=debug)
    if status == 200 and len(html) > 3000:
        return _parse_sa_table(html, SA_BALANCE_FIELDS, SA_BALANCE_LABELS, debug=debug)
    if debug:
        console.print(f"[dim]  SA balance HTTP {status}[/dim]")
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# ── QUELLE 3: MACROTRENDS (Fallback) ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

MT_METRICS = {
    "total_revenue":            "revenue",
    "net_income":               "net-income",
    "trailing_eps":             "eps-earnings-per-share-diluted",
    "total_debt":               "total-long-term-debt",
    "total_stockholder_equity": "total-shareholder-equity",
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
    url = (
        f"https://www.macrotrends.net/stocks/charts/{ticker}/{slug}/{metric_slug}?freq={freq}"
    )
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


def scrape_macrotrends(ticker: str, period_type: str,
                       metrics: list[str] | None = None,
                       debug: bool = False) -> dict[str, dict]:
    """
    Scrapt Macrotrends für eine Auswahl von Metriken.
    metrics=None  →  alle MT_METRICS
    metrics=["total_debt", "total_stockholder_equity"]  →  nur diese
    """
    freq   = "Q" if period_type == "quarterly" else "A"
    slug   = find_mt_slug(ticker, debug=debug)
    to_fetch = {k: v for k, v in MT_METRICS.items() if metrics is None or k in metrics}
    merged: dict[str, dict] = {}

    for metric_key, metric_slug in to_fetch.items():
        time.sleep(1.0)
        data = fetch_mt_metric(ticker, slug, metric_slug, freq, debug=debug)
        for date, val in data.items():
            merged.setdefault(date, {})[metric_key] = val

    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# MERGE & CALCULATE
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_year(period: str) -> str | None:
    """Extrahiert YYYY aus beliebigen Periodenformaten."""
    m = re.search(r"(20\d{2})", period)
    return m.group(1) if m else None


def _merge(base: dict[str, dict], supplement: dict[str, dict],
           keys: list[str] | None = None,
           annual: bool = False) -> dict[str, dict]:
    """
    Ergänzt `base` mit Werten aus `supplement`.
    keys=None  ->  alle Schluessel uebernehmen
    keys=[...]  ->  nur diese Schluessel ergaenzen (nicht ueberschreiben)

    Datumsabgleich:
      annual=False  ->  YYYY-MM exakt, dann fuzzy <=46 Tage
      annual=True   ->  nur YYYY vergleichen (Geschaeftsjahr kann Sep/Dez enden)
    """
    def _best_key_quarterly(period: str, cands: list[str]) -> str | None:
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

    def _best_key_annual(period: str, cands: list[str]) -> str | None:
        yr = _extract_year(period)
        if not yr:
            return None
        for c in cands:
            if _extract_year(c) == yr:
                return c
        return None

    _best_key = _best_key_annual if annual else _best_key_quarterly

    BALANCE_FIELDS = {"total_debt", "total_stockholder_equity"}

    sup_keys = list(supplement.keys())
    for period, vals in base.items():
        sk = _best_key(period, sup_keys)
        if not sk:
            continue
        for k, v in supplement[sk].items():
            if keys is not None and k not in keys:
                continue
            current = vals.get(k)
            if current is None and v is not None:
                # Fehlenden Wert setzen
                vals[k] = v
            elif current == 0 and v is not None and v != 0 and k == "total_stockholder_equity":
                # Equity-0 (Parsing-Fehler) durch echten Wert ersetzen.
                # total_debt == 0 ist valide (schuldenfreie Firma) → NICHT ersetzen.
                vals[k] = v

    # Supplement-Perioden die in base noch nicht existieren -> hinzufuegen
    if annual:
        base_years = {_extract_year(p) for p in base}
        for period, vals in supplement.items():
            if _extract_year(period) not in base_years:
                filtered = {k: v for k, v in vals.items() if keys is None or k in keys}
                if any(v is not None for v in filtered.values()):
                    base[period] = filtered
    else:
        base_yms = {p[:7] for p in base}
        for period, vals in supplement.items():
            if period[:7] not in base_yms:
                filtered = {k: v for k, v in vals.items() if keys is None or k in keys}
                if any(v is not None for v in filtered.values()):
                    base[period] = filtered

    return base


def _recalculate(vals: dict) -> dict:
    """
    Berechnet alle ableitbaren Felder neu.
    Setzt NaN-geschützte Berechnungen:
        profit_margins  = net_income / total_revenue
        debt_to_equity  = total_debt / total_stockholder_equity

    Hinweis: total_debt = 0 ist valide (schuldenfreie Firma) → D/E = 0.00
    """
    rev  = vals.get("total_revenue")
    net  = vals.get("net_income")
    debt = vals.get("total_debt")
    eq   = vals.get("total_stockholder_equity")

    if net is not None and rev and rev != 0:
        vals["profit_margins"] = round(net / rev, 6)
    # D/E berechnen wenn Equity vorhanden und != 0.
    # Debt darf 0 sein (schuldenfreie Firma → D/E = 0.00).
    if debt is not None and eq and eq != 0:
        vals["debt_to_equity"] = round(debt / eq, 6)

    return vals


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD RECORDS
# ═══════════════════════════════════════════════════════════════════════════════

def build_records(
    ticker:      str,
    short_name:  str,
    period_type: str,
    data:        dict[str, dict],
    source:      str,
) -> list[dict]:
    """
    Wandelt gescrapte Rohdaten in DB-Records um.
    Beschränkt auf MAX_QUARTERLY / MAX_ANNUAL neueste Perioden.
    Berechnet profit_margins und debt_to_equity selbst.
    """
    form  = "10-Q" if period_type == "quarterly" else "10-K"
    limit = MAX_QUARTERLY if period_type == "quarterly" else MAX_ANNUAL
    records = []

    for period_end, vals in data.items():
        vals = _recalculate(dict(vals))

        all_vals = [
            vals.get("trailing_eps"),
            vals.get("total_revenue"),
            vals.get("net_income"),
            vals.get("total_debt"),
            vals.get("total_stockholder_equity"),
        ]
        if all(v is None for v in all_vals):
            continue

        records.append({
            "ticker":                   ticker,
            "short_name":               short_name,
            "period_end":               period_end,
            "period_type":              period_type,
            "form":                     form,
            "trailing_eps":             vals.get("trailing_eps"),
            "total_revenue":            vals.get("total_revenue"),
            "net_income_to_common":     vals.get("net_income"),
            "profit_margins":           vals.get("profit_margins"),
            "total_debt":               vals.get("total_debt"),
            "total_stockholder_equity": vals.get("total_stockholder_equity"),
            "debt_to_equity":           vals.get("debt_to_equity"),
            "source":                   source,
        })

    # Neueste zuerst, dann auf Limit beschränken
    records.sort(key=lambda r: r["period_end"], reverse=True)
    return records[:limit]


# ═══════════════════════════════════════════════════════════════════════════════
# TICKER-VERARBEITUNG
# ═══════════════════════════════════════════════════════════════════════════════

def _get_short_name(ticker: str, exchange: str, debug: bool = False) -> str:
    """Extrahiert den Firmennamen aus der MarketBeat-Seite."""
    _, html = get_html(
        f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/financials/", debug=False
    )
    if html:
        soup = BeautifulSoup(html, "html.parser")
        tag  = soup.find("h1") or soup.find("h2")
        if tag:
            return tag.get_text(strip=True)
    return ticker


def process_ticker(
    ticker:   str,
    exchange: str = "",
    debug:    bool = False,
) -> list[dict]:
    """
    Scrapt einen Ticker (quarterly + annual).

    Strategie pro period_type:
      1. Income:   MarketBeat → StockAnalysis → Macrotrends
      2. Balance:  StockAnalysis → MarketBeat → Macrotrends
      3. Merge, berechne alle ableitbaren Felder
      4. Auf 8 Quartale / 4 Jahre begrenzen
    """
    all_records: list[dict] = []

    # Exchange suchen
    if not exchange:
        with console.status(f"[bold green]{ticker}: Exchange suchen …[/bold green]"):
            exchange, _ = find_exchange_mb(ticker, debug=debug)

    if not exchange:
        console.print(f"  [red]❌ {ticker}: Exchange nicht gefunden.[/red]")
        return []

    console.print(f"  [dim]Exchange: {exchange}[/dim]")
    short_name = _get_short_name(ticker, exchange, debug=debug)

    for period_type in ("quarterly", "annual"):
        console.print(f"  [bold]{period_type.capitalize()}[/bold]")
        combined: dict[str, dict] = {}
        sources_used: list[str]   = []
        income_keys  = ["total_revenue", "net_income", "trailing_eps"]
        balance_keys = ["total_debt", "total_stockholder_equity"]
        is_annual    = (period_type == "annual")

        # ════════════════════════════════════════════════════════════════════
        # INCOME (Revenue / Net Income / EPS)
        # Primär:  MarketBeat  → immer versuchen
        # Fallback: Macrotrends (nur wenn MB komplett leer)
        # ════════════════════════════════════════════════════════════════════

        # ── 1a. MB Income (immer) ────────────────────────────────────────────
        with console.status(f"  {ticker} {period_type}: MB Income …"):
            mb_inc = scrape_mb_income(ticker, exchange, period_type, debug=debug)
        time.sleep(1.2)

        if mb_inc:
            # Zähle Perioden mit echten Revenue-Daten
            rev_count = sum(1 for v in mb_inc.values() if v.get("total_revenue") is not None)
            console.print(
                f"    [green]✓ MB Income: {len(mb_inc)} Perioden "
                f"({rev_count} mit Revenue)[/green]"
            )
            sources_used.append("mb_inc")
            _merge(combined, mb_inc, keys=income_keys, annual=is_annual)
        else:
            console.print(f"    [yellow]⚠ MB Income leer[/yellow]")

        # ── 1b. SA Income als sekundäre Revenue-Quelle (immer bei annual, Fallback bei quarterly) ──
        # Bei annual liefert MB oft nur EPS, kein Revenue → SA Income ergänzt
        rev_count_mb = sum(1 for v in combined.values() if v.get("total_revenue") is not None)
        needs_sa_income = is_annual or rev_count_mb == 0
        if needs_sa_income:
            with console.status(f"  {ticker} {period_type}: SA Income ..."):
                sa_inc = scrape_sa_income(ticker, period_type, debug=debug)
            time.sleep(1.0)
            if sa_inc:
                sa_rev = sum(1 for v in sa_inc.values() if v.get("total_revenue") is not None)
                console.print(f"    [green]✓ SA Income: {sa_rev} Perioden mit Revenue[/green]")
                sources_used.append("sa_inc")
                _merge(combined, sa_inc, keys=income_keys, annual=is_annual)
            else:
                if is_annual:
                    console.print(f"    [yellow]⚠ SA Income leer[/yellow]")

        # ── 1c. Macrotrends Income-Fallback (nur wenn immer noch kein Revenue) ──
        has_rev = any(v.get("total_revenue") is not None for v in combined.values())
        if not has_rev:
            console.print(f"    [yellow]⚠ Revenue fehlt → Macrotrends Income …[/yellow]")
            with console.status(f"  {ticker} {period_type}: Macrotrends Income …"):
                mt_inc = scrape_macrotrends(
                    ticker, period_type,
                    metrics=["total_revenue", "net_income", "trailing_eps"],
                    debug=debug,
                )
            time.sleep(1.0)
            if mt_inc:
                rev_count = sum(1 for v in mt_inc.values() if v.get("total_revenue") is not None)
                console.print(f"    [green]✓ MT Income: {rev_count} Perioden mit Revenue[/green]")
                sources_used.append("mt_inc")
                _merge(combined, mt_inc, keys=income_keys, annual=is_annual)
            else:
                console.print(f"    [red]❌ Macrotrends Income ebenfalls leer[/red]")

        # ════════════════════════════════════════════════════════════════════
        # BALANCE (Debt / Equity)
        # Primär:  StockAnalysis  → beste Quelle, zuverlässige HTML-Tabellen
        # Fallback: MarketBeat   → zweite Chance
        # Fallback: Macrotrends  → letzter Ausweg
        # ════════════════════════════════════════════════════════════════════

        # ── 2a. SA Balance (immer) ───────────────────────────────────────────
        with console.status(f"  {ticker} {period_type}: SA Balance …"):
            sa_bal = scrape_sa_balance(ticker, period_type, debug=debug)
        time.sleep(1.0)

        if sa_bal:
            bal_count = sum(
                1 for v in sa_bal.values()
                if v.get("total_debt") is not None or v.get("total_stockholder_equity") is not None
            )
            console.print(
                f"    [green]✓ SA Balance: {bal_count} Perioden mit Debt/Equity[/green]"
            )
            sources_used.append("sa_bal")
            _merge(combined, sa_bal, keys=balance_keys, annual=is_annual)
        else:
            console.print(f"    [yellow]⚠ SA Balance leer[/yellow]")

        # ── 2b. MB Balance Fallback ──────────────────────────────────────────
        # Trigger wenn IRGENDEINE Periode debt oder equity FEHLT (None).
        # total_debt == 0 ist valide (schuldenfreie Firma) → kein Fallback nötig.
        # total_stockholder_equity == 0 ist immer ein Parsing-Fehler → Fallback.
        missing_debt = [p for p, v in combined.items()
                        if v.get("total_debt") is None]
        missing_eq   = [p for p, v in combined.items()
                        if v.get("total_stockholder_equity") is None
                        or v.get("total_stockholder_equity") == 0]
        has_debt = len(missing_debt) == 0
        has_eq   = len(missing_eq)   == 0
        if not has_debt or not has_eq:
            missing = [k for k, flag in [("Debt", not has_debt), ("Equity", not has_eq)] if flag]
            console.print(f"    [yellow]⚠ {', '.join(missing)} in {max(len(missing_debt), len(missing_eq))} Perioden → MB Balance …[/yellow]")
            with console.status(f"  {ticker} {period_type}: MB Balance …"):
                mb_bal = scrape_mb_balance(ticker, exchange, period_type, debug=debug)
            time.sleep(1.2)
            if mb_bal:
                console.print(f"    [green]✓ MB Balance: {len(mb_bal)} Perioden[/green]")
                sources_used.append("mb_bal")
                _merge(combined, mb_bal, keys=balance_keys, annual=is_annual)
            else:
                console.print(f"    [red]❌ MB Balance ebenfalls leer[/red]")

        # ── 2c. Macrotrends Balance Fallback ─────────────────────────────────
        # Nur noch bei echtem None triggern. total_debt == 0 ist valide.
        missing_debt = [p for p, v in combined.items()
                        if v.get("total_debt") is None]
        missing_eq   = [p for p, v in combined.items()
                        if v.get("total_stockholder_equity") is None
                        or v.get("total_stockholder_equity") == 0]
        has_debt = len(missing_debt) == 0
        has_eq   = len(missing_eq)   == 0
        if not has_debt or not has_eq:
            mt_bal_keys = []
            if not has_debt: mt_bal_keys.append("total_debt")
            if not has_eq:   mt_bal_keys.append("total_stockholder_equity")
            console.print(f"    [yellow]⚠ Macrotrends Balance für: {mt_bal_keys} ({max(len(missing_debt), len(missing_eq))} Perioden) …[/yellow]")
            with console.status(f"  {ticker} {period_type}: Macrotrends Balance …"):
                mt_bal = scrape_macrotrends(ticker, period_type, metrics=mt_bal_keys, debug=debug)
            time.sleep(1.0)
            if mt_bal:
                console.print(f"    [green]✓ MT Balance: {len(mt_bal)} Perioden[/green]")
                sources_used.append("mt_bal")
                _merge(combined, mt_bal, keys=mt_bal_keys, annual=is_annual)
            else:
                console.print(f"    [red]❌ Macrotrends Balance ebenfalls leer[/red]")

        if not combined:
            console.print(f"    [red]❌ Keine Daten für {ticker} {period_type}[/red]")
            continue

        # ── Annual: Balance aus Quarterly backfüllen wenn leer ───────────────
        # Viele Quellen liefern Annual-Balance nicht vollständig (z.B. AMD).
        # Strategie: für jede Annual-Periode das letzte Quartal des Jahres suchen
        # und fehlende Balance-Werte daraus übernehmen.
        if period_type == "annual" and all_records:
            q_records = [r for r in all_records if r["period_type"] == "quarterly"]
            if q_records:
                for period_end, vals in combined.items():
                    yr = _extract_year(period_end)
                    if not yr:
                        continue
                    needs_debt = vals.get("total_debt") is None
                    needs_eq   = vals.get("total_stockholder_equity") is None or vals.get("total_stockholder_equity") == 0
                    if not needs_debt and not needs_eq:
                        continue
                    # Letztes Quartal des Jahres suchen
                    yr_quarters = [
                        r for r in q_records
                        if _extract_year(r["period_end"]) == yr
                    ]
                    if not yr_quarters:
                        continue
                    yr_quarters.sort(key=lambda r: r["period_end"], reverse=True)
                    best_q = yr_quarters[0]
                    if needs_debt and best_q.get("total_debt") is not None:
                        vals["total_debt"] = best_q["total_debt"]
                        console.print(
                            f"    [dim]↩ Annual {period_end}: Debt aus Q {best_q['period_end']} übernommen[/dim]"
                        )
                    if needs_eq and best_q.get("total_stockholder_equity") is not None:
                        vals["total_stockholder_equity"] = best_q["total_stockholder_equity"]
                        console.print(
                            f"    [dim]↩ Annual {period_end}: Equity aus Q {best_q['period_end']} übernommen[/dim]"
                        )

        # ── Source-Label ─────────────────────────────────────────────────────
        source_label = "+".join(sources_used) if sources_used else "unknown"

        # ── Records bauen (inkl. Limit + Berechnungen) ────────────────────────
        recs = build_records(ticker, short_name, period_type, combined, source_label)
        console.print(
            f"    [cyan]→ {len(recs)} Perioden gespeichert "
            f"({period_type}, max {MAX_QUARTERLY if period_type == 'quarterly' else MAX_ANNUAL})[/cyan]"
        )
        all_records.extend(recs)
        time.sleep(1.5)

    return all_records


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
# RENDERING
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_float(v: float | None, decimals: int = 2, suffix: str = "") -> str:
    if v is None:
        return "[dim]–[/dim]"
    if abs(v) >= 1e12: return f"{v/1e12:.{decimals}f}T{suffix}"
    if abs(v) >= 1e9:  return f"{v/1e9:.{decimals}f}B{suffix}"
    if abs(v) >= 1e6:  return f"{v/1e6:.{decimals}f}M{suffix}"
    return f"{v:.{decimals}f}{suffix}"


def render_records(records: list[dict], title: str) -> None:
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
    tbl.add_column("Periode",    style="bold white", min_width=12)
    tbl.add_column("EPS",        justify="right",    min_width=8)
    tbl.add_column("Revenue",    justify="right",    min_width=11)
    tbl.add_column("Net Income", justify="right",    min_width=11)
    tbl.add_column("Margin %",   justify="right",    min_width=9)
    tbl.add_column("Debt",       justify="right",    min_width=11)
    tbl.add_column("Equity",     justify="right",    min_width=11)
    tbl.add_column("D/E",        justify="right",    min_width=7)
    tbl.add_column("Src",        justify="left",     min_width=6)

    for r in records:
        pm = f"{r['profit_margins']*100:.1f}%" if r.get("profit_margins") is not None else "–"
        de = f"{r['debt_to_equity']:.2f}"       if r.get("debt_to_equity")  is not None else "–"
        # Debt/Equity farbig markieren wenn vorhanden
        debt_str = fmt_float(r.get("total_debt"))
        eq_str   = fmt_float(r.get("total_stockholder_equity"))
        if r.get("total_debt") is not None:
            debt_str = f"[green]{debt_str}[/green]"
        if r.get("total_stockholder_equity") is not None:
            eq_str = f"[green]{eq_str}[/green]"

        tbl.add_row(
            r["period_end"],
            fmt_float(r.get("trailing_eps"), 2),
            fmt_float(r.get("total_revenue"), 2),
            fmt_float(r.get("net_income_to_common"), 2),
            pm,
            debt_str,
            eq_str,
            de,
            r.get("source", "")[:12],
        )

    console.print(tbl)
    console.print()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Fundamentaldaten Scraper v2")
    p.add_argument("ticker",           nargs="?",         help="Einzelner Ticker")
    p.add_argument("--exchange", "-e", default="",        help="Exchange (NASDAQ, NYSE …)")
    p.add_argument("--debug",    "-d", action="store_true")
    p.add_argument("--tickers-file", "-f", default=TICKERS_FILE,
                   help=f"Ticker-Datei (Standard: {TICKERS_FILE})")
    p.add_argument("--delay", type=float, default=3.0,
                   help="Pause zwischen Tickern in Sekunden (Standard: 3.0)")
    args = p.parse_args()

    console.print()
    console.print(Panel(
        "[bold cyan]Fundamentaldaten Scraper v2[/bold cyan]\n"
        "[dim]EPS · Revenue · Net Income · Debt · Equity  (Quarterly + Annual)[/dim]\n"
        "[dim]Quellen: MarketBeat · StockAnalysis · Macrotrends[/dim]\n"
        f"[dim]Limit: {MAX_QUARTERLY} Quartale / {MAX_ANNUAL} Jahreswerte pro Ticker[/dim]",
        border_style="cyan", expand=False,
    ))
    console.print()

    db = DB()
    console.print(f"  [dim]DB: {db.path}[/dim]\n")

    # ── Einzelner Ticker ──────────────────────────────────────────────────────
    if args.ticker:
        ticker   = args.ticker.upper()
        exchange = args.exchange.upper().strip()

        console.rule(f"[bold cyan]{ticker}[/bold cyan]")
        console.print()

        records = process_ticker(ticker, exchange, args.debug)

        if records:
            q_recs = [r for r in records if r["period_type"] == "quarterly"]
            a_recs = [r for r in records if r["period_type"] == "annual"]
            console.print()
            render_records(q_recs, f"📅 Quarterly ({len(q_recs)} Perioden)")
            render_records(a_recs, f"📆 Annual ({len(a_recs)} Perioden)")

            ins, upd = db.upsert_fundamentals(records)
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]"
            )
        else:
            console.print(f"[yellow]⚠ Keine Daten für {ticker}.[/yellow]")

        db.close()
        console.print()
        return

    # ── Batch-Modus ───────────────────────────────────────────────────────────
    tickers = load_tickers(args.tickers_file)
    if not tickers:
        console.print(f"[yellow]⚠ Keine Ticker in {args.tickers_file}.[/yellow]")
        db.close()
        sys.exit(0)

    console.print(
        f"[bold]Batch-Modus:[/bold] [cyan]{len(tickers)} Ticker[/cyan] "
        f"aus [dim]{args.tickers_file}[/dim]\n"
    )

    total_ins = total_upd = 0
    failed: list[str] = []

    for i, (ticker, exchange) in enumerate(tickers, 1):
        console.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        console.print()

        records = process_ticker(ticker, exchange, args.debug)

        if records:
            q_recs = [r for r in records if r["period_type"] == "quarterly"]
            a_recs = [r for r in records if r["period_type"] == "annual"]
            render_records(q_recs, f"📅 Quarterly")
            render_records(a_recs, f"📆 Annual")

            ins, upd = db.upsert_fundamentals(records)
            total_ins += ins
            total_upd += upd
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]\n"
            )
        else:
            failed.append(ticker)

        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    console.rule("[bold]Batch-Ergebnis[/bold]")
    console.print()
    lines = (
        f"[bold]Verarbeitet:[/bold] [green]{len(tickers)-len(failed)} ✅[/green]  "
        f"[red]{len(failed)} ❌[/red]\n"
        f"[bold]DB:[/bold] [green]+{total_ins} neu[/green]  [yellow]~{total_upd} aktualisiert[/yellow]"
    )
    if failed:
        lines += f"\n[red]Fehlgeschlagen:[/red] {', '.join(failed)}"
    console.print(Panel(lines, title="📦 Zusammenfassung", border_style="bright_black"))
    console.print()


if __name__ == "__main__":
    main()