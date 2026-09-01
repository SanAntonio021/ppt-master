#!/usr/bin/env python3
"""
PPT Master - CC Switch Distribution Verifier

Verify manifests, packed icons, raw rebuild equality, archive limits, offline
runtime behavior, and negative security cases without adding a test framework.

Usage:
    python tools/ccswitch_adapter/verify_distribution.py \
        --candidate-root <fork_tree> [--rebuilt-root <rebuilt_tree>] \
        [--simulate-archive <zip>] [--archive <real_codeload.zip>] \
        [--json-out <report.json>]

Examples:
    python tools/ccswitch_adapter/verify_distribution.py \
        --candidate-root . --rebuilt-root ../ppt-master-build-1 \
        --simulate-archive ../ppt-master-simulated.zip

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from build_distribution import (
    HARD_SHARD_BYTES,
    ICON_LIBRARIES,
    TOTAL_RESOURCE_BYTES_LIMIT,
    UPSTREAM_COMMIT,
    UPSTREAM_REPOSITORY,
    UPSTREAM_VERSION,
)


MAX_ARCHIVE_MEMBERS = 2000
_SKILL_RELATIVE = Path("skills") / "ppt-master"
_DISTRIBUTION_MANIFEST = _SKILL_RELATIVE / "distribution.manifest.json"
_ICON_MANIFEST = _SKILL_RELATIVE / "templates" / "icons" / "icons.manifest.json"
_PROVENANCE = _SKILL_RELATIVE / "ccswitch.provenance.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_WINDOWS_RESERVED_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z",
    re.IGNORECASE,
)
_IGNORED_DIRS = {".git", "__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_BANNED_NETWORK_IMPORTS = {"aiohttp", "http", "httpx", "requests", "socket", "urllib"}
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class VerificationError(RuntimeError):
    """The candidate failed one deterministic acceptance gate."""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _load_json(path: Path) -> dict:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"cannot read JSON file: {path}") from exc
    if payload.startswith(b"\xef\xbb\xbf"):
        raise VerificationError(f"JSON file has a UTF-8 BOM: {path}")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"invalid JSON file: {path}") from exc
    if not isinstance(parsed, dict):
        raise VerificationError(f"JSON root must be an object: {path}")
    return parsed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError("path must be a non-empty string")
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.startswith("//")
        or _DRIVE_RE.match(value)
        or PurePosixPath(value).is_absolute()
    ):
        raise VerificationError(f"unsafe path: {value!r}")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise VerificationError(f"unsafe path: {value!r}")
    for part in parts:
        if (
            ":" in part
            or part.endswith(".")
            or part.endswith(" ")
            or _WINDOWS_RESERVED_RE.fullmatch(part)
        ):
            raise VerificationError(f"unsafe Windows path segment: {value!r}")
    return value


def _inventory(root: Path) -> dict[str, tuple[int, str]]:
    inventory: dict[str, tuple[int, str]] = {}
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
                raise VerificationError(f"symbolic link is not allowed: {relative}")
            _validate_relative_path(relative)
            safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames
        for filename in sorted(filenames):
            path = directory_path / filename
            relative = path.relative_to(root).as_posix()
            if PurePosixPath(relative).suffix.casefold() in _IGNORED_SUFFIXES:
                continue
            _validate_relative_path(relative)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise VerificationError(f"non-regular file is not allowed: {relative}")
            folded = relative.casefold()
            if relative in inventory or folded in casefold_paths:
                raise VerificationError(f"duplicate or case-colliding path: {relative}")
            inventory[relative] = (metadata.st_size, _sha256_file(path))
            casefold_paths.add(folded)
    return dict(sorted(inventory.items()))


def _verify_raw_tree(candidate_root: Path, rebuilt_root: Path) -> dict:
    candidate = _inventory(candidate_root)
    rebuilt = _inventory(rebuilt_root)
    if candidate != rebuilt:
        candidate_paths = set(candidate)
        rebuilt_paths = set(rebuilt)
        missing = sorted(candidate_paths - rebuilt_paths)
        unexpected = sorted(rebuilt_paths - candidate_paths)
        changed = sorted(
            path
            for path in candidate_paths & rebuilt_paths
            if candidate[path] != rebuilt[path]
        )
        raise VerificationError(
            "raw rebuild mismatch: "
            f"missing={missing[:5]} unexpected={unexpected[:5]} changed={changed[:5]}"
        )
    return {"bytes": sum(size for size, _digest in candidate.values()), "files": len(candidate)}


def _verify_distribution_manifest(candidate_root: Path) -> dict[str, tuple[int, str]]:
    skill_root = candidate_root / _SKILL_RELATIVE
    manifest = _load_json(candidate_root / _DISTRIBUTION_MANIFEST)
    if manifest.get("schema_version") != 1 or manifest.get("distribution") != "ccswitch":
        raise VerificationError("unsupported distribution manifest")
    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict) or upstream != {
        "commit": UPSTREAM_COMMIT,
        "repository": UPSTREAM_REPOSITORY,
        "version": UPSTREAM_VERSION,
    }:
        raise VerificationError("distribution upstream provenance mismatch")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise VerificationError("distribution file inventory is missing")

    expected: dict[str, tuple[int, str]] = {}
    casefold_paths: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise VerificationError("invalid distribution entry")
        path = _validate_relative_path(raw.get("path"))
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            path == "distribution.manifest.json"
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise VerificationError(f"invalid distribution entry: {path}")
        folded = path.casefold()
        if path in expected or folded in casefold_paths:
            raise VerificationError(f"duplicate distribution entry: {path}")
        expected[path] = (size, digest)
        casefold_paths.add(folded)

    actual = _inventory(skill_root)
    actual.pop("distribution.manifest.json", None)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        changed = sorted(
            path for path in set(actual) & set(expected) if actual[path] != expected[path]
        )
        raise VerificationError(
            "distribution manifest mismatch: "
            f"missing={missing[:5]} unexpected={unexpected[:5]} changed={changed[:5]}"
        )
    totals = manifest.get("totals")
    if not isinstance(totals, dict) or totals != {
        "bytes": sum(size for size, _digest in expected.values()),
        "files": len(expected),
    }:
        raise VerificationError("distribution totals mismatch")
    return expected


def _validate_zip_infos(archive: zipfile.ZipFile, *, expected: set[str] | None = None) -> None:
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    total_raw = 0
    for info in archive.infolist():
        path = _validate_relative_path(info.filename)
        mode = (info.external_attr >> 16) & 0xF000
        if (
            info.is_dir()
            or info.compress_type != zipfile.ZIP_STORED
            or info.file_size != info.compress_size
            or info.flag_bits & 0x1
            or mode == stat.S_IFLNK
        ):
            raise VerificationError(f"unsafe or compressed icon member: {path}")
        folded = path.casefold()
        if path in seen or folded in seen_casefold:
            raise VerificationError(f"duplicate icon member: {path}")
        seen.add(path)
        seen_casefold.add(folded)
        total_raw += info.file_size
    if total_raw > TOTAL_RESOURCE_BYTES_LIMIT:
        raise VerificationError("icon archive raw bytes exceed 64 MiB")
    if expected is not None and seen != expected:
        raise VerificationError("icon archive roster differs from manifest")


def _verify_icon_manifest(candidate_root: Path, distribution: dict[str, tuple[int, str]]) -> dict:
    skill_root = candidate_root / _SKILL_RELATIVE
    icon_root = skill_root / "templates" / "icons"
    manifest = _load_json(candidate_root / _ICON_MANIFEST)
    if manifest.get("schema_version") != 1 or manifest.get("format") != "ppt-master-icon-packs":
        raise VerificationError("unsupported icon manifest")
    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict) or upstream != {
        "commit": UPSTREAM_COMMIT,
        "repository": UPSTREAM_REPOSITORY,
        "version": UPSTREAM_VERSION,
    }:
        raise VerificationError("icon upstream provenance mismatch")
    for library in ICON_LIBRARIES:
        if (icon_root / library).exists():
            raise VerificationError(f"loose icon library remains: {library}")

    raw_icons = manifest.get("icons")
    raw_shards = manifest.get("shards")
    if not isinstance(raw_icons, list) or not isinstance(raw_shards, list):
        raise VerificationError("icon inventories are missing")
    icons: dict[str, dict] = {}
    by_shard: dict[str, list[dict]] = {}
    seen_casefold: set[str] = set()
    for raw in raw_icons:
        if not isinstance(raw, dict):
            raise VerificationError("invalid icon entry")
        icon_id = raw.get("id")
        library = raw.get("library")
        name = raw.get("name")
        path = _validate_relative_path(raw.get("path"))
        member = _validate_relative_path(raw.get("member"))
        shard = _validate_relative_path(raw.get("shard"))
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            not isinstance(icon_id, str)
            or not isinstance(library, str)
            or library not in ICON_LIBRARIES
            or not isinstance(name, str)
            or icon_id != f"{library}/{name}"
            or path != f"{icon_id}.svg"
            or member != path
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise VerificationError(f"invalid icon entry: {icon_id!r}")
        folded = icon_id.casefold()
        if icon_id in icons or folded in seen_casefold:
            raise VerificationError(f"duplicate icon id: {icon_id}")
        icons[icon_id] = raw
        seen_casefold.add(folded)
        by_shard.setdefault(shard, []).append(raw)

    shards: dict[str, dict] = {}
    packed_bytes = 0
    for raw in raw_shards:
        if not isinstance(raw, dict):
            raise VerificationError("invalid shard entry")
        path = _validate_relative_path(raw.get("path"))
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            not path.startswith("packs/")
            or not path.endswith(".zip")
            or not isinstance(size, int)
            or size < 0
            or size > HARD_SHARD_BYTES
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or path in shards
        ):
            raise VerificationError(f"invalid shard entry: {path}")
        archive_path = icon_root.joinpath(*path.split("/"))
        distribution_path = f"templates/icons/{path}"
        if distribution.get(distribution_path) != (size, digest):
            raise VerificationError(f"shard differs from distribution manifest: {path}")
        expected_records = by_shard.get(path, [])
        if raw.get("members") != len(expected_records) or raw.get("raw_bytes") != sum(
            record["size"] for record in expected_records
        ):
            raise VerificationError(f"shard totals mismatch: {path}")
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                _validate_zip_infos(
                    archive,
                    expected={record["member"] for record in expected_records},
                )
                for record in expected_records:
                    payload = archive.read(record["member"])
                    if (
                        len(payload) != record["size"]
                        or hashlib.sha256(payload).hexdigest() != record["sha256"]
                    ):
                        raise VerificationError(f"icon payload mismatch: {record['id']}")
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise VerificationError(f"invalid icon shard: {path}") from exc
        shards[path] = raw
        packed_bytes += size
    if set(shards) != set(by_shard) or packed_bytes > TOTAL_RESOURCE_BYTES_LIMIT:
        raise VerificationError("icon shard roster or total is invalid")

    libraries = manifest.get("libraries")
    if not isinstance(libraries, dict) or set(libraries) != set(ICON_LIBRARIES):
        raise VerificationError("icon library summary is invalid")
    for library in ICON_LIBRARIES:
        records = [raw for raw in icons.values() if raw["library"] == library]
        expected_summary = {
            "count": len(records),
            "raw_bytes": sum(record["size"] for record in records),
            "shards": sorted({record["shard"] for record in records}),
        }
        if libraries[library] != expected_summary:
            raise VerificationError(f"icon library summary mismatch: {library}")

    license_files = manifest.get("license_files")
    if not isinstance(license_files, list) or not license_files:
        raise VerificationError("icon license inventory is missing")
    for raw in license_files:
        if not isinstance(raw, dict):
            raise VerificationError("invalid icon license entry")
        path = _validate_relative_path(raw.get("path"))
        size = raw.get("size")
        digest = raw.get("sha256")
        if distribution.get(f"templates/icons/{path}") != (size, digest):
            raise VerificationError(f"icon license mismatch: {path}")

    totals = manifest.get("totals")
    expected_totals = {
        "icons": len(icons),
        "packed_bytes": packed_bytes,
        "raw_bytes": sum(record["size"] for record in icons.values()),
        "shards": len(shards),
    }
    if totals != expected_totals:
        raise VerificationError("icon manifest totals mismatch")
    return expected_totals


def _verify_provenance(candidate_root: Path) -> dict:
    provenance = _load_json(candidate_root / _PROVENANCE)
    expected = {
        "distribution": "ccswitch",
        "fork_repository": "https://github.com/SanAntonio021/ppt-master",
        "icon_storage": "deterministic-zip-stored-shards",
        "release_tag": "v6.1.0-ccswitch.2",
        "schema_version": 1,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_repository": UPSTREAM_REPOSITORY,
        "upstream_version": UPSTREAM_VERSION,
    }
    if provenance != expected:
        raise VerificationError("CC Switch provenance mismatch")
    return provenance


def _verify_no_lfs(candidate_root: Path) -> None:
    for relative, (_size, _digest) in _inventory(candidate_root).items():
        path = candidate_root.joinpath(*relative.split("/"))
        with path.open("rb") as handle:
            if handle.read(len(_LFS_POINTER)) == _LFS_POINTER:
                raise VerificationError(f"Git LFS pointer is forbidden: {relative}")


def _audit_icon_network_imports(candidate_root: Path) -> None:
    scripts = candidate_root / _SKILL_RELATIVE / "scripts"
    for relative in ("icon_store.py", "icon_search.py", "icon_sync.py"):
        path = scripts / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".", 1)[0]
                    if top_level in _BANNED_NETWORK_IMPORTS:
                        module = alias.name
                        break
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level = node.module.split(".", 1)[0]
                if top_level in _BANNED_NETWORK_IMPORTS:
                    module = node.module
            if module:
                raise VerificationError(f"offline icon runtime imports network module: {module}")


def _archive_member_estimate(inventory: dict[str, tuple[int, str]]) -> int:
    directories: set[str] = set()
    for path in inventory:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return 1 + len(directories) + len(inventory)


def _zip_info(path: str, *, directory: bool = False) -> zipfile.ZipInfo:
    filename = path.rstrip("/") + ("/" if directory else "")
    info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    mode = (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)
    info.external_attr = mode << 16
    return info


def _create_simulated_archive(candidate_root: Path, archive_path: Path) -> None:
    if archive_path.exists():
        raise VerificationError(f"simulated archive already exists: {archive_path}")
    inventory = _inventory(candidate_root)
    directories: set[str] = set()
    for path in inventory:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    prefix = "ppt-master-simulated"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(_zip_info(prefix, directory=True), b"")
        for directory in sorted(directories):
            archive.writestr(_zip_info(f"{prefix}/{directory}", directory=True), b"")
        for relative in inventory:
            archive.writestr(
                _zip_info(f"{prefix}/{relative}"),
                candidate_root.joinpath(*relative.split("/")).read_bytes(),
            )


def _verify_archive(candidate_root: Path, archive_path: Path) -> dict:
    candidate = _inventory(candidate_root)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise VerificationError(
                    f"archive has {len(infos)} members; limit is {MAX_ARCHIVE_MEMBERS}"
                )
            top_levels = {
                info.filename.split("/", 1)[0]
                for info in infos
                if info.filename
            }
            if len(top_levels) != 1:
                raise VerificationError("archive must have exactly one top-level directory")
            prefix = next(iter(top_levels))
            extracted: dict[str, tuple[int, str]] = {}
            casefold_paths: set[str] = set()
            for info in infos:
                mode = (info.external_attr >> 16) & 0xF000
                if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                    raise VerificationError(f"unsafe archive member: {info.filename}")
                if info.filename in {prefix, f"{prefix}/"} or info.is_dir():
                    continue
                expected_prefix = f"{prefix}/"
                if not info.filename.startswith(expected_prefix):
                    raise VerificationError(f"archive member escapes root: {info.filename}")
                relative = _validate_relative_path(info.filename[len(expected_prefix):])
                folded = relative.casefold()
                if relative in extracted or folded in casefold_paths:
                    raise VerificationError(f"archive path collision: {relative}")
                payload = archive.read(info)
                extracted[relative] = (len(payload), hashlib.sha256(payload).hexdigest())
                casefold_paths.add(folded)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise VerificationError(f"cannot verify archive: {archive_path}") from exc
    if extracted != candidate:
        missing = sorted(set(candidate) - set(extracted))
        unexpected = sorted(set(extracted) - set(candidate))
        changed = sorted(
            path for path in set(candidate) & set(extracted) if candidate[path] != extracted[path]
        )
        raise VerificationError(
            "archive raw tree mismatch: "
            f"missing={missing[:5]} unexpected={unexpected[:5]} changed={changed[:5]}"
        )
    return {
        "members": len(infos),
        "sha256": _sha256_file(archive_path),
        "size": archive_path.stat().st_size,
    }


def _run_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if completed.returncode != expected:
        raise VerificationError(
            f"command returned {completed.returncode}, expected {expected}: {command!r}; "
            f"stdout={completed.stdout[-500:]!r}; stderr={completed.stderr[-500:]!r}"
        )
    return completed


def _offline_environment(candidate_root: Path) -> dict[str, str]:
    guard = candidate_root / "tools" / "ccswitch_adapter" / "offline_guard"
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(guard) + (os.pathsep + existing if existing else "")
    env["PPT_MASTER_BLOCK_NETWORK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_cli_contract(candidate_root: Path, env: dict[str, str]) -> None:
    scripts = candidate_root / _SKILL_RELATIVE / "scripts"
    python = sys.executable
    _run_command([python, str(scripts / "attribution_guard.py")], env=env)
    valid = _run_command(
        [
            python,
            str(scripts / "icon_search.py"),
            "home",
            "--library",
            "tabler-outline",
            "--limit",
            "3",
        ],
        env=env,
    )
    try:
        parsed = json.loads(valid.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError("icon_search did not emit fixed JSON") from exc
    if not isinstance(parsed, list) or not parsed or parsed[0].get("id") != "tabler-outline/home":
        raise VerificationError("icon_search valid-result contract failed")
    empty = _run_command(
        [python, str(scripts / "icon_search.py"), "zzzz-no-such-icon-zzzz"],
        env=env,
    )
    if empty.stdout.strip() != "[]":
        raise VerificationError("icon_search empty-result contract failed")
    _run_command([python, str(scripts / "icon_search.py")], env=env, expected=2)
    _run_command(
        [python, str(scripts / "icon_search.py"), "home", "--limit", "0"],
        env=env,
        expected=2,
    )


def _run_internal_offline_smoke(candidate_root: Path, project_root: Path) -> int:
    skill_root = candidate_root / _SKILL_RELATIVE
    scripts = skill_root / "scripts"
    sys.path.insert(0, str(scripts))
    from icon_store import IconIntegrityError, IconStore, validate_relative_path
    from icon_sync import sync_icons
    from svg_finalize.embed_icons import process_svg_file

    store = IconStore()
    samples = {
        "chunk-filled": "home",
        "phosphor-duotone": "house",
        "simple-icons": "github",
        "tabler-filled": "home",
        "tabler-outline": "home",
    }
    icon_ids: list[str] = []
    for library, name in samples.items():
        icon_id = f"{library}/{name}"
        results = store.search(name, library=library, limit=20)
        if not any(result["id"] == icon_id for result in results):
            raise VerificationError(f"search did not find smoke icon: {icon_id}")
        icon_ids.append(icon_id)
    if store.search("zzzz-no-such-icon-zzzz"):
        raise VerificationError("process-local empty search was not empty")

    project_root.mkdir(parents=True, exist_ok=False)
    copied, missing = sync_icons(project_root, icon_ids, store=store)
    if missing or len(copied) != len(icon_ids):
        raise VerificationError(f"five-library sync failed: copied={copied} missing={missing}")
    for icon_id in icon_ids:
        library, name = icon_id.split("/", 1)
        destination = project_root / "icons" / library / f"{name}.svg"
        expected = store.read_icon(icon_id)
        if expected is None or destination.read_bytes() != expected:
            raise VerificationError(f"synced icon differs from packed member: {icon_id}")

    overwrite_target = project_root / "icons" / "tabler-outline" / "home.svg"
    overwrite_target.write_bytes(b"changed")
    sync_icons(project_root, ["tabler-outline/home"], store=store)
    if overwrite_target.read_bytes() != store.read_icon("tabler-outline/home"):
        raise VerificationError("icon_sync overwrite compatibility failed")

    svg_dir = project_root / "svg_output"
    svg_dir.mkdir()
    svg_path = svg_dir / "01_icon_smoke.svg"
    uses = "\n".join(
        f'<use data-icon="{icon_id}" x="{20 + index * 50}" y="20" '
        'width="32" height="32" fill="#0076A8"/>'
        for index, icon_id in enumerate(icon_ids)
    )
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100">\n'
        f"{uses}\n</svg>\n",
        encoding="utf-8",
        newline="\n",
    )
    replaced = process_svg_file(svg_path, project_root / "icons", False, False)
    rendered = svg_path.read_text(encoding="utf-8")
    if replaced != len(icon_ids) or "data-icon=" in rendered:
        raise VerificationError("five-library embed smoke failed")

    unsafe = (
        "/absolute.svg",
        "//server/share.svg",
        "C:/drive.svg",
        "..\\escape.svg",
        "../escape.svg",
        "safe/../escape.svg",
        "safe/CON.svg",
        "safe/LPT1.txt",
    )
    for value in unsafe:
        try:
            validate_relative_path(value)
        except IconIntegrityError:
            continue
        raise VerificationError(f"runtime path validator accepted: {value!r}")
    return 0


def _copy_skill_with_links(source: Path, destination: Path) -> None:
    try:
        shutil.copytree(source, destination, copy_function=os.link)
    except OSError:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, copy_function=shutil.copyfile)


def _negative_runtime_cases(candidate_root: Path, env: dict[str, str]) -> None:
    source_skill = candidate_root / _SKILL_RELATIVE
    python = sys.executable
    with tempfile.TemporaryDirectory(prefix="ppt-master-negative-") as temp_name:
        temp = Path(temp_name)

        residual_skill = temp / "residual" / "ppt-master"
        residual_skill.parent.mkdir()
        _copy_skill_with_links(source_skill, residual_skill)
        (residual_skill / "unexpected-residual.txt").write_text(
            "unexpected\n",
            encoding="utf-8",
            newline="\n",
        )
        _run_command(
            [python, str(residual_skill / "scripts" / "icon_search.py"), "home"],
            env=env,
            expected=3,
        )

        corrupt_skill = temp / "corrupt" / "ppt-master"
        corrupt_skill.parent.mkdir()
        _copy_skill_with_links(source_skill, corrupt_skill)
        pack = next((corrupt_skill / "templates" / "icons" / "packs").glob("*.zip"))
        original_pack = source_skill / "templates" / "icons" / "packs" / pack.name
        pack.unlink()
        shutil.copyfile(original_pack, pack)
        with pack.open("r+b") as handle:
            handle.seek(max(0, pack.stat().st_size // 2))
            current = handle.read(1)
            if not current:
                raise VerificationError("cannot corrupt empty shard")
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes([current[0] ^ 0xFF]))
        _run_command(
            [python, str(corrupt_skill / "scripts" / "icon_search.py"), "home"],
            env=env,
            expected=3,
        )

        attribution_skill = temp / "attribution" / "ppt-master"
        attribution_skill.parent.mkdir()
        _copy_skill_with_links(source_skill, attribution_skill)
        (attribution_skill / "SPONSORS.md").unlink()
        _run_command(
            [python, str(attribution_skill / "scripts" / "attribution_guard.py")],
            env=env,
            expected=78,
        )
        _run_command(
            [python, str(attribution_skill / "scripts" / "icon_search.py"), "home"],
            env=env,
            expected=3,
        )


def _negative_zip_cases() -> None:
    with tempfile.TemporaryDirectory(prefix="ppt-master-zip-negative-") as temp_name:
        temp = Path(temp_name)
        cases: list[tuple[str, callable]] = []

        traversal = temp / "traversal.zip"
        with zipfile.ZipFile(traversal, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("../escape.svg", b"<svg/>")
        cases.append(("path traversal", lambda: zipfile.ZipFile(traversal, "r")))

        bomb = temp / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("tabler-outline/bomb.svg", b"0" * (1024 * 1024))
        cases.append(("ZIP bomb compression", lambda: zipfile.ZipFile(bomb, "r")))

        collision = temp / "collision.zip"
        with zipfile.ZipFile(collision, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("tabler-outline/Home.svg", b"<svg/>")
            archive.writestr("tabler-outline/home.svg", b"<svg/>")
        cases.append(("case collision", lambda: zipfile.ZipFile(collision, "r")))

        symlink = temp / "symlink.zip"
        with zipfile.ZipFile(symlink, "w", compression=zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo("tabler-outline/link.svg")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target.svg")
        cases.append(("symbolic link", lambda: zipfile.ZipFile(symlink, "r")))

        for label, opener in cases:
            try:
                with opener() as archive:
                    _validate_zip_infos(archive)
            except VerificationError:
                continue
            raise VerificationError(f"negative ZIP case was accepted: {label}")


def _run_offline_smokes(candidate_root: Path) -> dict:
    env = _offline_environment(candidate_root)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('127.0.0.1', 9), 0.1)",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if probe.returncode == 0 or "PPT_MASTER_OFFLINE_BLOCKED" not in probe.stderr:
        raise VerificationError("offline guard probe did not prove socket isolation")

    _run_cli_contract(candidate_root, env)
    with tempfile.TemporaryDirectory(prefix="ppt-master-offline-smoke-") as temp_name:
        project = Path(temp_name) / "project"
        _run_command(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--internal-offline-smoke",
                str(candidate_root),
                "--internal-project",
                str(project),
            ],
            env=env,
        )
    _negative_runtime_cases(candidate_root, env)
    _negative_zip_cases()
    return {
        "attribution_negative": "pass",
        "corrupt_shard_negative": "pass",
        "five_library_search_sync_embed": "pass",
        "illegal_arguments": "pass",
        "offline_probe": "pass",
        "path_security": "pass",
        "residual_file_negative": "pass",
        "zip_bomb_negative": "pass",
    }


def verify_candidate(
    candidate_root: Path,
    *,
    rebuilt_root: Path | None = None,
    simulate_archive: Path | None = None,
    archive: Path | None = None,
    run_smokes: bool = True,
) -> dict:
    """Run all requested acceptance gates and return a structured report."""
    candidate_root = candidate_root.resolve()
    distribution = _verify_distribution_manifest(candidate_root)
    icon_totals = _verify_icon_manifest(candidate_root, distribution)
    provenance = _verify_provenance(candidate_root)
    _verify_no_lfs(candidate_root)
    _audit_icon_network_imports(candidate_root)
    repository_inventory = _inventory(candidate_root)
    estimated_members = _archive_member_estimate(repository_inventory)
    if estimated_members > MAX_ARCHIVE_MEMBERS:
        raise VerificationError(
            f"simulated archive estimate is {estimated_members}; limit is {MAX_ARCHIVE_MEMBERS}"
        )

    report = {
        "archive_member_estimate": estimated_members,
        "distribution_bytes": sum(size for size, _digest in distribution.values()),
        "distribution_files": len(distribution),
        "icon_totals": icon_totals,
        "network_dependency_audit": "pass",
        "provenance": provenance,
        "repository_files": len(repository_inventory),
    }
    if rebuilt_root is not None:
        report["raw_rebuild"] = _verify_raw_tree(candidate_root, rebuilt_root.resolve())
    if simulate_archive is not None:
        simulate_archive = simulate_archive.resolve()
        _create_simulated_archive(candidate_root, simulate_archive)
        report["simulated_archive"] = _verify_archive(candidate_root, simulate_archive)
    if archive is not None:
        report["real_archive"] = _verify_archive(candidate_root, archive.resolve())
    if run_smokes:
        report["smokes"] = _run_offline_smokes(candidate_root)
    return report


def build_parser() -> argparse.ArgumentParser:
    """Build the verifier CLI and its internal isolated-smoke entry."""
    parser = argparse.ArgumentParser(
        description="Verify the PPT Master CC Switch distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--candidate-root", help="Candidate repository tree")
    parser.add_argument("--rebuilt-root", help="Second deterministic rebuild to compare")
    parser.add_argument("--simulate-archive", help="Create and verify a local codeload simulation")
    parser.add_argument("--archive", help="Verify a real GitHub codeload ZIP")
    parser.add_argument("--json-out", help="Optional path for the verification report")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip runtime smoke commands")
    parser.add_argument("--internal-offline-smoke", help=argparse.SUPPRESS)
    parser.add_argument("--internal-project", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Run the public verifier or one isolated internal smoke process."""
    args = build_parser().parse_args(argv)
    try:
        if args.internal_offline_smoke:
            if not args.internal_project:
                raise VerificationError("internal smoke requires --internal-project")
            return _run_internal_offline_smoke(
                Path(args.internal_offline_smoke).resolve(),
                Path(args.internal_project).resolve(),
            )
        if not args.candidate_root:
            raise VerificationError("--candidate-root is required")
        report = verify_candidate(
            Path(args.candidate_root),
            rebuilt_root=Path(args.rebuilt_root) if args.rebuilt_root else None,
            simulate_archive=Path(args.simulate_archive) if args.simulate_archive else None,
            archive=Path(args.archive) if args.archive else None,
            run_smokes=not args.skip_smoke,
        )
        if args.json_out:
            _write_json(Path(args.json_out), report)
    except (VerificationError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
