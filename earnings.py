#!/usr/bin/env python3
"""
MarketBeat Earnings Scraper v4
================================
EPS & Revenue: Schätzungen vs. Aktuals – letzte 2 Jahre (Quarterly + Annual)

Installation:
    pip install requests beautifulsoup4 rich curl_cffi cloudscraper

Ausführung:
    python marketbeat_earnings.py
    python marketbeat_earnings.py AAPL
    python marketbeat_earnings.py UI --exchange NYSE
    python marketbeat_earnings.py AAPL --debug
"""

import sys
import re
import time
import argparse
from datetime import datetime

from db import DB

from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()


# ─── HTTP Client ─────────────────────────────────────────────────────────────

def get_html(url: str, debug: bool = False) -> tuple[int, str]:
    # 1) curl_cffi – bester Cloudflare-Bypass
    try:
        from curl_cffi import requests as cf
        if debug:
            console.print("[dim]  Methode: curl_cffi[/dim]")
        session = cf.Session(impersonate="chrome124")
        hdrs = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        session.get("https://www.marketbeat.com/", headers=hdrs, timeout=12)
        time.sleep(1.0)
        r = session.get(url, headers={**hdrs, "Referer": "https://www.marketbeat.com/"}, timeout=15)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} Zeichen[/dim]")
        if r.status_code == 200 and len(r.text) > 5000:
            return r.status_code, r.text
    except ImportError:
        if debug:
            console.print("[dim]  curl_cffi nicht installiert[/dim]")
    except Exception as e:
        if debug:
            console.print(f"[dim]  curl_cffi: {e}[/dim]")

    time.sleep(1.5)

    # 2) cloudscraper
    try:
        import cloudscraper
        if debug:
            console.print("[dim]  Methode: cloudscraper[/dim]")
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=4,
        )
        scraper.get("https://www.marketbeat.com/", timeout=12)
        time.sleep(1.2)
        r = scraper.get(url, timeout=18)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} Zeichen[/dim]")
        if r.status_code == 200 and len(r.text) > 5000:
            return r.status_code, r.text
    except ImportError:
        if debug:
            console.print("[dim]  cloudscraper nicht installiert[/dim]")
    except Exception as e:
        if debug:
            console.print(f"[dim]  cloudscraper: {e}[/dim]")

    time.sleep(1.0)

    # 3) requests plain
    try:
        import requests
        if debug:
            console.print("[dim]  Methode: requests[/dim]")
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
        r = requests.Session().get(url, headers=hdrs, timeout=15)
        if debug:
            console.print(f"[dim]  → {r.status_code}, {len(r.text)} Zeichen[/dim]")
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


# ─── Exchange ─────────────────────────────────────────────────────────────────

def find_exchange(ticker: str, debug: bool = False) -> tuple[str, str]:
    for ex in ["NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEMKT"]:
        url = f"https://www.marketbeat.com/stocks/{ex}/{ticker}/earnings/"
        status, html = get_html(url, debug=debug)
        if status == 200 and len(html) > 10000:
            return ex, url
        time.sleep(0.5)
    return "", ""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def clean(s: str) -> str:
    if not s:
        return "–"
    s = s.strip().replace("\xa0", "").replace("\u200b", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s if s and s not in ("-", "—", "N/A", "n/a", "") else "–"


def is_range_value(s: str) -> bool:
    """Erkennt Bereichsangaben wie '$107.2 B - $110.0 B' → True → verwerfen."""
    return bool(re.search(r"\$[\d.,]+\s*[BMKT]?\s*[-–]\s*\$[\d.,]+", s))


def clean_value(s: str) -> str:
    """Gibt '–' zurück wenn s ein Range-Wert ist, sonst s unverändert."""
    if s and s != "–" and is_range_value(s):
        return "–"
    return s


def is_historical(period_str: str) -> bool:
    """
    Gibt True zurück wenn die Periode in der Vergangenheit liegt
    (maximal 2 Jahre zurück, KEIN Zukunftsdatum).

    Zukunfts-Estimates (Q1 2027, Q2 2026 etc.) werden ausgeschlossen.
    """
    if not period_str or period_str == "–":
        return False

    now       = datetime.now()
    cutoff_lo = now.year - 2   # nicht älter als 2 Jahre

    # ── Vollständiges Datum (z.B. "2/6/2026", "2026-01-31") ─────────────────
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            d = datetime.strptime(period_str.strip(), fmt)
            return cutoff_lo <= d.year and d <= now
        except ValueError:
            pass

    # ── Quartal/Jahr-Format (z.B. "Q3 2025", "Q1 2026") ─────────────────────
    m = re.match(r"Q(\d)\s+(20\d{2})", period_str.strip(), re.I)
    if m:
        q, yr = int(m.group(1)), int(m.group(2))
        # Letzter Monat des Quartals
        q_end_month = q * 3
        import calendar as _cal
        last_day = _cal.monthrange(yr, q_end_month)[1]
        d = datetime(yr, q_end_month, last_day)
        return cutoff_lo <= yr and d <= now

    # ── Nur Jahreszahl (z.B. "FY 2024", "2024") ──────────────────────────────
    years = re.findall(r"(20\d{2})", period_str)
    if years:
        yr = int(max(years))
        return cutoff_lo <= yr <= now.year

    return False  # im Zweifel verwerfen


# Rückwärtskompatibilität (intern nicht mehr verwendet)
def is_recent(period_str: str) -> bool:
    return is_historical(period_str)


def to_float(v: str) -> float | None:
    """Wandelt '$1.23B', '$450M', '1.74' in float um."""
    v = v.replace("$", "").replace(",", "").replace("+", "").strip()
    mult = 1.0
    if v.upper().endswith("T"):
        mult, v = 1e12, v[:-1]
    elif v.upper().endswith("B"):
        mult, v = 1e9,  v[:-1]
    elif v.upper().endswith("M"):
        mult, v = 1e6,  v[:-1]
    elif v.upper().endswith("K"):
        mult, v = 1e3,  v[:-1]
    try:
        return float(v) * mult
    except ValueError:
        return None


def calc_beat(est: str, act: str) -> str:
    """Beat/Miss aus Estimate vs. Actual berechnen."""
    e, a = to_float(est), to_float(act)
    if e is None or a is None:
        return "–"
    if e == 0:
        return "Met" if a == 0 else ("Beat" if a > 0 else "Miss")
    pct = (a - e) / abs(e)
    if pct > 0.005:
        return "Beat"
    if pct < -0.005:
        return "Miss"
    return "Met"


def css_beat(text: str, css: str) -> str:
    t, c = text.upper(), css.lower()
    if "BEAT" in t:  return "Beat"
    if "MISS" in t:  return "Miss"
    if "MET"  in t or "IN-LINE" in t:  return "Met"
    if any(k in c for k in ["success", "positive", "green"]):  return "Beat"
    if any(k in c for k in ["danger",  "negative", "red"]):    return "Miss"
    return "–"


# ─── Spalten-Mapper ──────────────────────────────────────────────────────────
#
# Kernproblem: "Revenue Estimate" und "Revenue" landen auf demselben Index.
# Lösung: Jede Kategorie bekommt eine sortierte Liste von Kandidaten
# (spezifischste zuerst). Einmal vergebene Indizes werden gesperrt.

# Regeln: Liste von (muss_enthalten, darf_nicht_enthalten)
# Reihenfolge = Priorität (spezifisch → generisch)

RULES = {
    "period":  [
        (["quarter ending", "reporting date", "report date"], []),
        (["quarter"],   ["eps", "rev", "sales", "est", "act"]),
        (["period"],    ["eps", "rev", "sales"]),
        (["ending"],    ["eps", "rev", "sales"]),
        (["fiscal"],    ["eps", "rev", "sales"]),
        (["date"],      ["eps", "rev", "sales"]),
        (["year"],      ["eps", "rev", "sales", "fiscal year"]),
    ],
    "eps_est": [
        (["consensus eps estimate"], []),
        (["eps estimate"],           []),
        (["eps est"],                []),
        (["consensus eps"],          []),
        (["estimated eps"],          []),
        (["eps forecast"],           []),
        (["eps"],                    ["actual", "act", "reported", "report", "beat", "miss"]),
    ],
    "eps_act": [
        (["reported eps"],  []),
        (["eps actual"],    []),
        (["eps act"],       []),
        (["actual eps"],    []),
        (["eps"],           ["estimate", "est", "consensus", "forecast", "beat", "miss"]),
    ],
    "rev_est": [
        (["revenue estimate"],   []),
        (["revenue est"],        []),
        (["sales estimate"],     []),
        (["sales est"],          []),
        (["rev estimate"],       []),
        (["rev est"],            []),
        (["revenue consensus"],  []),
        (["consensus revenue"],  []),
        (["revenue"],            ["actual", "act", "reported", "report"]),
        (["sales"],              ["actual", "act", "reported", "report"]),
        (["rev"],                ["actual", "act", "reported", "report"]),
    ],
    "rev_act": [
        (["revenue actual"],    []),
        (["revenue act"],       []),
        (["revenue reported"],  []),
        (["reported revenue"],  []),
        (["sales actual"],      []),
        (["sales act"],         []),
        (["rev actual"],        []),
        (["rev act"],           []),
        (["revenue"],           ["estimate", "est", "consensus", "forecast"]),
        (["sales"],             ["estimate", "est", "consensus", "forecast"]),
        (["rev"],               ["estimate", "est", "consensus", "forecast"]),
    ],
    "beat": [
        (["beat/miss"],  []),
        (["beat"],       []),
        (["miss"],       []),
        (["result"],     ["fiscal", "quarter", "period"]),
        (["status"],     []),
        (["vs estimate"], []),
    ],
}


def map_columns(col_lower: list[str], debug: bool = False) -> dict[str, int | None]:
    """
    Ordnet Spalten den Kategorien zu.
    Jeder Index kann nur EINMAL vergeben werden (kein Doppel-Mapping).
    """
    used: set[int] = set()
    result: dict[str, int | None] = {k: None for k in RULES}

    for key, rule_list in RULES.items():
        for (must_have, must_not) in rule_list:
            found = None
            for i, h in enumerate(col_lower):
                if i in used:
                    continue
                if all(m in h for m in must_have) and not any(n in h for n in must_not):
                    found = i
                    break
            if found is not None:
                result[key] = found
                used.add(found)
                break

    if debug:
        console.print(f"[dim]  Spalten-Mapping: {result}[/dim]")
        console.print(f"[dim]  Header: {col_lower}[/dim]")

    return result


# ─── Parsing ─────────────────────────────────────────────────────────────────

def parse_earnings_page(html: str, debug: bool = False) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    company = ""
    for sel in ["h1", ".company-name", "h2"]:
        tag = soup.select_one(sel)
        if tag:
            company = tag.get_text(strip=True)
            break

    result = {"company": company, "quarterly": [], "annual": [], "error": None}

    all_tables = soup.find_all("table")

    if debug:
        console.print(f"\n[dim]  {len(all_tables)} Tabellen gefunden:[/dim]")
        for i, tbl in enumerate(all_tables):
            hdrs = [th.get_text(strip=True) for th in tbl.find_all("th")][:10]
            console.print(f"  [dim]  [{i}] {hdrs}[/dim]")

    for table in all_tables:
        header_row = table.find("tr")
        if not header_row:
            continue

        raw_headers = [th.get_text(" ", strip=True) for th in header_row.find_all(["th", "td"])]
        col_lower   = [h.lower().strip() for h in raw_headers]
        joined      = " ".join(col_lower)

        # Tabelle muss EPS oder Revenue enthalten
        has_eps = any("eps" in c for c in col_lower)
        has_rev = any(k in c for k in ["revenue", "sales", " rev"] for c in col_lower)
        if not has_eps and not has_rev:
            continue

        is_annual = any(k in joined for k in ["annual", "fiscal year", "full year"])

        # Spalten zuordnen
        idx = map_columns(col_lower, debug=debug)

        # Fallback period = Spalte 0
        if idx["period"] is None:
            idx["period"] = 0

        rows = table.find_all("tr")[1:]
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            texts   = [clean(c.get_text(" ", strip=True)) for c in cells]
            classes = [" ".join(c.get("class", [])) for c in cells]

            def g(key):
                i = idx.get(key)
                return texts[i] if i is not None and i < len(texts) else "–"

            def gc(key):
                i = idx.get(key)
                return classes[i] if i is not None and i < len(classes) else ""

            period = g("period")
            if not is_historical(period):
                continue

            eps_est = clean_value(g("eps_est"))
            eps_act = clean_value(g("eps_act"))
            rev_est = clean_value(g("rev_est"))
            rev_act = clean_value(g("rev_act"))

            # Beat/Miss: 1) dedizierte Spalte, 2) CSS-Farben, 3) berechnen
            beat_text = g("beat")
            beat_css  = gc("beat")

            eps_beat = css_beat(beat_text, beat_css)
            if eps_beat == "–":
                # CSS der EPS-Actual-Zelle prüfen
                eps_beat = css_beat(g("eps_act"), gc("eps_act"))
            if eps_beat == "–":
                eps_beat = calc_beat(eps_est, eps_act)

            rev_beat = css_beat(beat_text, beat_css)
            if rev_beat == "–":
                rev_beat = css_beat(g("rev_act"), gc("rev_act"))
            if rev_beat == "–":
                rev_beat = calc_beat(rev_est, rev_act)

            # Mindestens EPS oder Rev muss vorhanden sein
            if all(v == "–" for v in [eps_est, eps_act, rev_est, rev_act]):
                continue

            # Reine Zukunfts-Estimates (noch nicht berichtet): beide Actuals fehlen
            # UND kein Beat/Miss → verwerfen
            if eps_act == "–" and rev_act == "–" and eps_beat == "–" and rev_beat == "–":
                continue

            entry = {
                "period":   period,
                "eps_est":  eps_est,
                "eps_act":  eps_act,
                "eps_beat": eps_beat,
                "rev_est":  rev_est,
                "rev_act":  rev_act,
                "rev_beat": rev_beat,
            }

            target = result["annual"] if is_annual else result["quarterly"]
            if not any(e["period"] == period for e in target):
                target.append(entry)

    # Sortieren: neueste zuerst
    def sort_key(e):
        # Versuche Datum zu parsen
        s = e["period"]
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        # Jahreszahl fallback
        m = re.search(r"(20\d{2})", s)
        if m:
            return datetime(int(m.group(1)), 1, 1)
        return datetime(2000, 1, 1)

    result["quarterly"].sort(key=sort_key, reverse=True)
    result["quarterly"] = result["quarterly"][:8]
    result["annual"].sort(key=sort_key, reverse=True)
    result["annual"] = result["annual"][:4]

    return result


# ─── Rendering ───────────────────────────────────────────────────────────────

def fmt_beat(beat: str) -> Text:
    u = beat.upper()
    if u == "BEAT":  return Text("✅ Beat", style="bold green")
    if u == "MISS":  return Text("❌ Miss", style="bold red")
    if u == "MET":   return Text("➖ Met",  style="bold yellow")
    return Text("–", style="dim")


def fmt_val(val: str, beat: str) -> Text:
    t = Text(val, justify="right")
    u = beat.upper()
    if u == "BEAT":  t.stylize("bold green")
    elif u == "MISS": t.stylize("bold red")
    elif u == "MET":  t.stylize("bold yellow")
    return t


def render_table(entries: list, title: str, period_col: str = "Quartal") -> None:
    if not entries:
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
    tbl.add_column(period_col,  style="bold white", min_width=14)
    tbl.add_column("EPS Est.",  justify="right",    min_width=10)
    tbl.add_column("EPS Akt.",  justify="right",    min_width=10)
    tbl.add_column("EPS",       justify="center",   min_width=11)
    tbl.add_column("Rev Est.",  justify="right",    min_width=13)
    tbl.add_column("Rev Akt.",  justify="right",    min_width=13)
    tbl.add_column("Rev",       justify="center",   min_width=11)

    for e in entries:
        tbl.add_row(
            e["period"],
            e["eps_est"],
            fmt_val(e["eps_act"], e["eps_beat"]),
            fmt_beat(e["eps_beat"]),
            e["rev_est"],
            fmt_val(e["rev_act"], e["rev_beat"]),
            fmt_beat(e["rev_beat"]),
        )

    console.print(tbl)
    console.print()


def render_summary(data: dict) -> None:
    all_e = data["quarterly"] + data["annual"]
    n = len(all_e)
    if not n:
        return
    eb = sum(1 for e in all_e if e["eps_beat"].upper() == "BEAT")
    em = sum(1 for e in all_e if e["eps_beat"].upper() == "MISS")
    rb = sum(1 for e in all_e if e["rev_beat"].upper() == "BEAT")
    rm = sum(1 for e in all_e if e["rev_beat"].upper() == "MISS")
    console.print(Panel(
        f"[bold]Perioden: {n}[/bold]  │  "
        f"[cyan]EPS Beat-Rate: {round(eb/n*100)}%[/cyan] "
        f"([green]{eb}✅[/green] / [red]{em}❌[/red])  │  "
        f"[cyan]Rev Beat-Rate: {round(rb/n*100)}%[/cyan] "
        f"([green]{rb}✅[/green] / [red]{rm}❌[/red])",
        title="📊 Zusammenfassung",
        border_style="bright_black",
    ))


# ─── Main ────────────────────────────────────────────────────────────────────

TICKERS_FILE = "tickers.txt"


# ─── Tickers.txt laden ───────────────────────────────────────────────────────

def load_tickers(path: str) -> list[tuple[str, str]]:
    """
    Liest tickers.txt und gibt eine Liste von (ticker, exchange) zurück.
    Format pro Zeile:  TICKER   oder   TICKER:EXCHANGE
    Kommentare (#) und Leerzeilen werden übersprungen.
    """
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
        console.print(f"  Erstelle [bold]{path}[/bold] mit einem Ticker pro Zeile.")
        sys.exit(1)
    return tickers


# ─── Einzelnen Ticker verarbeiten ────────────────────────────────────────────

def process_ticker(ticker: str, exchange: str = "", debug: bool = False) -> dict | None:
    """Lädt, parst und gibt Daten für einen einzelnen Ticker zurück."""
    if exchange:
        url = f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/earnings/"
    else:
        with console.status(f"[bold green]{ticker}: Exchange suchen …[/bold green]"):
            exchange, url = find_exchange(ticker, debug=debug)

    if not url:
        console.print(f"  [red]❌ '{ticker}' nicht gefunden.[/red]")
        console.print(f"     Tipp: [dim]{ticker}:NASDAQ[/dim] in tickers.txt eintragen")
        return None

    console.print(f"  [green]✓[/green] [dim]{url}[/dim]")

    with console.status(f"[bold green]{ticker}: Lade Seite …[/bold green]"):
        status, html = get_html(url, debug=debug)

    if debug:
        console.print(f"[dim]HTTP {status} | {len(html)} Zeichen[/dim]")

    if status == 403:
        console.print(f"  [bold red]❌ 403 Cloudflare-Block für {ticker}[/bold red]")
        return None

    if status != 200 or len(html) < 5000:
        console.print(f"  [red]❌ HTTP {status} ({len(html)} Zeichen)[/red]")
        return None

    with console.status(f"[bold green]{ticker}: Parse Tabellen …[/bold green]"):
        data = parse_earnings_page(html, debug=debug)

    data["ticker"]   = ticker
    data["exchange"] = exchange
    data["url"]      = url
    return data


# ─── Ausgabe für einen Ticker ─────────────────────────────────────────────────

def display_ticker(data: dict) -> None:
    ticker = data["ticker"]
    info = f"[bold cyan]{ticker}[/bold cyan]"
    if data["company"] and ticker.lower() not in data["company"].lower():
        info += f"  [dim]–  {data['company']}[/dim]"
    console.print(Panel(info, expand=False, border_style="cyan"))
    console.print(f"[dim]{data['url']}[/dim]\n")

    render_table(data["quarterly"], "📅 Quartals-Earnings (letzte 2 Jahre)", "Quartal")
    render_table(data["annual"],    "📆 Annual Earnings (letzte 2 Jahre)",    "Geschäftsjahr")

    if not data["quarterly"] and not data["annual"]:
        console.print(f"[yellow]⚠ Keine Daten für {ticker}.[/yellow]")
    else:
        render_summary(data)
    console.print()


def main():
    p = argparse.ArgumentParser(description="MarketBeat Earnings Scraper v4")
    p.add_argument("ticker",        nargs="?",          help="Einzelner Ticker (optional)")
    p.add_argument("--exchange",    "-e", default="",   help="Exchange (z.B. NASDAQ, NYSE)")
    p.add_argument("--debug",       "-d", action="store_true")
    p.add_argument("--tickers-file", "-f", default=TICKERS_FILE,
                   help=f"Pfad zur Ticker-Liste (Standard: {TICKERS_FILE})")
    p.add_argument("--delay",       type=float, default=2.0,
                   help="Pause zwischen Tickern in Sekunden (Standard: 2.0)")
    args = p.parse_args()

    console.print()
    console.print(Panel(
        "[bold cyan]MarketBeat Earnings Scraper v4[/bold cyan]\n"
        "[dim]EPS & Revenue – Schätzungen vs. Aktuals (letzte 2 Jahre)[/dim]",
        border_style="cyan", expand=False,
    ))
    console.print()

    debug = args.debug

    db = DB()
    console.print(f"  [dim]DB: {db.path}[/dim]\n")

    # ── Einzelner Ticker (CLI-Argument) ──────────────────────────────────────
    if args.ticker:
        ticker   = args.ticker.upper()
        exchange = args.exchange.upper().strip()
        data = process_ticker(ticker, exchange, debug)
        if data:
            console.print()
            display_ticker(data)
            ins, upd = db.upsert_earnings(ticker, data)
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]"
            )
        db.close()
        console.print()
        return

    # ── Batch-Modus: tickers.txt ─────────────────────────────────────────────
    tickers = load_tickers(args.tickers_file)

    if not tickers:
        console.print(f"[yellow]⚠ Keine Ticker in [bold]{args.tickers_file}[/bold] gefunden.[/yellow]")
        db.close()
        sys.exit(0)

    console.print(
        f"[bold]Batch-Modus:[/bold] [cyan]{len(tickers)} Ticker[/cyan] "
        f"aus [dim]{args.tickers_file}[/dim]\n"
    )

    results_ok:   list[dict] = []
    results_fail: list[str]  = []
    total_ins = total_upd = 0

    for i, (ticker, exchange) in enumerate(tickers, 1):
        console.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        console.print()

        data = process_ticker(ticker, exchange, debug)
        if data:
            display_ticker(data)
            ins, upd = db.upsert_earnings(ticker, data)
            total_ins += ins
            total_upd += upd
            console.print(
                f"  [dim]💾 DB: [green]+{ins} neu[/green]  [yellow]~{upd} aktualisiert[/yellow][/dim]\n"
            )
            results_ok.append(data)
        else:
            results_fail.append(ticker)

        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Batch-Zusammenfassung ────────────────────────────────────────────────
    console.rule("[bold]Batch-Ergebnis[/bold]")
    console.print()

    total_q = sum(len(d["quarterly"]) for d in results_ok)
    total_a = sum(len(d["annual"])    for d in results_ok)

    status_lines = (
        f"[bold]Verarbeitet:[/bold] [green]{len(results_ok)} ✅[/green]  "
        f"[red]{len(results_fail)} ❌[/red]"
        f"   [dim]|[/dim]   "
        f"[bold]Datensätze:[/bold] {total_q} Quartale, {total_a} Jahreswerte\n"
        f"[bold]DB:[/bold] [green]+{total_ins} neu eingefügt[/green]  "
        f"[yellow]~{total_upd} aktualisiert[/yellow]"
    )
    if results_fail:
        status_lines += f"\n[red]Fehlgeschlagen:[/red] {', '.join(results_fail)}"

    console.print(Panel(status_lines, title="📦 Batch-Zusammenfassung", border_style="bright_black"))
    console.print()


if __name__ == "__main__":
    main()