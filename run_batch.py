#!/usr/bin/env python3
"""
scripts/run_batch.py
=====================
Batch-Worker mit Timeout pro Ticker.
Angepasst auf die neue test_backtest.db Struktur (nur Fundamentals).
"""

import argparse
import signal
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.panel import Panel
from db import DB

console = Console()

# Maximale Zeit pro Ticker in Sekunden (5 Minuten)
TICKER_TIMEOUT = 300


class TickerTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise TickerTimeout("Ticker-Timeout")


def load_batch(path: str) -> list[tuple[str, str]]:
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


def run_with_timeout(fn, *args, timeout=TICKER_TIMEOUT, **kwargs):
    """Führt fn aus, bricht nach timeout Sekunden ab."""
    # signal.alarm funktioniert nur auf Unix (Linux/Mac) — GitHub Actions = Linux ✓
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout)
    try:
        result = fn(*args, **kwargs)
        signal.alarm(0)
        return result
    except TickerTimeout:
        signal.alarm(0)
        raise
    finally:
        signal.signal(signal.SIGALRM, old_handler)
        signal.alarm(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-file", required=True)
    ap.add_argument("--db",         required=True)
    ap.add_argument("--delay",      type=float, default=2.0)
    ap.add_argument("--timeout",    type=int,   default=TICKER_TIMEOUT,
                    help=f"Max. Sekunden pro Ticker (Standard: {TICKER_TIMEOUT})")
    ap.add_argument("--debug",      action="store_true")
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
        f"[dim]Delay: {args.delay}s  |  Timeout/Ticker: {args.timeout}s[/dim]",
        border_style="cyan", expand=False,
    ))
    console.print()

    try:
        from fundamentals import process_ticker as fund_process
    except ImportError as e:
        console.print(f"[red]❌ Import-Fehler: {e}[/red]")
        sys.exit(1)

    db = DB(args.db)

    stats = {
        "fund_ins": 0, "fund_upd": 0, "fund_fail": [],
        "timeout":  [],
    }

    for i, (ticker, exchange) in enumerate(tickers, 1):
        console.rule(f"[bold cyan]{i}/{len(tickers)}  {ticker}[/bold cyan]")
        console.print()

        ticker_start = time.time()

        # ── Fundamentals ──────────────────────────────────────────────────────
        try:
            records = run_with_timeout(
                fund_process, ticker, exchange,
                debug=args.debug, timeout=args.timeout
            )
            if records:
                ins, upd = db.upsert_fundamentals(records)
                stats["fund_ins"] += ins
                stats["fund_upd"] += upd
                console.print(f"  [green]✓ Fundamentals[/green]  [dim]+{ins} ~{upd}[/dim]")
            else:
                stats["fund_fail"].append(ticker)
                console.print(f"  [yellow]⚠ Fundamentals: keine Daten[/yellow]")
        except TickerTimeout:
            stats["fund_fail"].append(ticker)
            stats["timeout"].append(f"{ticker}/fundamentals")
            console.print(f"  [red]⏱ Fundamentals: Timeout nach {args.timeout}s[/red]")
        except Exception as e:
            stats["fund_fail"].append(ticker)
            console.print(f"  [red]❌ Fundamentals: {e}[/red]")

        elapsed = round(time.time() - ticker_start, 1)
        console.print(f"  [dim]⏱ {elapsed}s[/dim]")
        console.print()

        if i < len(tickers):
            time.sleep(args.delay)

    db.close()

    # ── Abschlussstatus ───────────────────────────────────────────────────────
    console.rule("[bold]Batch fertig[/bold]")
    console.print()

    timeout_count = len(stats["timeout"])
    lines = (
        f"[bold]{batch_name}[/bold]  –  {len(tickers)} Ticker\n\n"
        f"[bold]Fundamentals:[/bold] [green]+{stats['fund_ins']}[/green]  [yellow]~{stats['fund_upd']}[/yellow]"
        + (f"  [red]Fehler: {len(stats['fund_fail'])}[/red]" if stats["fund_fail"] else "")
        + (f"\n[red]Timeouts: {timeout_count}[/red]" if timeout_count else "")
    )
    console.print(Panel(lines, title="📦 Batch-Ergebnis", border_style="bright_black"))
    console.print()

    sys.exit(0)


if __name__ == "__main__":
    main()