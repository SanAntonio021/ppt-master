#!/usr/bin/env python3
"""
PPT Master - Icon Sync

Copy chosen library icons into `<project>/icons/<lib>/` when selected. Missing
names exit non-zero before export. Known basenames need no separate existence
check; search the chosen library only for unresolved concepts.

Project-local custom icons count as satisfied. In one resource-selection batch,
`simple-icons` may accompany one of the four stylistic libraries for
real brand marks.

Usage:
    python3 scripts/icon_sync.py <project_path> <lib/name> [<lib/name> ...]

Examples:
    python3 scripts/icon_sync.py projects/deck tabler-outline/home tabler-outline/chart
    python3 scripts/icon_sync.py projects/deck tabler-outline/home simple-icons/github

Dependencies:
    None (standard library only).

See references/executor-base.md §4 and templates/icons/README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from console_encoding import configure_utf8_stdio
from icon_store import IconIntegrityError, IconStore, IconStoreIOError

configure_utf8_stdio()

_STYLISTIC_LIBRARIES = {
    "chunk-filled",
    "phosphor-duotone",
    "tabler-filled",
    "tabler-outline",
}
_SYNC_LIBRARIES = _STYLISTIC_LIBRARIES | {"simple-icons"}
_ICON_SEARCH_SCRIPT = Path(__file__).resolve().parent / "icon_search.py"


def _split_name(icon_name: str) -> tuple[str, str]:
    """Validate and split one complete bundled ``library/name`` id."""
    if icon_name.count("/") != 1:
        raise ValueError(
            f"icon id must use the complete library/name form: {icon_name!r}"
        )
    lib, name = icon_name.split("/", 1)
    if lib not in _SYNC_LIBRARIES:
        raise ValueError(f"unsupported bundled icon library: {lib!r}")
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).name != name
    ):
        raise ValueError(f"invalid icon name: {name!r}")
    return lib, name


def sync_icons(
    project_path: Path,
    icon_names: list[str],
    *,
    store: IconStore | None = None,
) -> tuple[list[str], list[str]]:
    """Copy each `lib/name` from the global library into `<project>/icons/`.

    Returns (copied, missing). A name already present in the project (e.g. a
    custom icon) counts as satisfied, not missing.
    """
    active_store = store or IconStore()
    project_icons = project_path / "icons"
    copied: list[str] = []
    missing: list[str] = []
    parsed = [(*_split_name(raw), raw) for raw in icon_names]
    payloads = active_store.read_icons([raw for _lib, _name, raw in parsed])

    for lib, name, raw in parsed:
        dst = project_icons / lib / f"{name}.svg"
        payload = payloads.get(raw)
        if payload is not None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(payload)
            copied.append(f"{lib}/{name}")
        elif dst.is_file():
            copied.append(f"{lib}/{name} (already in project)")
        else:
            missing.append(f"{lib}/{name}")

    return copied, missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy chosen library icons into a project's icons/ folder; report missing ones.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project_path", help="Project directory")
    parser.add_argument("icons", nargs="+", help="Icon names to copy, e.g. chunk-filled/home")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    project = Path(args.project_path)
    if not project.is_dir():
        print(f"[ERROR] project not found: {project}", file=sys.stderr)
        return 1

    try:
        requested_libraries = {_split_name(raw)[0] for raw in args.icons}
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    stylistic_libraries = sorted(requested_libraries & _STYLISTIC_LIBRARIES)
    if len(stylistic_libraries) > 1:
        print(
            f"[ERROR] mixed stylistic icon libraries: {', '.join(stylistic_libraries)}",
            file=sys.stderr,
        )
        print(
            "Choose one of the four stylistic libraries per selection batch; "
            "simple-icons may coexist for real brand marks.",
            file=sys.stderr,
        )
        return 1

    try:
        store = IconStore()
        copied, missing = sync_icons(project, args.icons, store=store)
    except IconIntegrityError as exc:
        print(f"[ERROR] icon store integrity failure: {exc}", file=sys.stderr)
        return 1
    except (IconStoreIOError, OSError) as exc:
        print(f"[ERROR] icon store I/O failure: {exc}", file=sys.stderr)
        return 1

    if copied:
        print(f"[OK] {len(copied)} icon(s) in {project / 'icons'}:", file=sys.stderr)
        for c in copied:
            print(f"     + {c}", file=sys.stderr)

    if missing:
        print(f"\n[MISSING] {len(missing)} icon(s) not in the library — re-pick before continuing:", file=sys.stderr)
        for m in missing:
            lib = m.split("/", 1)[0]
            print(
                f'     ✗ {m}   (search: python3 "{_ICON_SEARCH_SCRIPT}" "<keyword>" --library {lib})',
                file=sys.stderr,
            )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
