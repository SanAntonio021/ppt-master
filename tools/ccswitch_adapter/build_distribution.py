#!/usr/bin/env python3
"""
PPT Master - CC Switch Distribution Builder

Rebuild the CC Switch branch from one exact, complete official checkout and a
small adapter allowlist. Icon SVG bytes are packed deterministically without
compression; both runtime manifests are regenerated from raw bytes.

Usage:
    python tools/ccswitch_adapter/build_distribution.py \
        --upstream-root <official_checkout> \
        --adapter-root <fork_checkout> \
        --output <new_directory> [--json-out <report.json>]

Examples:
    python tools/ccswitch_adapter/build_distribution.py \
        --upstream-root ../ppt-master-v6.3.0-upstream \
        --adapter-root . --output ../ppt-master-build-1

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional


UPSTREAM_REPOSITORY = "https://github.com/hugohe3/ppt-master"
UPSTREAM_VERSION = "6.3.0"
UPSTREAM_COMMIT = "a4f5487dc930ba22f7002d775f49c81f47210960"
ICON_LIBRARIES = (
    "chunk-filled",
    "phosphor-duotone",
    "simple-icons",
    "tabler-filled",
    "tabler-outline",
)
EXPECTED_LIBRARY_COUNTS = {
    "chunk-filled": 641,
    "phosphor-duotone": 1518,
    "simple-icons": 3675,
    "tabler-filled": 1055,
    "tabler-outline": 5138,
}
TARGET_SHARD_BYTES = 16 * 1024 * 1024
HARD_SHARD_BYTES = 20 * 1024 * 1024
TOTAL_RESOURCE_BYTES_LIMIT = 64 * 1024 * 1024
MAX_ICON_BYTES = 4 * 1024 * 1024
_SKILL_RELATIVE = Path("skills") / "ppt-master"
_ICON_RELATIVE = _SKILL_RELATIVE / "templates" / "icons"
_DISTRIBUTION_MANIFEST = _SKILL_RELATIVE / "distribution.manifest.json"
_ADAPTER_LIST = Path("tools") / "ccswitch_adapter" / "adapter-files.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_WINDOWS_RESERVED_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z",
    re.IGNORECASE,
)
_IGNORED_DIRS = {".git", "__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class BuildError(RuntimeError):
    """The source or generated distribution violated the release contract."""


@dataclass(frozen=True)
class InventoryFile:
    """Record one raw regular file in a deterministic inventory."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class IconSource:
    """Record one official SVG before it is written into a shard."""

    library: str
    name: str
    path: str
    source_path: Path
    size: int
    sha256: str


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildError("path must be a non-empty string")
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.startswith("//")
        or _DRIVE_RE.match(value)
        or PurePosixPath(value).is_absolute()
    ):
        raise BuildError(f"unsafe path: {value!r}")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise BuildError(f"unsafe path: {value!r}")
    for part in parts:
        if (
            ":" in part
            or part.endswith(".")
            or part.endswith(" ")
            or _WINDOWS_RESERVED_RE.fullmatch(part)
        ):
            raise BuildError(f"unsafe Windows path segment: {value!r}")
    return value


def _iter_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    casefold_paths: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        safe_dirnames = []
        for dirname in sorted(dirnames):
            path = directory_path / dirname
            relative = path.relative_to(root).as_posix()
            if dirname in _IGNORED_DIRS:
                continue
            if path.is_symlink():
                raise BuildError(f"symbolic link is not allowed: {relative}")
            _validate_relative_path(relative)
            safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames
        for filename in sorted(filenames):
            path = directory_path / filename
            relative = path.relative_to(root).as_posix()
            if PurePosixPath(relative).suffix.casefold() in _IGNORED_SUFFIXES:
                continue
            _validate_relative_path(relative)
            if path.is_symlink():
                raise BuildError(f"symbolic link is not allowed: {relative}")
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise BuildError(f"non-regular file is not allowed: {relative}")
            folded = relative.casefold()
            if folded in casefold_paths:
                raise BuildError(f"case-insensitive path collision: {relative}")
            casefold_paths.add(folded)
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _build_inventory(root: Path) -> tuple[list[InventoryFile], str]:
    entries: list[InventoryFile] = []
    inventory_digest = hashlib.sha256()
    for path in _iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        digest = _sha256_file(path)
        entries.append(InventoryFile(relative, size, digest))
        inventory_digest.update(relative.encode("utf-8"))
        inventory_digest.update(b"\0")
        inventory_digest.update(str(size).encode("ascii"))
        inventory_digest.update(b"\0")
        inventory_digest.update(digest.encode("ascii"))
        inventory_digest.update(b"\n")
    return entries, inventory_digest.hexdigest()


def _run_git(root: Path, *args: str, allow_failure: bool = False) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 and not allow_failure:
        raise BuildError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _validate_upstream_checkout(root: Path, inventory: list[InventoryFile]) -> None:
    if not (root / ".git").exists():
        raise BuildError("upstream root must be a Git checkout, not an archive directory")
    if _run_git(root, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise BuildError("upstream checkout is not the pinned v6.3.0 commit")
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise BuildError("upstream checkout is not clean")
    tracked_output = _run_git(root, "ls-files", "-z")
    tracked = {path for path in tracked_output.split("\0") if path}
    actual = {entry.path for entry in inventory}
    if tracked != actual:
        raise BuildError("upstream checkout files do not match the Git index")
    if _run_git(root, "diff", "--name-only", "HEAD"):
        raise BuildError("upstream checkout raw files differ from HEAD")
    promisor = _run_git(
        root,
        "config",
        "--get-regexp",
        r"^remote\..*\.promisor$",
        allow_failure=True,
    )
    partial = _run_git(
        root,
        "config",
        "--get-regexp",
        r"^(extensions\.partialClone|remote\..*\.partialclonefilter)$",
        allow_failure=True,
    )
    if promisor or partial:
        raise BuildError("upstream checkout is partial or promisor-backed")
    _run_git(root, "fsck", "--full", "--no-dangling")


def _copy_official_tree(upstream_root: Path, output_root: Path) -> None:
    if output_root.exists():
        raise BuildError(f"output directory already exists: {output_root}")

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _IGNORED_DIRS or Path(name).suffix.casefold() in _IGNORED_SUFFIXES
        }

    shutil.copytree(
        upstream_root,
        output_root,
        symlinks=False,
        ignore=ignore,
        copy_function=shutil.copyfile,
    )


def _collect_icons(upstream_root: Path) -> list[IconSource]:
    icon_root = upstream_root / _ICON_RELATIVE
    icons: list[IconSource] = []
    casefold_paths: set[str] = set()
    for library in ICON_LIBRARIES:
        library_root = icon_root / library
        if not library_root.is_dir() or library_root.is_symlink():
            raise BuildError(f"missing official icon library: {library}")
        library_files = sorted(
            path
            for path in library_root.rglob("*")
            if path.is_file()
        )
        if len(library_files) != EXPECTED_LIBRARY_COUNTS[library]:
            raise BuildError(
                f"official {library} count changed: "
                f"{len(library_files)} != {EXPECTED_LIBRARY_COUNTS[library]}"
            )
        for source_path in library_files:
            if source_path.is_symlink() or source_path.suffix != ".svg":
                raise BuildError(f"official icon is not a regular SVG: {source_path}")
            relative_name = source_path.relative_to(library_root).as_posix()
            member_path = _validate_relative_path(f"{library}/{relative_name}")
            folded = member_path.casefold()
            if folded in casefold_paths:
                raise BuildError(f"icon path collision: {member_path}")
            payload = source_path.read_bytes()
            if len(payload) > MAX_ICON_BYTES:
                raise BuildError(f"icon exceeds 4 MiB: {member_path}")
            if payload.startswith(b"\xef\xbb\xbf"):
                raise BuildError(f"icon contains a UTF-8 BOM: {member_path}")
            name = PurePosixPath(relative_name).with_suffix("").as_posix()
            icons.append(
                IconSource(
                    library=library,
                    name=name,
                    path=member_path,
                    source_path=source_path,
                    size=len(payload),
                    sha256=_sha256_bytes(payload),
                )
            )
            casefold_paths.add(folded)
    if sum(icon.size for icon in icons) > TOTAL_RESOURCE_BYTES_LIMIT:
        raise BuildError("official icon bytes exceed the 64 MiB resource limit")
    return icons


def _predicted_stored_zip_size(icons: list[IconSource]) -> int:
    member_overhead = sum(
        76 + (2 * len(icon.path.encode("utf-8"))) + icon.size
        for icon in icons
    )
    return 22 + member_overhead


def _partition_library(icons: list[IconSource]) -> list[list[IconSource]]:
    shards: list[list[IconSource]] = []
    current: list[IconSource] = []
    for icon in icons:
        candidate = [*current, icon]
        if current and _predicted_stored_zip_size(candidate) > TARGET_SHARD_BYTES:
            shards.append(current)
            current = [icon]
        else:
            current = candidate
        if _predicted_stored_zip_size(current) > HARD_SHARD_BYTES:
            raise BuildError(f"one deterministic icon shard exceeds 20 MiB: {icon.path}")
    if current:
        shards.append(current)
    return shards


def _write_shard(path: Path, icons: list[IconSource]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
        strict_timestamps=True,
    ) as archive:
        for icon in icons:
            info = zipfile.ZipInfo(icon.path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, icon.source_path.read_bytes())
    if path.stat().st_size > HARD_SHARD_BYTES:
        raise BuildError(f"generated icon shard exceeds 20 MiB: {path}")


def _build_icon_packs(
    upstream_root: Path,
    output_root: Path,
) -> tuple[dict, list[dict]]:
    icons = _collect_icons(upstream_root)
    output_icon_root = output_root / _ICON_RELATIVE
    for library in ICON_LIBRARIES:
        target = output_icon_root / library
        resolved_target = target.resolve()
        try:
            resolved_target.relative_to(output_icon_root.resolve())
        except ValueError as exc:
            raise BuildError(f"icon removal target escaped output: {target}") from exc
        if not target.is_dir() or target.is_symlink():
            raise BuildError(f"copied icon library is not a normal directory: {target}")
        shutil.rmtree(target)

    icon_entries: list[dict] = []
    shard_entries: list[dict] = []
    libraries: dict[str, dict] = {}
    for library in ICON_LIBRARIES:
        library_icons = sorted(
            (icon for icon in icons if icon.library == library),
            key=lambda icon: icon.path,
        )
        shard_paths: list[str] = []
        for index, shard_icons in enumerate(_partition_library(library_icons), start=1):
            shard_relative = f"packs/{library}-{index:04d}.zip"
            shard_path = output_icon_root.joinpath(*shard_relative.split("/"))
            _write_shard(shard_path, shard_icons)
            shard_size = shard_path.stat().st_size
            shard_entries.append(
                {
                    "members": len(shard_icons),
                    "path": shard_relative,
                    "raw_bytes": sum(icon.size for icon in shard_icons),
                    "sha256": _sha256_file(shard_path),
                    "size": shard_size,
                }
            )
            shard_paths.append(shard_relative)
            for icon in shard_icons:
                icon_entries.append(
                    {
                        "id": f"{icon.library}/{icon.name}",
                        "library": icon.library,
                        "member": icon.path,
                        "name": icon.name,
                        "path": icon.path,
                        "sha256": icon.sha256,
                        "shard": shard_relative,
                        "size": icon.size,
                    }
                )
        libraries[library] = {
            "count": len(library_icons),
            "raw_bytes": sum(icon.size for icon in library_icons),
            "shards": shard_paths,
        }

    packed_bytes = sum(entry["size"] for entry in shard_entries)
    if packed_bytes > TOTAL_RESOURCE_BYTES_LIMIT:
        raise BuildError("packed icon resources exceed 64 MiB")
    notices = output_icon_root / "THIRD_PARTY_NOTICES.md"
    license_files = [
        {
            "path": "THIRD_PARTY_NOTICES.md",
            "sha256": _sha256_file(notices),
            "size": notices.stat().st_size,
        }
    ]
    icon_manifest = {
        "format": "ppt-master-icon-packs",
        "icons": sorted(icon_entries, key=lambda entry: entry["id"]),
        "libraries": libraries,
        "license_files": license_files,
        "schema_version": 1,
        "shards": sorted(shard_entries, key=lambda entry: entry["path"]),
        "storage": {
            "compression": "ZIP_STORED",
            "hard_shard_bytes": HARD_SHARD_BYTES,
            "target_shard_bytes": TARGET_SHARD_BYTES,
            "total_resource_bytes_limit": TOTAL_RESOURCE_BYTES_LIMIT,
        },
        "totals": {
            "icons": len(icon_entries),
            "packed_bytes": packed_bytes,
            "raw_bytes": sum(icon.size for icon in icons),
            "shards": len(shard_entries),
        },
        "upstream": {
            "commit": UPSTREAM_COMMIT,
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
        },
    }
    _write_json(output_icon_root / "icons.manifest.json", icon_manifest)
    return icon_manifest, shard_entries


def _load_adapter_paths(adapter_root: Path) -> list[str]:
    manifest_path = adapter_root / _ADAPTER_LIST
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read adapter allowlist: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise BuildError("adapter allowlist must contain a JSON object")
    raw_files = payload.get("files")
    if payload.get("schema_version") != 1 or not isinstance(raw_files, list):
        raise BuildError("unsupported adapter allowlist")
    files: list[str] = []
    casefold_paths: set[str] = set()
    generated = {
        _DISTRIBUTION_MANIFEST.as_posix(),
        (_ICON_RELATIVE / "icons.manifest.json").as_posix(),
    }
    for raw in raw_files:
        relative = _validate_relative_path(raw)
        folded = relative.casefold()
        if relative in generated or relative.startswith(f"{_ICON_RELATIVE.as_posix()}/packs/"):
            raise BuildError(f"generated path must not be an adapter input: {relative}")
        if folded in casefold_paths:
            raise BuildError(f"duplicate adapter path: {relative}")
        source = adapter_root.joinpath(*relative.split("/"))
        if not source.is_file() or source.is_symlink():
            raise BuildError(f"adapter file is missing or unsafe: {relative}")
        files.append(relative)
        casefold_paths.add(folded)
    if _ADAPTER_LIST.as_posix() not in files:
        raise BuildError("adapter allowlist must include itself")
    return sorted(files)


def _apply_adapter(adapter_root: Path, output_root: Path) -> list[str]:
    files = _load_adapter_paths(adapter_root)
    for relative in files:
        source = adapter_root.joinpath(*relative.split("/"))
        destination = output_root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return files


def _write_distribution_manifest(output_root: Path) -> dict:
    skill_root = output_root / _SKILL_RELATIVE
    entries: list[dict] = []
    for path in _iter_regular_files(skill_root):
        relative = path.relative_to(skill_root).as_posix()
        if relative == "distribution.manifest.json":
            continue
        entries.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    manifest = {
        "distribution": "ccswitch",
        "files": entries,
        "schema_version": 1,
        "totals": {
            "bytes": sum(entry["size"] for entry in entries),
            "files": len(entries),
        },
        "upstream": {
            "commit": UPSTREAM_COMMIT,
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
        },
    }
    _write_json(output_root / _DISTRIBUTION_MANIFEST, manifest)
    return manifest


def build_distribution(
    upstream_root: Path,
    adapter_root: Path,
    output_root: Path,
) -> dict:
    """Run the fixed inventory, pack, adapter, and manifest build sequence."""
    upstream_root = upstream_root.resolve()
    adapter_root = adapter_root.resolve()
    output_root = output_root.resolve()
    if output_root in {upstream_root, adapter_root}:
        raise BuildError("output must be separate from upstream and adapter roots")
    if output_root.is_relative_to(upstream_root) or output_root.is_relative_to(adapter_root):
        raise BuildError("output must not be nested inside an input checkout")

    upstream_inventory, upstream_inventory_digest = _build_inventory(upstream_root)
    _validate_upstream_checkout(upstream_root, upstream_inventory)
    _copy_official_tree(upstream_root, output_root)
    icon_manifest, shards = _build_icon_packs(upstream_root, output_root)
    adapter_files = _apply_adapter(adapter_root, output_root)
    distribution_manifest = _write_distribution_manifest(output_root)
    final_inventory, final_inventory_digest = _build_inventory(output_root)
    return {
        "adapter_files": len(adapter_files),
        "distribution_bytes": distribution_manifest["totals"]["bytes"],
        "distribution_files": distribution_manifest["totals"]["files"],
        "final_inventory_sha256": final_inventory_digest,
        "final_repository_files": len(final_inventory),
        "icon_count": icon_manifest["totals"]["icons"],
        "icon_packed_bytes": icon_manifest["totals"]["packed_bytes"],
        "icon_raw_bytes": icon_manifest["totals"]["raw_bytes"],
        "shards": shards,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_files": len(upstream_inventory),
        "upstream_inventory_sha256": upstream_inventory_digest,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the deterministic distribution-builder CLI."""
    parser = argparse.ArgumentParser(
        description="Build the pinned PPT Master CC Switch distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--upstream-root", required=True, help="Exact official v6.3.0 checkout")
    parser.add_argument("--adapter-root", required=True, help="Fork checkout containing adapter files")
    parser.add_argument("--output", required=True, help="New output directory; must not exist")
    parser.add_argument("--json-out", help="Optional path for the build report")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Build the distribution and print its machine-readable report."""
    args = build_parser().parse_args(argv)
    try:
        report = build_distribution(
            Path(args.upstream_root),
            Path(args.adapter_root),
            Path(args.output),
        )
        if args.json_out:
            _write_json(Path(args.json_out), report)
    except (BuildError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
