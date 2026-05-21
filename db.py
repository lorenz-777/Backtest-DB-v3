#!/usr/bin/env python3
"""
db.py – Zentrale SQLite-Datenbankschicht
=========================================
Tabellen:
  - earnings     (EPS & Revenue Schätzungen vs. Aktuals)
  - fundamentals (EPS, Revenue, NetIncome, Debt, Equity – Quarterly + Annual)
  - growth       (YoY EPS & Revenue Wachstum – aus fundamentals berechnet)

Verwendung:
    from db import DB
    db = DB()
    db.upsert_earnings(ticker, data)
    db.upsert_fundamentals(ticker, records)
    db.upsert_growth(records)
    db.close()
"""

import sqlite3
from datetime import datetime, timezone


DB_FILE = "data.db"


class DB:
    def __init__(self, path: str = DB_FILE):
        self.path = path
        self.con  = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    # ─── Schema ──────────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS earnings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT    NOT NULL,
                period           TEXT    NOT NULL,
                period_type      TEXT    NOT NULL,
                epsEstimate      TEXT,
                epsActual        TEXT,
                epsBeat          TEXT,
                revenueEstimate  TEXT,
                revenueActual    TEXT,
                revenueBeat      TEXT,
                scraped_at       TEXT    NOT NULL,
                UNIQUE (ticker, period, period_type)
            );

            CREATE INDEX IF NOT EXISTS idx_earnings_ticker
                ON earnings (ticker);

            CREATE INDEX IF NOT EXISTS idx_earnings_scraped
                ON earnings (scraped_at);

            -- ── Fundamentaldaten ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS fundamentals (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker                   TEXT    NOT NULL,
                short_name               TEXT,
                period_end               TEXT    NOT NULL,
                period_type              TEXT    NOT NULL,  -- 'quarterly' | 'annual'
                form                     TEXT,              -- '10-Q' | '10-K'
                trailing_eps             REAL,
                total_revenue            REAL,
                net_income_to_common     REAL,
                profit_margins           REAL,
                total_debt               REAL,
                total_stockholder_equity REAL,
                debt_to_equity           REAL,
                source                   TEXT,              -- 'marketbeat' | 'macrotrends'
                scraped_at               TEXT    NOT NULL,
                UNIQUE (ticker, period_end, period_type)
            );

            CREATE INDEX IF NOT EXISTS idx_fund_ticker
                ON fundamentals (ticker);

            CREATE INDEX IF NOT EXISTS idx_fund_period
                ON fundamentals (period_end);

            -- ── Wachstumsdaten ────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS growth (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker            TEXT    NOT NULL,
                period_end        TEXT    NOT NULL,
                period_type       TEXT    NOT NULL,  -- 'quarterly' | 'annual'
                form              TEXT,              -- '10-Q' | '10-K'
                earningsGrowth    REAL,              -- YoY EPS-Wachstum  (z.B. 0.23 = +23%)
                revenueGrowth     REAL,              -- YoY Rev-Wachstum
                eps_current       REAL,              -- EPS dieser Periode
                eps_prior_year    REAL,              -- EPS Vorjahreszeitraum
                rev_current       REAL,              -- Revenue dieser Periode
                rev_prior_year    REAL,              -- Revenue Vorjahreszeitraum
                prior_period_end  TEXT,              -- period_end des Vorjahreszeitraums
                scraped_at        TEXT    NOT NULL,
                UNIQUE (ticker, period_end, period_type)
            );

            CREATE INDEX IF NOT EXISTS idx_growth_ticker
                ON growth (ticker);

            CREATE INDEX IF NOT EXISTS idx_growth_period
                ON growth (period_end);
        """)
        self.con.commit()

    # ─── UPSERT earnings ─────────────────────────────────────────────────────

    def upsert_earnings(self, ticker: str, data: dict) -> tuple[int, int]:
        now      = datetime.now(timezone.utc).isoformat()
        inserted = updated = 0

        for period_type in ("quarterly", "annual"):
            rows = data.get(period_type, [])
            # Nur die letzten 8 Quartale speichern
            rows = rows[:8]
            for row in rows:
                period = row.get("period", "")
                if not period or period == "–":
                    continue
                existing = self.con.execute(
                    "SELECT id FROM earnings WHERE ticker=? AND period=? AND period_type=?",
                    (ticker, period, period_type),
                ).fetchone()
                self.con.execute(
                    """
                    INSERT INTO earnings
                        (ticker, period, period_type,
                         epsEstimate, epsActual, epsBeat,
                         revenueEstimate, revenueActual, revenueBeat,
                         scraped_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(ticker, period, period_type) DO UPDATE SET
                        epsEstimate=excluded.epsEstimate,
                        epsActual=excluded.epsActual,
                        epsBeat=excluded.epsBeat,
                        revenueEstimate=excluded.revenueEstimate,
                        revenueActual=excluded.revenueActual,
                        revenueBeat=excluded.revenueBeat,
                        scraped_at=excluded.scraped_at
                    """,
                    (ticker, period, period_type,
                     row.get("eps_est","–"), row.get("eps_act","–"), row.get("eps_beat","–"),
                     row.get("rev_est","–"), row.get("rev_act","–"), row.get("rev_beat","–"),
                     now),
                )
                if existing: updated  += 1
                else:        inserted += 1

        self.con.commit()
        return inserted, updated

    # ─── UPSERT fundamentals ─────────────────────────────────────────────────

    def upsert_fundamentals(self, records: list[dict]) -> tuple[int, int]:
        """
        Speichert Fundamentaldaten (Liste von Dicts aus fundamentals.py).

        Jedes Dict enthält:
            ticker, short_name, period_end, period_type, form,
            trailing_eps, total_revenue, net_income_to_common,
            profit_margins, total_debt, total_stockholder_equity,
            debt_to_equity, source
        """
        now = datetime.now(timezone.utc).isoformat()
        inserted = updated = 0

        for r in records:
            ticker     = r.get("ticker", "")
            period_end = r.get("period_end", "")
            ptype      = r.get("period_type", "")
            if not ticker or not period_end or not ptype:
                continue

            existing = self.con.execute(
                "SELECT id FROM fundamentals WHERE ticker=? AND period_end=? AND period_type=?",
                (ticker, period_end, ptype),
            ).fetchone()

            self.con.execute(
                """
                INSERT INTO fundamentals
                    (ticker, short_name, period_end, period_type, form,
                     trailing_eps, total_revenue, net_income_to_common,
                     profit_margins, total_debt, total_stockholder_equity,
                     debt_to_equity, source, scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker, period_end, period_type) DO UPDATE SET
                    short_name               = excluded.short_name,
                    form                     = excluded.form,
                    trailing_eps             = excluded.trailing_eps,
                    total_revenue            = excluded.total_revenue,
                    net_income_to_common     = excluded.net_income_to_common,
                    profit_margins           = excluded.profit_margins,
                    total_debt               = excluded.total_debt,
                    total_stockholder_equity = excluded.total_stockholder_equity,
                    debt_to_equity           = excluded.debt_to_equity,
                    source                   = excluded.source,
                    scraped_at               = excluded.scraped_at
                """,
                (ticker,
                 r.get("short_name"),     period_end,                  ptype,
                 r.get("form"),           r.get("trailing_eps"),        r.get("total_revenue"),
                 r.get("net_income_to_common"), r.get("profit_margins"),
                 r.get("total_debt"),     r.get("total_stockholder_equity"),
                 r.get("debt_to_equity"), r.get("source"),              now),
            )
            if existing: updated  += 1
            else:        inserted += 1

        self.con.commit()
        return inserted, updated

    # ─── UPSERT growth ───────────────────────────────────────────────────────

    def upsert_growth(self, records: list[dict]) -> tuple[int, int]:
        """
        Speichert Wachstumsdaten (Liste von Dicts aus growth.py).

        Jedes Dict enthält:
            ticker, period_end, period_type, form,
            earningsGrowth, revenueGrowth,
            eps_current, eps_prior_year,
            rev_current, rev_prior_year,
            prior_period_end
        """
        now = datetime.now(timezone.utc).isoformat()
        inserted = updated = 0

        for r in records:
            ticker     = r.get("ticker", "")
            period_end = r.get("period_end", "")
            ptype      = r.get("period_type", "")
            if not ticker or not period_end or not ptype:
                continue

            existing = self.con.execute(
                "SELECT id FROM growth WHERE ticker=? AND period_end=? AND period_type=?",
                (ticker, period_end, ptype),
            ).fetchone()

            self.con.execute(
                """
                INSERT INTO growth
                    (ticker, period_end, period_type, form,
                     earningsGrowth, revenueGrowth,
                     eps_current, eps_prior_year,
                     rev_current, rev_prior_year,
                     prior_period_end, scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker, period_end, period_type) DO UPDATE SET
                    form             = excluded.form,
                    earningsGrowth   = excluded.earningsGrowth,
                    revenueGrowth    = excluded.revenueGrowth,
                    eps_current      = excluded.eps_current,
                    eps_prior_year   = excluded.eps_prior_year,
                    rev_current      = excluded.rev_current,
                    rev_prior_year   = excluded.rev_prior_year,
                    prior_period_end = excluded.prior_period_end,
                    scraped_at       = excluded.scraped_at
                """,
                (ticker,             period_end,                   ptype,
                 r.get("form"),      r.get("earningsGrowth"),       r.get("revenueGrowth"),
                 r.get("eps_current"), r.get("eps_prior_year"),
                 r.get("rev_current"), r.get("rev_prior_year"),
                 r.get("prior_period_end"), now),
            )
            if existing: updated  += 1
            else:        inserted += 1

        self.con.commit()
        return inserted, updated

    # ─── Lesen ───────────────────────────────────────────────────────────────

    def get_earnings(self, ticker: str, period_type: str | None = None,
                     limit: int = 100) -> list[sqlite3.Row]:
        if period_type:
            return self.con.execute(
                "SELECT * FROM earnings WHERE ticker=? AND period_type=? "
                "ORDER BY period DESC LIMIT ?",
                (ticker.upper(), period_type, limit),
            ).fetchall()
        return self.con.execute(
            "SELECT * FROM earnings WHERE ticker=? "
            "ORDER BY period DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()

    def get_growth(self, ticker: str, period_type: str | None = None,
                   limit: int = 100) -> list[sqlite3.Row]:
        if period_type:
            return self.con.execute(
                "SELECT * FROM growth WHERE ticker=? AND period_type=? "
                "ORDER BY period_end DESC LIMIT ?",
                (ticker.upper(), period_type, limit),
            ).fetchall()
        return self.con.execute(
            "SELECT * FROM growth WHERE ticker=? "
            "ORDER BY period_end DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()

    def get_fundamentals(self, ticker: str, period_type: str | None = None,
                         limit: int = 100) -> list[sqlite3.Row]:
        if period_type:
            return self.con.execute(
                "SELECT * FROM fundamentals WHERE ticker=? AND period_type=? "
                "ORDER BY period_end DESC LIMIT ?",
                (ticker.upper(), period_type, limit),
            ).fetchall()
        return self.con.execute(
            "SELECT * FROM fundamentals WHERE ticker=? "
            "ORDER BY period_end DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()

    def get_all_tickers(self) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT ticker FROM earnings "
            "UNION SELECT DISTINCT ticker FROM fundamentals ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]

    def summary(self) -> dict:
        e = self.con.execute(
            "SELECT COUNT(*) as total, COUNT(DISTINCT ticker) as tickers, "
            "MAX(scraped_at) as last_update FROM earnings"
        ).fetchone()
        f = self.con.execute(
            "SELECT COUNT(*) as total, COUNT(DISTINCT ticker) as tickers, "
            "MAX(scraped_at) as last_update FROM fundamentals"
        ).fetchone()
        g = self.con.execute(
            "SELECT COUNT(*) as total, COUNT(DISTINCT ticker) as tickers, "
            "MAX(scraped_at) as last_update FROM growth"
        ).fetchone()
        return {
            "earnings_rows":        e["total"],
            "earnings_tickers":     e["tickers"],
            "earnings_last_update": e["last_update"],
            "fund_rows":            f["total"],
            "fund_tickers":         f["tickers"],
            "fund_last_update":     f["last_update"],
            "growth_rows":          g["total"],
            "growth_tickers":       g["tickers"],
            "growth_last_update":   g["last_update"],
        }

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ─── CLI: DB befüllen + Status anzeigen ──────────────────────────────────────

if __name__ == "__main__":
    import sys
    import time
    import argparse
    from rich.console import Console
    from rich.panel import Panel

    con = Console()

    ap = argparse.ArgumentParser(
        description="DB befüllen (Earnings + Fundamentals) aus tickers.txt"
    )
    ap.add_argument("--db",      default=DB_FILE,      help="DB-Pfad")
    ap.add_argument("--tickers", default="tickers.txt", help="Ticker-Datei")
    ap.add_argument("--delay",   type=float, default=3.0,
                    help="Pause zwischen Tickern in Sekunden (Standard: 3.0)")
    ap.add_argument("--debug",   action="store_true")
    ap.add_argument("--status",  action="store_true",
                    help="Nur DB-Status anzeigen, nichts scrapen")
    args = ap.parse_args()

    # ── Nur Status ────────────────────────────────────────────────────────────
    if args.status:
        with DB(args.db) as db:
            info = db.summary()
        con.print(f"\n[bold cyan]DB:[/bold cyan] [dim]{args.db}[/dim]")
        con.print(f"  [bold]earnings[/bold]     : {info['earnings_rows']} Zeilen "
                  f"| {info['earnings_tickers']} Ticker "
                  f"| zuletzt: {info['earnings_last_update'] or '–'}")
        con.print(f"  [bold]fundamentals[/bold] : {info['fund_rows']} Zeilen "
                  f"| {info['fund_tickers']} Ticker "
                  f"| zuletzt: {info['fund_last_update'] or '–'}")
        con.print(f"  [bold]growth[/bold]       : {info['growth_rows']} Zeilen "
                  f"| {info['growth_tickers']} Ticker "
                  f"| zuletzt: {info['growth_last_update'] or '–'}\n")
        sys.exit(0)

    # ── Ticker-Liste laden ────────────────────────────────────────────────────
    def _load_tickers(path: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        t, ex = line.split(":", 1)
                        result.append((t.strip().upper(), ex.strip().upper()))
                    else:
                        result.append((line.upper(), ""))
        except FileNotFoundError:
            con.print(f"[red]❌ Ticker-Datei nicht gefunden: {path}[/red]")
            sys.exit(1)
        return result

    tickers = _load_tickers(args.tickers)
    if not tickers:
        con.print(f"[yellow]⚠ Keine Ticker in {args.tickers}.[/yellow]")
        sys.exit(0)

    # ── Scraper importieren ───────────────────────────────────────────────────
    try:
        from earnings import process_ticker as earn_process
        from fundamentals import process_ticker as fund_process
        from growth import process_ticker as growth_process
    except ImportError as e:
        con.print(f"[red]❌ Import-Fehler: {e}[/red]")
        con.print("   Stelle sicher, dass earnings.py, fundamentals.py und growth.py "
                  "im selben Verzeichnis liegen.")
        sys.exit(1)

    con.print()
    con.print(Panel(
        f"[bold cyan]DB Befüllung[/bold cyan]\n"
        f"[dim]Earnings + Fundamentals + Growth für {len(tickers)} Ticker[/dim]\n"
        f"[dim]Quelle: {args.tickers}  →  DB: {args.db}[/dim]",
        border_style="cyan", expand=False,
    ))
    con.print()

    db = DB(args.db)
    total_earn_ins = total_earn_upd = 0
    total_fund_ins = total_fund_upd = 0
    total_grow_ins = total_grow_upd = 0
    failed_earn:  list[str] = []
    failed_fund:  list[str] = []
    failed_grow:  list[str] = []

    for i, (ticker, exchange) in enumerate(tickers, 1):
        con.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        con.print()

        # ── Earnings ──────────────────────────────────────────────────────────
        con.print(f"[bold]📅 Earnings …[/bold]")
        try:
            earn_data = earn_process(ticker, exchange, debug=args.debug)
            if earn_data:
                ins, upd = db.upsert_earnings(ticker, earn_data)
                total_earn_ins += ins
                total_earn_upd += upd
                con.print(f"  [green]✓[/green] [dim]+{ins} neu  ~{upd} aktualisiert[/dim]")
            else:
                con.print(f"  [yellow]⚠ Keine Earnings-Daten[/yellow]")
                failed_earn.append(ticker)
        except Exception as e:
            con.print(f"  [red]❌ Earnings Fehler: {e}[/red]")
            failed_earn.append(ticker)

        time.sleep(1.5)

        # ── Fundamentals ──────────────────────────────────────────────────────
        con.print(f"[bold]📆 Fundamentals …[/bold]")
        try:
            fund_records = fund_process(ticker, exchange, debug=args.debug)
            if fund_records:
                ins, upd = db.upsert_fundamentals(fund_records)
                total_fund_ins += ins
                total_fund_upd += upd
                con.print(f"  [green]✓[/green] [dim]+{ins} neu  ~{upd} aktualisiert[/dim]")
            else:
                con.print(f"  [yellow]⚠ Keine Fundamentals-Daten[/yellow]")
                failed_fund.append(ticker)
        except Exception as e:
            con.print(f"  [red]❌ Fundamentals Fehler: {e}[/red]")
            failed_fund.append(ticker)

        time.sleep(1.5)

        # ── Growth ────────────────────────────────────────────────────────────
        con.print(f"[bold]📈 Growth …[/bold]")
        try:
            grow_records = growth_process(ticker, db, exchange=exchange, debug=args.debug)
            if grow_records:
                ins, upd = db.upsert_growth(grow_records)
                total_grow_ins += ins
                total_grow_upd += upd
                con.print(f"  [green]✓[/green] [dim]+{ins} neu  ~{upd} aktualisiert[/dim]")
            else:
                con.print(f"  [yellow]⚠ Keine Growth-Daten[/yellow]")
                failed_grow.append(ticker)
        except Exception as e:
            con.print(f"  [red]❌ Growth Fehler: {e}[/red]")
            failed_grow.append(ticker)

        con.print()
        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Abschlussstatus ───────────────────────────────────────────────────────
    con.rule("[bold]Ergebnis[/bold]")
    con.print()

    with DB(args.db) as db_check:
        info = db_check.summary()

    lines = (
        f"[bold]Earnings:[/bold]     "
        f"[green]+{total_earn_ins} neu[/green]  [yellow]~{total_earn_upd} aktualisiert[/yellow]"
        + (f"  [red]| Fehler: {', '.join(failed_earn)}[/red]" if failed_earn else "") + "\n"
        f"[bold]Fundamentals:[/bold] "
        f"[green]+{total_fund_ins} neu[/green]  [yellow]~{total_fund_upd} aktualisiert[/yellow]"
        + (f"  [red]| Fehler: {', '.join(failed_fund)}[/red]" if failed_fund else "") + "\n"
        f"[bold]Growth:[/bold]       "
        f"[green]+{total_grow_ins} neu[/green]  [yellow]~{total_grow_upd} aktualisiert[/yellow]"
        + (f"  [red]| Fehler: {', '.join(failed_grow)}[/red]" if failed_grow else "") + "\n\n"
        f"[dim]DB earnings:     {info['earnings_rows']} Zeilen | {info['earnings_tickers']} Ticker[/dim]\n"
        f"[dim]DB fundamentals: {info['fund_rows']} Zeilen | {info['fund_tickers']} Ticker[/dim]\n"
        f"[dim]DB growth:       {info['growth_rows']} Zeilen | {info['growth_tickers']} Ticker[/dim]"
    )
    con.print(Panel(lines, title="📦 Zusammenfassung", border_style="bright_black"))
    con.print()