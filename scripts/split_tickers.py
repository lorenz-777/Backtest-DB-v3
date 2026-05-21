#!/usr/bin/env python3
"""
scripts/split_tickers.py
=========================
Teilt tickers.txt (oder eine komma-getrennte Liste) in N gleichgroße
Batch-Dateien auf und gibt die GitHub-Actions-Matrix als JSON aus.

Ausgabe:
    batch_inputs/batch_0.txt  …  batch_inputs/batch_N-1.txt
    GitHub Actions Output:
        matrix   = [0, 1, 2, ..., N-1]   (als JSON-String)
        num_batches = N
"""

import argparse
import json
import math
import os
import sys


def load_tickers(path_or_csv: str) -> list[str]:
    """
    Akzeptiert:
      - Pfad zu einer tickers.txt   (TICKER oder TICKER:EXCHANGE pro Zeile)
      - Komma-getrennte Ticker-Liste  (z.B. "AAPL,TSLA,MSFT")
    """
    if "," in path_or_csv or (not os.path.exists(path_or_csv) and path_or_csv.strip()):
        # Komma-getrennte Inline-Liste
        return [t.strip().upper() for t in path_or_csv.split(",") if t.strip()]

    lines: list[str] = []
    try:
        with open(path_or_csv, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line.upper())
    except FileNotFoundError:
        print(f"ERROR: tickers file not found: {path_or_csv}", file=sys.stderr)
        sys.exit(1)
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers",     default="tickers.txt",
                    help="Pfad zu tickers.txt ODER komma-getrennte Ticker-Liste")
    ap.add_argument("--num-batches", type=int, default=20,
                    help="Anzahl Batches (= parallele Jobs)")
    ap.add_argument("--output-dir",  default="batch_inputs",
                    help="Ausgabe-Verzeichnis für Batch-Dateien")
    args = ap.parse_args()

    tickers = load_tickers(args.tickers)
    if not tickers:
        print("ERROR: keine Ticker gefunden", file=sys.stderr)
        sys.exit(1)

    # Leere Batches vermeiden: max. len(tickers) Batches
    num_batches = min(args.num_batches, len(tickers))
    batch_size  = math.ceil(len(tickers) / num_batches)

    os.makedirs(args.output_dir, exist_ok=True)

    batch_ids: list[int] = []
    for i in range(num_batches):
        chunk = tickers[i * batch_size : (i + 1) * batch_size]
        if not chunk:
            break
        path = os.path.join(args.output_dir, f"batch_{i}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        batch_ids.append(i)
        print(f"  Batch {i:3d}: {len(chunk):4d} Ticker  →  {path}")

    print(f"\n✓ {len(batch_ids)} Batches  |  {len(tickers)} Ticker  |  ~{batch_size}/Batch")

    # GitHub Actions Outputs setzen
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"matrix={json.dumps(batch_ids)}\n")
            f.write(f"num_batches={len(batch_ids)}\n")
    else:
        # Lokaler Test
        print(f"\nmatrix={json.dumps(batch_ids)}")
        print(f"num_batches={len(batch_ids)}")


if __name__ == "__main__":
    main()
