#!/usr/bin/env python3
"""
scripts/merge_dbs.py
=====================
Führt alle Teil-Datenbanken (db_batch_*.db) in eine einzige data.db zusammen.

Strategie: INSERT OR REPLACE
  – Da das Schema nun vereinfacht wurde (ohlcv & fundamentals) und keinen
    scraped_at Zeitstempel mehr besitzt, werden neuere Daten einfach stumpf 
    übergeschrieben (REPLACE).

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

try:
    from rich.console import Console
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    class _FallbackConsole:
        def print(self, *a, **k): print(*a)
        def rule(self, *a, **k): print("-" * 60)
    console = _FallbackConsole()


TABLES = {
    "fundamentals": {
        "unique_cols": ["ticker", "date"],
        "all_cols": [
            "ticker", "date", "revenue", "eps", "debt_to_equity", "profit_margin"
        ],
    },
    "ohlcv": {
        "unique_cols": ["ticker", "date"],
        "all_cols": [
            "ticker", "date", "open", "high", "low", "close", "volume"
        ],
    },
}


def ensure_schema(con: sqlite3.Connection) -> None:
    """Erstellt Tabellen falls sie noch nicht existieren."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (ticker, date)
        );

        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker TEXT,
            date TEXT,
            revenue REAL,
            eps REAL,
            debt_to_equity REAL,
            profit_margin REAL,
            PRIMARY KEY (ticker, date)
        );
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
    Mergt eine Tabelle aus src in dst via INSERT OR REPLACE.
    """
    cfg      = TABLES[table]
    cols     = cfg["all_cols"]
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))

    try:
        rows = src.execute(
            f"SELECT {col_list} FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0, 0   # Tabelle existiert nicht in src

    if not rows:
        return 0, 0

    inserted = len(rows)
    
    # Da wir INSERT OR REPLACE verwenden, überschreibt er automatisch bei UNIQUE-Konflikt
    dst.executemany(
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
        rows,
    )
    dst.commit()

    return inserted, 0  # Wir können leider nicht genau sagen wie viele upgedated wurden bei OR REPLACE


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge partial SQLite DBs into one")
    ap.add_argument("--parts-dir", required=True,  help="Verzeichnis mit Teil-DBs")
    ap.add_argument("--output",    required=True,  help="Ziel-DB")
    ap.add_argument("--base",      default="",
                    help="Bestehende Basis-DB die als Ausgangspunkt dient (optional)")
    ap.add_argument("--pattern",   default="*.db",
                    help="Datei-Glob für Teil-DBs (Standard: *.db)")
    args = ap.parse_args()

    parts = sorted(glob.glob(os.path.join(args.parts_dir, args.pattern)))
    if not parts:
        console.print(f"[yellow]⚠ Keine Teil-DBs gefunden in {args.parts_dir}[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold cyan]Merge[/bold cyan]  {len(parts)} Teil-DBs  →  {args.output}\n")

    if args.base and os.path.exists(args.base) and args.base != args.output:
        shutil.copy2(args.base, args.output)
        console.print(f"  [dim]Basis: {args.base}[/dim]")

    dst_con = sqlite3.connect(args.output)
    dst_con.execute("PRAGMA journal_mode=WAL")
    dst_con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(dst_con)

    before = get_db_stats(dst_con)

    total_ins = 0
    failed: list[str] = []

    for part_path in parts:
        part_name = os.path.basename(part_path)
        if not os.path.exists(part_path) or os.path.getsize(part_path) < 1024:
            console.print(f"  [yellow]⚠ Überspringe leere/fehlende DB: {part_name}[/yellow]")
            continue
        try:
            src_con = sqlite3.connect(part_path)
            src_con.row_factory = sqlite3.Row

            ins_total = 0
            for table in TABLES:
                ins, _ = merge_table(dst_con, src_con, table)
                ins_total += ins

            src_con.close()
            total_ins += ins_total
            console.print(
                f"  [green]✓[/green] {part_name:<30}  "
                f"[green]+{ins_total:>5} Rows merged[/green]"
            )
        except Exception as e:
            console.print(f"  [red]❌ {part_name}: {e}[/red]")
            failed.append(part_name)

    dst_con.close()

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
        delta_rows = a.get("rows", 0) - b.get("rows", 0)
        sign = "+" if delta_rows >= 0 else ""
        console.print(
            f"  [bold]{table:<15}[/bold]  "
            f"{a.get('rows', 0):>7} Zeilen  "
            f"({sign}{delta_rows:>5})"
        )

    console.print()
    console.print(
        f"  [bold]Gesamt:[/bold]  "
        f"[green]ca. {total_ins} Operationen verarbeitet[/green]  |  "
        f"DB-Größe: {size_mb:.1f} MB"
    )

    if failed:
        console.print(f"\n  [red]Fehler bei:[/red] {', '.join(failed)}")

    console.print()


if __name__ == "__main__":
    main()
