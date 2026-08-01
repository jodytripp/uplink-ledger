#!/usr/bin/env python3
"""Idempotently import a Uplink Ledger CSV mirror into PostgreSQL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from isp_loss_monitor import CsvStore, PostgresStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import historical Uplink Ledger CSV rows into PostgreSQL. "
            "Existing intervals are updated, so the command is safe to rerun."
        )
    )
    parser.add_argument("--csv", required=True, type=Path, help="source CSV file")
    parser.add_argument(
        "--postgres-url",
        default="postgresql:///isp_loss_monitor",
        help="PostgreSQL URI using local peer auth by default",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="rows per transaction (default: 200)",
    )
    parser.add_argument(
        "--psql-command",
        default="psql",
        help="psql executable (default: psql)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    csv_path = args.csv.expanduser().resolve()
    if not csv_path.is_file():
        parser.error(f"CSV file was not found: {csv_path}")
    if args.batch_size < 1 or args.batch_size > 5000:
        parser.error("--batch-size must be between 1 and 5000")

    store = PostgresStore(args.postgres_url, args.psql_command)
    csv_store = CsvStore(csv_path)
    try:
        csv_store.ensure()
        store.ensure()
        imported = store.import_records(
            csv_store.iter_records(),
            batch_size=args.batch_size,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Imported or updated {imported} interval(s) from {csv_path} "
        f"into {args.postgres_url}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
