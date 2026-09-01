#!/usr/bin/env python3
"""
PPT Master - Icon Search

Search the deterministic packed icon manifest without extracting archives.
The command prints one stable JSON array to stdout.

Usage:
    python3 scripts/icon_search.py QUERY [--library LIB] [--limit N]

Examples:
    python3 scripts/icon_search.py chart --library tabler-outline --limit 8
    python3 scripts/icon_search.py github --library simple-icons

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from console_encoding import configure_utf8_stdio
from icon_store import (
    ICON_LIBRARIES,
    IconIntegrityError,
    IconStore,
    IconStoreIOError,
)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the stable packed-icon search interface."""
    parser = argparse.ArgumentParser(
        description="Search the bundled PPT Master icon manifest and print JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", help="Icon basename or drawable-object query")
    parser.add_argument(
        "--library",
        choices=ICON_LIBRARIES,
        help="Restrict results to one bundled library",
    )
    parser.add_argument(
        "--limit",
        type=_positive_limit,
        default=20,
        help="Maximum number of results (1-100; default: 20)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Search one process-local store and emit the fixed JSON result."""
    try:
        configure_utf8_stdio()
    except SystemExit as exc:
        if exc.code == 78:
            print("[ERROR] icon store integrity failure: attribution guard failed", file=sys.stderr)
            return 3
        raise
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.query.strip():
        parser.error("query must not be empty")

    try:
        store = IconStore()
        results = store.search(
            args.query,
            library=args.library,
            limit=args.limit,
        )
    except IconIntegrityError as exc:
        print(f"[ERROR] icon store integrity failure: {exc}", file=sys.stderr)
        return 3
    except (IconStoreIOError, OSError) as exc:
        print(f"[ERROR] icon store I/O failure: {exc}", file=sys.stderr)
        return 4

    print(
        json.dumps(
            results,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
