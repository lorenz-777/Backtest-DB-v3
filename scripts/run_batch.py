#!/usr/bin/env python3
"""
scripts/run_batch.py
=====================
Führt den kompletten Scraping-Prozess (Earnings + Fundamentals + Growth)
für EINEN Batch von Tickern durch und speichert das Ergebnis in einer
eigenen SQLite-Datenbank.

Dieses Script ist der Worker, der in jedem GitHub-Actions-Matrix-Job
gestartet wird.

Verwendung:
    python scripts/run_batch.py \
        --batch-file batch_inputs/batch_3.txt \
        --db         db_batch_3.db \
        --delay      2.0
"""

import argparse
import sys
import time
import os

# Projektverzeichnis zum Python-Pfad hinzufügen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.panel import Panel

from db import DB

console = Console()


def load_batch(path: str) -> list[tuple[str, str]]:
    """Liest eine Batch-Datei: TICKER oder TICKER:EXCHANGE pro Zeile."""
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
        console.print(f"[red]❌ Batch-Datei nicht gefunden: {path}[/red]")
        sys.exit(1)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch-Worker: scrapet einen Ticker-Block")
    ap.add_argument("--batch-file", required=True,  help="Pfad zur Batch-Ticker-Datei")
    ap.add_argument("--db",         required=True,  help="Ausgabe-DB-Pfad")
    ap.add_argument("--delay",      type=float, default=2.0,
                    help="Pause zwischen Tickern in Sekunden (Standard: 2.0)")
    ap.add_argument("--debug",      action="store_true")
    ap.add_argument("--skip-earnings",     action="store_true")
    ap.add_argument("--skip-fundamentals", action="store_true")
    ap.add_argument("--skip-growth",       action="store_true")
    args = ap.parse_args()

    tickers = load_batch(args.batch_file)
    if not tickers:
        console.print(f"[yellow]⚠ Keine Ticker in {args.batch_file}[/yellow]")
        sys.exit(0)

    batch_name = os.path.basename(args.batch_file).replace(".txt", "")

    console.print()
    console.print(Panel(
        f"[bold cyan]Batch Worker[/bold cyan]  –  {batch_name}\n"
        f"[dim]{len(tickers)} Ticker  →  {args.db}[/dim]\n"
        f"[dim]Delay: {args.delay}s  |  Debug: {args.debug}[/dim]",
        border_style="cyan", expand=False,
    ))
    console.print()

    # ── Scraper importieren ───────────────────────────────────────────────────
    try:
        from earnings     import process_ticker as earn_process
        from fundamentals import process_ticker as fund_process
        from growth       import process_ticker as growth_process
    except ImportError as e:
        console.print(f"[red]❌ Import-Fehler: {e}[/red]")
        sys.exit(1)

    db = DB(args.db)

    stats = {
        "earn_ins":  0, "earn_upd":  0, "earn_fail":  [],
        "fund_ins":  0, "fund_upd":  0, "fund_fail":  [],
        "grow_ins":  0, "grow_upd":  0, "grow_fail":  [],
    }

    for i, (ticker, exchange) in enumerate(tickers, 1):
        console.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        console.print()

        # ── Earnings ──────────────────────────────────────────────────────────
        if not args.skip_earnings:
            try:
                data = earn_process(ticker, exchange, debug=args.debug)
                if data:
                    ins, upd = db.upsert_earnings(ticker, data)
                    stats["earn_ins"] += ins
                    stats["earn_upd"] += upd
                    console.print(f"  [green]✓ Earnings[/green]  [dim]+{ins} ~{upd}[/dim]")
                else:
                    stats["earn_fail"].append(ticker)
                    console.print(f"  [yellow]⚠ Earnings: keine Daten[/yellow]")
            except Exception as e:
                stats["earn_fail"].append(ticker)
                console.print(f"  [red]❌ Earnings: {e}[/red]")
            time.sleep(1.0)

        # ── Fundamentals ──────────────────────────────────────────────────────
        if not args.skip_fundamentals:
            try:
                records = fund_process(ticker, exchange, debug=args.debug)
                if records:
                    ins, upd = db.upsert_fundamentals(records)
                    stats["fund_ins"] += ins
                    stats["fund_upd"] += upd
                    console.print(f"  [green]✓ Fundamentals[/green]  [dim]+{ins} ~{upd}[/dim]")
                else:
                    stats["fund_fail"].append(ticker)
                    console.print(f"  [yellow]⚠ Fundamentals: keine Daten[/yellow]")
            except Exception as e:
                stats["fund_fail"].append(ticker)
                console.print(f"  [red]❌ Fundamentals: {e}[/red]")
            time.sleep(1.0)

        # ── Growth ────────────────────────────────────────────────────────────
        if not args.skip_growth:
            try:
                records = growth_process(ticker, db, exchange=exchange, debug=args.debug)
                if records:
                    ins, upd = db.upsert_growth(records)
                    stats["grow_ins"] += ins
                    stats["grow_upd"] += upd
                    console.print(f"  [green]✓ Growth[/green]  [dim]+{ins} ~{upd}[/dim]")
                else:
                    stats["grow_fail"].append(ticker)
                    console.print(f"  [yellow]⚠ Growth: keine Daten[/yellow]")
            except Exception as e:
                stats["grow_fail"].append(ticker)
                console.print(f"  [red]❌ Growth: {e}[/red]")

        console.print()

        # Delay zwischen Tickern (letzter Ticker bekommt keinen Delay)
        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Abschlussstatus ───────────────────────────────────────────────────────
    console.rule("[bold]Batch fertig[/bold]")
    console.print()

    total_ok = len(tickers) - max(
        len(stats["earn_fail"]),
        len(stats["fund_fail"]),
        len(stats["grow_fail"]),
    )

    lines = (
        f"[bold]{batch_name}[/bold]  –  {len(tickers)} Ticker\n\n"
        f"[bold]Earnings:[/bold]     [green]+{stats['earn_ins']}[/green]  [yellow]~{stats['earn_upd']}[/yellow]"
        + (f"  [red]Fehler: {len(stats['earn_fail'])}[/red]" if stats["earn_fail"] else "") + "\n"
        f"[bold]Fundamentals:[/bold] [green]+{stats['fund_ins']}[/green]  [yellow]~{stats['fund_upd']}[/yellow]"
        + (f"  [red]Fehler: {len(stats['fund_fail'])}[/red]" if stats["fund_fail"] else "") + "\n"
        f"[bold]Growth:[/bold]       [green]+{stats['grow_ins']}[/green]  [yellow]~{stats['grow_upd']}[/yellow]"
        + (f"  [red]Fehler: {len(stats['grow_fail'])}[/red]" if stats["grow_fail"] else "")
    )

    console.print(Panel(lines, title="📦 Batch-Ergebnis", border_style="bright_black"))
    console.print()

    # Exit-Code: 0 = OK, 1 = alle Ticker fehlgeschlagen
    all_failed = (
        len(stats["earn_fail"]) == len(tickers) and
        len(stats["fund_fail"]) == len(tickers)
    )
    sys.exit(1 if all_failed else 0)


if __name__ == "__main__":
    main()
