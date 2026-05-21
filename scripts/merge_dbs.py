#!/usr/bin/env python3
"""
scripts/merge_dbs.py
=====================
Führt alle Teil-Datenbanken (db_batch_*.db) in eine einzige data.db zusammen.

Strategie: UPSERT (neueste scraped_at gewinnt)
  – Für jede Tabelle (earnings, fundamentals, growth) werden alle Zeilen
    aus den Teil-DBs via INSERT OR REPLACE in die Ziel-DB eingefügt.
  – Bei Konflikten (UNIQUE-Constraint) gewinnt der Datensatz mit der
    neueren scraped_at.

Verwendung:
    python scripts/merge_dbs.py --parts-dir db_parts/ --output data.db
    python scripts/merge_dbs.py --parts-dir db_parts/ --output data.db --base existing.db
"""

import argparse
import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime

try:
    from rich.console import Console
    from rich.progress import track
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    class _FallbackConsole:
        def print(self, *a, **k): print(*a)
        def rule(self, *a, **k): print("-" * 60)
    console = _FallbackConsole()


# ─── Tabellen-Definitionen ───────────────────────────────────────────────────
# Für jede Tabelle: welche Spalten gibt es, welche bilden den UNIQUE-Key?

TABLES = {
    "earnings": {
        "unique_cols": ["ticker", "period", "period_type"],
        "all_cols": [
            "ticker", "period", "period_type",
            "epsEstimate", "epsActual", "epsBeat",
            "revenueEstimate", "revenueActual", "revenueBeat",
            "scraped_at",
        ],
    },
    "fundamentals": {
        "unique_cols": ["ticker", "period_end", "period_type"],
        "all_cols": [
            "ticker", "short_name", "period_end", "period_type", "form",
            "trailing_eps", "total_revenue", "net_income_to_common",
            "profit_margins", "total_debt", "total_stockholder_equity",
            "debt_to_equity", "source", "scraped_at",
        ],
    },
    "growth": {
        "unique_cols": ["ticker", "period_end", "period_type"],
        "all_cols": [
            "ticker", "period_end", "period_type", "form",
            "earningsGrowth", "revenueGrowth",
            "eps_current", "eps_prior_year",
            "rev_current", "rev_prior_year",
            "prior_period_end", "scraped_at",
        ],
    },
}


def ensure_schema(con: sqlite3.Connection) -> None:
    """Erstellt Tabellen falls sie noch nicht existieren."""
    con.executescript("""
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
        CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings (ticker);

        CREATE TABLE IF NOT EXISTS fundamentals (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                   TEXT    NOT NULL,
            short_name               TEXT,
            period_end               TEXT    NOT NULL,
            period_type              TEXT    NOT NULL,
            form                     TEXT,
            trailing_eps             REAL,
            total_revenue            REAL,
            net_income_to_common     REAL,
            profit_margins           REAL,
            total_debt               REAL,
            total_stockholder_equity REAL,
            debt_to_equity           REAL,
            source                   TEXT,
            scraped_at               TEXT    NOT NULL,
            UNIQUE (ticker, period_end, period_type)
        );
        CREATE INDEX IF NOT EXISTS idx_fund_ticker ON fundamentals (ticker);

        CREATE TABLE IF NOT EXISTS growth (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker            TEXT    NOT NULL,
            period_end        TEXT    NOT NULL,
            period_type       TEXT    NOT NULL,
            form              TEXT,
            earningsGrowth    REAL,
            revenueGrowth     REAL,
            eps_current       REAL,
            eps_prior_year    REAL,
            rev_current       REAL,
            rev_prior_year    REAL,
            prior_period_end  TEXT,
            scraped_at        TEXT    NOT NULL,
            UNIQUE (ticker, period_end, period_type)
        );
        CREATE INDEX IF NOT EXISTS idx_growth_ticker ON growth (ticker);
    """)
    con.commit()


def get_db_stats(con: sqlite3.Connection) -> dict:
    stats = {}
    for table in TABLES:
        try:
            row = con.execute(
                f"SELECT COUNT(*) as n, COUNT(DISTINCT ticker) as t FROM {table}"
            ).fetchone()
            stats[table] = {"rows": row[0], "tickers": row[1]}
        except sqlite3.OperationalError:
            stats[table] = {"rows": 0, "tickers": 0}
    return stats


def merge_table(
    dst: sqlite3.Connection,
    src: sqlite3.Connection,
    table: str,
) -> tuple[int, int]:
    """
    Mergt eine Tabelle aus src in dst.
    Strategie: neuere scraped_at gewinnt beim UNIQUE-Konflikt.
    Gibt (inserted, updated) zurück.
    """
    cfg      = TABLES[table]
    cols     = cfg["all_cols"]
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))

    # Quell-Zeilen lesen
    try:
        rows = src.execute(
            f"SELECT {col_list} FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0, 0   # Tabelle existiert nicht in src

    if not rows:
        return 0, 0

    # UNIQUE-Cols für Lookup-Dict
    unique_idx = [cols.index(c) for c in cfg["unique_cols"]]

    def unique_key(row):
        return tuple(row[i] for i in unique_idx)

    # Aktuelle Keys + scraped_at aus dst lesen
    try:
        existing = dst.execute(
            f"SELECT {col_list} FROM {table}"
        ).fetchall()
        scraped_at_idx = cols.index("scraped_at")
        dst_keys = {unique_key(r): r[scraped_at_idx] for r in existing}
    except sqlite3.OperationalError:
        dst_keys = {}

    inserted = updated = 0
    to_insert: list[tuple] = []

    for row in rows:
        key = unique_key(row)
        src_ts  = row[cols.index("scraped_at")] if "scraped_at" in cols else ""
        dst_ts  = dst_keys.get(key, "")

        if not dst_ts:
            # Neu
            to_insert.append(row)
            inserted += 1
        elif src_ts > dst_ts:
            # Aktueller in src → ersetzen
            to_insert.append(row)
            updated += 1
        # sonst: dst ist aktueller → überspringen

    if to_insert:
        # INSERT OR REPLACE ist OK weil wir scraped_at selbst vergleichen
        update_cols = [c for c in cols if c not in cfg["unique_cols"] and c != "id"]
        update_set  = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        dst.executemany(
            f"""INSERT INTO {table} ({col_list})
                VALUES ({placeholders})
                ON CONFLICT({', '.join(cfg['unique_cols'])}) DO UPDATE SET
                    {update_set}
                WHERE excluded.scraped_at > {table}.scraped_at
            """,
            to_insert,
        )
        dst.commit()

    return inserted, updated


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge partial SQLite DBs into one")
    ap.add_argument("--parts-dir", required=True,  help="Verzeichnis mit Teil-DBs")
    ap.add_argument("--output",    required=True,  help="Ziel-DB")
    ap.add_argument("--base",      default="",
                    help="Bestehende Basis-DB die als Ausgangspunkt dient (optional)")
    ap.add_argument("--pattern",   default="*.db",
                    help="Datei-Glob für Teil-DBs (Standard: *.db)")
    args = ap.parse_args()

    # ── Teil-DBs finden ───────────────────────────────────────────────────────
    parts = sorted(glob.glob(os.path.join(args.parts_dir, args.pattern)))
    if not parts:
        console.print(f"[yellow]⚠ Keine Teil-DBs gefunden in {args.parts_dir}[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold cyan]Merge[/bold cyan]  {len(parts)} Teil-DBs  →  {args.output}\n")

    # ── Ziel-DB vorbereiten ───────────────────────────────────────────────────
    if args.base and os.path.exists(args.base) and args.base != args.output:
        shutil.copy2(args.base, args.output)
        console.print(f"  [dim]Basis: {args.base}[/dim]")
    elif not os.path.exists(args.output):
        pass  # wird neu erstellt

    dst_con = sqlite3.connect(args.output)
    dst_con.execute("PRAGMA journal_mode=WAL")
    dst_con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(dst_con)

    # Vor-Stats
    before = get_db_stats(dst_con)

    # ── Mergen ────────────────────────────────────────────────────────────────
    total_ins = total_upd = 0
    failed: list[str] = []

    for part_path in parts:
        part_name = os.path.basename(part_path)
        if not os.path.exists(part_path) or os.path.getsize(part_path) < 1024:
            console.print(f"  [yellow]⚠ Überspringe leere/fehlende DB: {part_name}[/yellow]")
            continue
        try:
            src_con = sqlite3.connect(part_path)
            src_con.row_factory = sqlite3.Row

            ins_total = upd_total = 0
            for table in TABLES:
                ins, upd = merge_table(dst_con, src_con, table)
                ins_total += ins
                upd_total += upd

            src_con.close()
            total_ins += ins_total
            total_upd += upd_total
            console.print(
                f"  [green]✓[/green] {part_name:<30}  "
                f"[green]+{ins_total:>5}[/green]  [yellow]~{upd_total:>5}[/yellow]"
            )
        except Exception as e:
            console.print(f"  [red]❌ {part_name}: {e}[/red]")
            failed.append(part_name)

    dst_con.close()

    # ── Abschluss-Stats ───────────────────────────────────────────────────────
    dst_con2 = sqlite3.connect(args.output)
    after    = get_db_stats(dst_con2)
    dst_con2.close()

    console.print()
    console.rule("[bold]Ergebnis[/bold]")
    console.print()

    size_mb = os.path.getsize(args.output) / 1024 / 1024

    for table in TABLES:
        b = before.get(table, {})
        a = after.get(table, {})
        delta_rows    = a.get("rows", 0)    - b.get("rows", 0)
        delta_tickers = a.get("tickers", 0) - b.get("tickers", 0)
        sign = "+" if delta_rows >= 0 else ""
        console.print(
            f"  [bold]{table:<15}[/bold]  "
            f"{a.get('rows', 0):>7} Zeilen  "
            f"({sign}{delta_rows:>5})  |  "
            f"{a.get('tickers', 0):>5} Ticker  "
            f"({'+' if delta_tickers >= 0 else ''}{delta_tickers})"
        )

    console.print()
    console.print(
        f"  [bold]Gesamt:[/bold]  "
        f"[green]+{total_ins} eingefügt[/green]  "
        f"[yellow]~{total_upd} aktualisiert[/yellow]  |  "
        f"DB-Größe: {size_mb:.1f} MB"
    )

    if failed:
        console.print(f"\n  [red]Fehler bei:[/red] {', '.join(failed)}")

    console.print()


if __name__ == "__main__":
    main()
