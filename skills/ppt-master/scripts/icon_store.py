#!/usr/bin/env python3
"""
PPT Master - Packed Icon Store

Validate the CC Switch distribution and provide process-local access to the
deterministic bundled icon packs. The store never extracts archives and never
uses the network.

Usage:
    Import ``IconStore`` from ``icon_sync.py``, ``icon_search.py``, or the
    Confirm UI server.

Examples:
    store = IconStore()
    payload = store.read_icons(["tabler-outline/home"])

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from attribution_guard import require_skill_integrity


ICON_LIBRARIES = (
    "chunk-filled",
    "phosphor-duotone",
    "simple-icons",
    "tabler-filled",
    "tabler-outline",
)
_SKILL_DIR = Path(__file__).resolve().parent.parent
_DISTRIBUTION_MANIFEST = "distribution.manifest.json"
_ICON_MANIFEST = "templates/icons/icons.manifest.json"
_PROVENANCE = "ccswitch.provenance.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_WINDOWS_RESERVED_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z",
    re.IGNORECASE,
)
_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RUNTIME_IGNORED_DIRS = {"__pycache__"}
_RUNTIME_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_TARGET_SHARD_BYTES = 16 * 1024 * 1024
_HARD_SHARD_BYTES = 20 * 1024 * 1024
_TOTAL_RESOURCE_BYTES = 64 * 1024 * 1024
_MAX_ICON_BYTES = 4 * 1024 * 1024
_MAX_ICON_COUNT = 20_000


class IconStoreError(RuntimeError):
    """Base error for packed icon access."""


class IconIntegrityError(IconStoreError):
    """The installed distribution is incomplete, modified, or unsafe."""


class IconStoreIOError(IconStoreError):
    """The installed distribution could not be read from disk."""


@dataclass(frozen=True)
class IconRecord:
    """Describe one immutable icon member in a deterministic shard."""

    icon_id: str
    library: str
    name: str
    path: str
    size: int
    sha256: str
    shard: str
    member: str


def validate_relative_path(value: str) -> str:
    """Return one safe canonical POSIX relative path or reject it."""
    if not isinstance(value, str) or not value:
        raise IconIntegrityError("manifest path must be a non-empty string")
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.startswith("//")
        or _DRIVE_RE.match(value)
        or PurePosixPath(value).is_absolute()
    ):
        raise IconIntegrityError(f"unsafe manifest path: {value!r}")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise IconIntegrityError(f"unsafe manifest path: {value!r}")
    for part in parts:
        if (
            ":" in part
            or part.endswith(".")
            or part.endswith(" ")
            or _WINDOWS_RESERVED_RE.fullmatch(part)
        ):
            raise IconIntegrityError(f"unsafe Windows path segment: {value!r}")
    return value


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = path.read_bytes()
    except PermissionError as exc:
        raise IconStoreIOError(f"cannot read {label}: {path}") from exc
    except OSError as exc:
        if not path.exists():
            raise IconIntegrityError(f"missing {label}: {path}") from exc
        raise IconStoreIOError(f"cannot read {label}: {path}") from exc
    if payload.startswith(b"\xef\xbb\xbf"):
        raise IconIntegrityError(f"UTF-8 BOM is not allowed in {label}")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IconIntegrityError(f"invalid {label}: {path}") from exc
    if not isinstance(parsed, dict):
        raise IconIntegrityError(f"{label} must contain a JSON object")
    return parsed


def _is_runtime_ignored(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return (
        any(part in _RUNTIME_IGNORED_DIRS for part in parts)
        or PurePosixPath(relative_path).suffix.casefold() in _RUNTIME_IGNORED_SUFFIXES
    )


def _regular_file_state(path: Path, label: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except PermissionError as exc:
        raise IconStoreIOError(f"cannot stat {label}: {path}") from exc
    except OSError as exc:
        raise IconIntegrityError(f"missing protected file: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise IconIntegrityError(f"protected path is not a regular file: {path}")
    return metadata.st_size, metadata.st_mtime_ns


def _sha256_file(path: Path, label: str) -> tuple[str, tuple[int, int]]:
    before = _regular_file_state(path, label)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except PermissionError as exc:
        raise IconStoreIOError(f"cannot read {label}: {path}") from exc
    except OSError as exc:
        raise IconStoreIOError(f"I/O failure while reading {label}: {path}") from exc
    after = _regular_file_state(path, label)
    if before != after:
        raise IconIntegrityError(f"protected file changed during verification: {path}")
    return digest.hexdigest(), after


def _safe_child(root: Path, relative_path: str) -> Path:
    relative_path = validate_relative_path(relative_path)
    candidate = root.joinpath(*relative_path.split("/"))
    resolved_root = root.resolve()
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise IconIntegrityError(f"path escapes the skill root: {relative_path!r}") from exc
    return candidate


class IconStore:
    """Validate one installed distribution and serve packed icons within it."""

    def __init__(self, skill_root: Path | None = None) -> None:
        self.skill_root = (skill_root or _SKILL_DIR).resolve()
        if self.skill_root != _SKILL_DIR.resolve():
            raise IconIntegrityError("IconStore must use the directory that owns this module")
        try:
            require_skill_integrity()
        except SystemExit as exc:
            raise IconIntegrityError("PPT Master attribution guard failed") from exc

        self._distribution = _load_json(
            self.skill_root / _DISTRIBUTION_MANIFEST,
            "distribution manifest",
        )
        self._file_digests, verified_states = self._verify_distribution()
        self._provenance = self._verify_provenance()
        self._icon_manifest = _load_json(
            self.skill_root / _ICON_MANIFEST,
            "icon manifest",
        )
        self._records, self._records_by_shard = self._verify_icon_manifest()

        distribution_path = self.skill_root / _DISTRIBUTION_MANIFEST
        verified_states[_DISTRIBUTION_MANIFEST] = _regular_file_state(
            distribution_path,
            "distribution manifest",
        )
        self._snapshot = dict(sorted(verified_states.items()))
        self._bytes_cache: dict[str, bytes] = {}

    @property
    def libraries(self) -> tuple[str, ...]:
        """Return the supported bundled library ids in stable order."""
        return ICON_LIBRARIES

    def _verify_distribution(self) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
        manifest = self._distribution
        if manifest.get("schema_version") != 1 or manifest.get("distribution") != "ccswitch":
            raise IconIntegrityError("unsupported distribution manifest")
        upstream = manifest.get("upstream")
        if not isinstance(upstream, dict):
            raise IconIntegrityError("distribution manifest is missing upstream provenance")

        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise IconIntegrityError("distribution manifest has no file inventory")

        expected: dict[str, tuple[int, str]] = {}
        casefold_paths: set[str] = set()
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise IconIntegrityError("invalid distribution file entry")
            relative_path = validate_relative_path(raw.get("path"))
            size = raw.get("size")
            digest = raw.get("sha256")
            if (
                relative_path == _DISTRIBUTION_MANIFEST
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
            ):
                raise IconIntegrityError(f"invalid distribution entry: {relative_path!r}")
            folded = relative_path.casefold()
            if relative_path in expected or folded in casefold_paths:
                raise IconIntegrityError(f"duplicate distribution path: {relative_path!r}")
            expected[relative_path] = (size, digest)
            casefold_paths.add(folded)

        actual: set[str] = set()
        for directory, dirnames, filenames in os.walk(self.skill_root, followlinks=False):
            directory_path = Path(directory)
            safe_dirnames = []
            for dirname in sorted(dirnames):
                path = directory_path / dirname
                relative = path.relative_to(self.skill_root).as_posix()
                if dirname in _RUNTIME_IGNORED_DIRS:
                    continue
                if path.is_symlink():
                    raise IconIntegrityError(f"symbolic link is not allowed: {relative}")
                safe_dirnames.append(dirname)
            dirnames[:] = safe_dirnames
            for filename in sorted(filenames):
                path = directory_path / filename
                relative = path.relative_to(self.skill_root).as_posix()
                if relative == _DISTRIBUTION_MANIFEST or _is_runtime_ignored(relative):
                    continue
                validate_relative_path(relative)
                if path.is_symlink():
                    raise IconIntegrityError(f"symbolic link is not allowed: {relative}")
                actual.add(relative)

        missing = sorted(set(expected) - actual)
        unexpected = sorted(actual - set(expected))
        if missing or unexpected:
            detail = []
            if missing:
                detail.append(f"missing={missing[:5]}")
            if unexpected:
                detail.append(f"unexpected={unexpected[:5]}")
            raise IconIntegrityError("distribution tree mismatch: " + "; ".join(detail))

        digests: dict[str, str] = {}
        states: dict[str, tuple[int, int]] = {}
        for relative_path in sorted(expected):
            path = _safe_child(self.skill_root, relative_path)
            digest, state = _sha256_file(path, relative_path)
            expected_size, expected_digest = expected[relative_path]
            if state[0] != expected_size or digest != expected_digest:
                raise IconIntegrityError(f"distribution hash mismatch: {relative_path}")
            digests[relative_path] = digest
            states[relative_path] = state

        totals = manifest.get("totals")
        if not isinstance(totals, dict):
            raise IconIntegrityError("distribution totals are missing")
        if totals.get("files") != len(expected) or totals.get("bytes") != sum(
            size for size, _digest in expected.values()
        ):
            raise IconIntegrityError("distribution totals do not match the inventory")
        return digests, states

    def _verify_provenance(self) -> dict:
        provenance = _load_json(self.skill_root / _PROVENANCE, "CC Switch provenance")
        required = {
            "schema_version": 1,
            "distribution": "ccswitch",
            "fork_repository": "https://github.com/SanAntonio021/ppt-master",
            "upstream_repository": "https://github.com/hugohe3/ppt-master",
            "upstream_version": "6.3.2",
            "upstream_commit": "5e8746b08de2d625c371acfa413e17fd27a067f5",
            "release_tag": "v6.3.2-ccswitch.1",
            "icon_storage": "deterministic-zip-stored-shards",
        }
        if any(provenance.get(key) != value for key, value in required.items()):
            raise IconIntegrityError("CC Switch provenance does not match this release")
        upstream = self._distribution.get("upstream", {})
        if (
            upstream.get("repository") != required["upstream_repository"]
            or upstream.get("version") != required["upstream_version"]
            or upstream.get("commit") != required["upstream_commit"]
        ):
            raise IconIntegrityError("distribution provenance does not match the release pin")
        return provenance

    def _verify_icon_manifest(
        self,
    ) -> tuple[dict[str, IconRecord], dict[str, list[IconRecord]]]:
        manifest = self._icon_manifest
        if manifest.get("schema_version") != 1 or manifest.get("format") != "ppt-master-icon-packs":
            raise IconIntegrityError("unsupported icon manifest")
        upstream = manifest.get("upstream")
        if not isinstance(upstream, dict) or (
            upstream.get("repository") != self._provenance["upstream_repository"]
            or upstream.get("version") != self._provenance["upstream_version"]
            or upstream.get("commit") != self._provenance["upstream_commit"]
        ):
            raise IconIntegrityError("icon manifest provenance mismatch")
        storage = manifest.get("storage")
        if not isinstance(storage, dict) or (
            storage.get("compression") != "ZIP_STORED"
            or storage.get("target_shard_bytes") != _TARGET_SHARD_BYTES
            or storage.get("hard_shard_bytes") != _HARD_SHARD_BYTES
            or storage.get("total_resource_bytes_limit") != _TOTAL_RESOURCE_BYTES
        ):
            raise IconIntegrityError("icon storage contract mismatch")

        icon_root = self.skill_root / "templates" / "icons"
        for library in ICON_LIBRARIES:
            if (icon_root / library).exists():
                raise IconIntegrityError(f"loose icon library remains in distribution: {library}")

        raw_icons = manifest.get("icons")
        raw_shards = manifest.get("shards")
        if (
            not isinstance(raw_icons, list)
            or not isinstance(raw_shards, list)
            or len(raw_icons) > _MAX_ICON_COUNT
        ):
            raise IconIntegrityError("invalid icon or shard inventory")

        shard_entries: dict[str, dict] = {}
        shard_casefold: set[str] = set()
        total_shard_bytes = 0
        for raw in raw_shards:
            if not isinstance(raw, dict):
                raise IconIntegrityError("invalid shard entry")
            shard_path = validate_relative_path(raw.get("path"))
            size = raw.get("size")
            raw_bytes = raw.get("raw_bytes")
            member_count = raw.get("members")
            digest = raw.get("sha256")
            if (
                not shard_path.startswith("packs/")
                or not shard_path.endswith(".zip")
                or not isinstance(size, int)
                or size < 0
                or size > _HARD_SHARD_BYTES
                or not isinstance(raw_bytes, int)
                or raw_bytes < 0
                or not isinstance(member_count, int)
                or member_count < 1
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
            ):
                raise IconIntegrityError(f"invalid shard metadata: {shard_path!r}")
            folded = shard_path.casefold()
            if shard_path in shard_entries or folded in shard_casefold:
                raise IconIntegrityError(f"duplicate shard path: {shard_path!r}")
            distribution_path = f"templates/icons/{shard_path}"
            if self._file_digests.get(distribution_path) != digest:
                raise IconIntegrityError(f"shard hash mismatch: {shard_path}")
            if _safe_child(icon_root, shard_path).stat().st_size != size:
                raise IconIntegrityError(f"shard size mismatch: {shard_path}")
            shard_entries[shard_path] = raw
            shard_casefold.add(folded)
            total_shard_bytes += size
        if total_shard_bytes > _TOTAL_RESOURCE_BYTES:
            raise IconIntegrityError("packed icon resources exceed 64 MiB")

        records: dict[str, IconRecord] = {}
        records_casefold: set[str] = set()
        records_by_shard: dict[str, list[IconRecord]] = {path: [] for path in shard_entries}
        for raw in raw_icons:
            if not isinstance(raw, dict):
                raise IconIntegrityError("invalid icon entry")
            icon_id = raw.get("id")
            library = raw.get("library")
            name = raw.get("name")
            path = validate_relative_path(raw.get("path"))
            member = validate_relative_path(raw.get("member"))
            shard = validate_relative_path(raw.get("shard"))
            size = raw.get("size")
            digest = raw.get("sha256")
            if (
                not isinstance(icon_id, str)
                or not isinstance(library, str)
                or library not in ICON_LIBRARIES
                or not isinstance(name, str)
                or not name
                or icon_id != f"{library}/{name}"
                or path != f"{icon_id}.svg"
                or member != path
                or shard not in shard_entries
                or not isinstance(size, int)
                or size < 0
                or size > _MAX_ICON_BYTES
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
            ):
                raise IconIntegrityError(f"invalid icon metadata: {icon_id!r}")
            folded = icon_id.casefold()
            if icon_id in records or folded in records_casefold:
                raise IconIntegrityError(f"duplicate icon id: {icon_id!r}")
            record = IconRecord(
                icon_id=icon_id,
                library=library,
                name=name,
                path=path,
                size=size,
                sha256=digest,
                shard=shard,
                member=member,
            )
            records[icon_id] = record
            records_casefold.add(folded)
            records_by_shard[shard].append(record)

        libraries = manifest.get("libraries")
        if not isinstance(libraries, dict) or set(libraries) != set(ICON_LIBRARIES):
            raise IconIntegrityError("icon library summary mismatch")
        for library in ICON_LIBRARIES:
            summary = libraries[library]
            library_records = [record for record in records.values() if record.library == library]
            if (
                not isinstance(summary, dict)
                or summary.get("count") != len(library_records)
                or summary.get("raw_bytes") != sum(record.size for record in library_records)
                or summary.get("shards") != sorted(
                    {record.shard for record in library_records}
                )
            ):
                raise IconIntegrityError(f"icon library summary mismatch: {library}")

        declared_pack_paths = set(shard_entries)
        pack_root = icon_root / "packs"
        try:
            actual_pack_paths = {
                path.relative_to(icon_root).as_posix()
                for path in pack_root.iterdir()
                if path.is_file()
            }
        except PermissionError as exc:
            raise IconStoreIOError(f"cannot read icon pack directory: {pack_root}") from exc
        except OSError as exc:
            raise IconIntegrityError(f"missing icon pack directory: {pack_root}") from exc
        if actual_pack_paths != declared_pack_paths:
            raise IconIntegrityError("icon pack directory does not match the manifest")

        for shard_path, expected_records in sorted(records_by_shard.items()):
            archive_path = _safe_child(icon_root, shard_path)
            try:
                with zipfile.ZipFile(archive_path, "r") as archive:
                    infos = archive.infolist()
            except PermissionError as exc:
                raise IconStoreIOError(f"cannot read icon shard: {archive_path}") from exc
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise IconIntegrityError(f"invalid icon shard: {shard_path}") from exc
            if len(infos) != len(expected_records):
                raise IconIntegrityError(f"icon shard member count mismatch: {shard_path}")
            info_by_name: dict[str, zipfile.ZipInfo] = {}
            info_casefold: set[str] = set()
            for info in infos:
                member = validate_relative_path(info.filename)
                mode = (info.external_attr >> 16) & 0xF000
                if (
                    info.is_dir()
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.file_size != info.compress_size
                    or info.file_size > _MAX_ICON_BYTES
                    or info.flag_bits & 0x1
                    or mode == stat.S_IFLNK
                ):
                    raise IconIntegrityError(f"unsafe icon shard member: {member}")
                folded = member.casefold()
                if member in info_by_name or folded in info_casefold:
                    raise IconIntegrityError(f"duplicate icon shard member: {member}")
                info_by_name[member] = info
                info_casefold.add(folded)
            expected_names = {record.member for record in expected_records}
            if set(info_by_name) != expected_names:
                raise IconIntegrityError(f"icon shard roster mismatch: {shard_path}")
            for record in expected_records:
                if info_by_name[record.member].file_size != record.size:
                    raise IconIntegrityError(f"icon member size mismatch: {record.icon_id}")
            shard_summary = shard_entries[shard_path]
            if shard_summary.get("raw_bytes") != sum(
                record.size for record in expected_records
            ):
                raise IconIntegrityError(f"icon shard raw total mismatch: {shard_path}")

        license_files = manifest.get("license_files")
        if not isinstance(license_files, list) or not license_files:
            raise IconIntegrityError("icon license inventory is missing")
        for raw in license_files:
            if not isinstance(raw, dict):
                raise IconIntegrityError("invalid icon license entry")
            path = validate_relative_path(raw.get("path"))
            size = raw.get("size")
            digest = raw.get("sha256")
            distribution_path = f"templates/icons/{path}"
            license_path = _safe_child(icon_root, path)
            if (
                not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
                or self._file_digests.get(distribution_path) != digest
                or license_path.stat().st_size != size
            ):
                raise IconIntegrityError(f"icon license hash mismatch: {path}")

        totals = manifest.get("totals")
        if not isinstance(totals, dict) or (
            totals.get("icons") != len(records)
            or totals.get("raw_bytes") != sum(record.size for record in records.values())
            or totals.get("packed_bytes") != total_shard_bytes
            or totals.get("shards") != len(shard_entries)
        ):
            raise IconIntegrityError("icon manifest totals mismatch")
        return records, records_by_shard

    def _ensure_snapshot_current(self) -> None:
        for relative_path, expected_state in self._snapshot.items():
            path = _safe_child(self.skill_root, relative_path)
            if _regular_file_state(path, relative_path) != expected_state:
                raise IconIntegrityError(
                    f"protected file changed after validation: {relative_path}"
                )

    def search(
        self,
        query: str,
        *,
        library: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """Search stable manifest ids without reading or extracting shard members."""
        self._ensure_snapshot_current()
        normalized = query.strip().casefold()
        tokens = _SEARCH_TOKEN_RE.findall(normalized)
        if not normalized or not tokens:
            return []
        ranked: list[tuple[int, str, str, IconRecord]] = []
        for record in self._records.values():
            if library is not None and record.library != library:
                continue
            name = record.name.casefold()
            normalized_name = name.replace("_", "-")
            if name == normalized:
                score = 0
            elif name.startswith(normalized):
                score = 1
            elif normalized in name:
                score = 2
            elif all(token in normalized_name for token in tokens):
                score = 3
            else:
                continue
            ranked.append((score, record.name, record.library, record))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return [
            {
                "id": record.icon_id,
                "library": record.library,
                "name": record.name,
            }
            for _score, _name, _library, record in ranked[:limit]
        ]

    def read_icons(self, icon_ids: list[str]) -> dict[str, bytes]:
        """Read requested members once, hashing each on its first process read."""
        self._ensure_snapshot_current()
        result: dict[str, bytes] = {}
        pending_by_shard: dict[str, list[IconRecord]] = {}
        for icon_id in icon_ids:
            record = self._records.get(icon_id)
            if record is None:
                continue
            cached = self._bytes_cache.get(icon_id)
            if cached is not None:
                result[icon_id] = cached
                continue
            pending_by_shard.setdefault(record.shard, []).append(record)

        icon_root = self.skill_root / "templates" / "icons"
        for shard, records in sorted(pending_by_shard.items()):
            relative_path = f"templates/icons/{shard}"
            expected_state = self._snapshot[relative_path]
            archive_path = _safe_child(icon_root, shard)
            if _regular_file_state(archive_path, shard) != expected_state:
                raise IconIntegrityError(f"icon shard changed before read: {shard}")
            try:
                with zipfile.ZipFile(archive_path, "r") as archive:
                    for record in records:
                        payload = archive.read(record.member)
                        digest = hashlib.sha256(payload).hexdigest()
                        if len(payload) != record.size or digest != record.sha256:
                            raise IconIntegrityError(
                                f"icon member hash mismatch: {record.icon_id}"
                            )
                        self._bytes_cache[record.icon_id] = payload
                        result[record.icon_id] = payload
            except PermissionError as exc:
                raise IconStoreIOError(f"cannot read icon shard: {archive_path}") from exc
            except KeyError as exc:
                raise IconIntegrityError(f"missing icon member in shard: {shard}") from exc
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise IconIntegrityError(f"invalid icon shard during read: {shard}") from exc
            if _regular_file_state(archive_path, shard) != expected_state:
                raise IconIntegrityError(f"icon shard changed during read: {shard}")
        return result

    def read_icon(self, icon_id: str) -> bytes | None:
        """Return one verified icon payload, or ``None`` when the id is absent."""
        return self.read_icons([icon_id]).get(icon_id)
