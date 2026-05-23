#!/usr/bin/env python3
"""
scripts/find_missing.py
========================
Findet Ticker die:
  1. Komplett fehlen in data.db
  2. Vorhanden aber mit leeren Pflichtfeldern (unvollständig)

Ausgabe: missing_tickers.txt
"""

import argparse
import os
import sqlite3


def load_tickers(path: str) -> list[str]:
    tickers = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if ":" in line:
                    tickers.append(line.split(":")[0].strip().upper())
                else:
                    tickers.append(line.upper())
    return tickers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="tickers.txt")
    ap.add_argument("--db",      default="data.db")
    ap.add_argument("--output",  default="missing_tickers.txt")
    args = ap.parse_args()

    all_tickers = load_tickers(args.tickers)
    needs_retry: set[str] = set()

    if not os.path.exists(args.db):
        print(f"DB nicht gefunden: {args.db} -> alle Ticker als fehlend markiert")
        needs_retry = set(all_tickers)
    else:
        con = sqlite3.connect(args.db)

        # 1. Komplett fehlende Ticker
        for table in ["fundamentals", "growth", "earnings"]:
            try:
                rows = con.execute(f"SELECT DISTINCT ticker FROM {table}").fetchall()
                have = {r[0] for r in rows}
                missing = set(all_tickers) - have
                needs_retry.update(missing)
                print(f"{table:<15} {len(have):>5} vorhanden  |  {len(missing):>5} komplett fehlend")
            except sqlite3.OperationalError:
                needs_retry.update(all_tickers)

        print()

        # 2. Ticker mit leeren Pflichtfeldern in fundamentals
        try:
            rows = con.execute("""
                SELECT DISTINCT ticker FROM fundamentals
                WHERE trailing_eps             IS NULL
                   OR total_revenue            IS NULL
                   OR net_income_to_common     IS NULL
                   OR total_debt               IS NULL
                   OR total_stockholder_equity IS NULL
            """).fetchall()
            incomplete = {r[0] for r in rows}
            needs_retry.update(incomplete)
            print(f"fundamentals    {len(incomplete):>5} Ticker mit leeren Feldern")
        except sqlite3.OperationalError:
            pass

        # 3. Ticker ohne Growth-Daten
        try:
            rows = con.execute("""
                SELECT DISTINCT ticker FROM growth
                WHERE earningsGrowth IS NULL
                  AND revenueGrowth  IS NULL
            """).fetchall()
            incomplete_g = {r[0] for r in rows}
            needs_retry.update(incomplete_g)
            print(f"growth          {len(incomplete_g):>5} Ticker ohne jegliche Growth-Daten")
        except sqlite3.OperationalError:
            pass

        con.close()

    # Nur Ticker die auch in tickers.txt sind
    needs_retry = needs_retry & set(all_tickers)
    sorted_retry = sorted(needs_retry)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted_retry) + "\n")

    print(f"\n{'='*50}")
    print(f"Gesamt Ticker in tickers.txt: {len(all_tickers)}")
    print(f"Retry nötig:                  {len(sorted_retry)}")
    print(f"Ausgabe:                      {args.output}")

    # GitHub Actions Output
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"missing_count={len(sorted_retry)}\n")
            f.write(f"has_missing={'true' if sorted_retry else 'false'}\n")


if __name__ == "__main__":
    main()
