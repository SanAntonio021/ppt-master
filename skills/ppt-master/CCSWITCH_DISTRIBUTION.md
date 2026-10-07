# CC Switch Distribution Adapter

The Fork's default `main` branch uses official PPT Master 6.6.0 at upstream
commit `a50758ac29ec027e85966db33e2ae80031446756`. The five loose icon trees
are represented by deterministic stored ZIP shards plus manifests. Runtime
commands keep the same project-local outputs and quality gates.

Release `.2` also includes a limited workflow adaptation: its discovery
description selects explicit PPT Master requests, existing projects, and
coordinator-selected tasks. Such tasks use
[`references/confirmed-handoff.md`](references/confirmed-handoff.md) to reuse
genuine prior decisions through the existing confirmation branches. Direct
use retains the complete workflow. This Fork therefore maintains both
distribution and narrowly scoped handoff differences from upstream.

Release `.3` puts the user's requested concise Chinese trigger boundary first
in the discovery description so truncated host catalogs retain it. The workflow
and handoff behavior are unchanged from `.2`.

## Runtime contract

- `scripts/icon_sync.py` keeps the established
  `<project> <library/name>...` interface and overwrite behavior.
- `scripts/icon_search.py QUERY [--library LIB] [--limit N]` prints one stable
  JSON array. No match is `[]` with exit 0; argument errors use exit 2;
  integrity or license failures use exit 3; I/O failures use exit 4.
- `scripts/icon_store.py` validates official attribution, the complete raw
  distribution inventory, icon and license manifests, shard metadata, and safe
  paths on first open. It does not cache across processes.
- One process reuses one validation snapshot. A protected file size or
  `mtime_ns` change invalidates that snapshot, and every member is checked
  against its raw SHA-256 on first read.
- Runtime code never extracts packs and never uses the network.

## Trust boundary

`distribution.manifest.json` inventories every shipped skill file except the
manifest itself by raw size and SHA-256. Its own digest and the accepted Fork
commit belong to the independent local `pptx` pin; the installed skill must not
self-authorize a changed distribution manifest.

The immutable release label is `v6.6.0-ccswitch.3`. CC Switch follows the
fast-forward-only default `main` branch; the tag exists for provenance and rebuild
comparison. Git LFS is not used.

## Rebuild

Repository-level build and verification commands live in
`tools/ccswitch_adapter/README.md`. They rebuild from the exact official source
tree, apply the small adapter allowlist, regenerate both manifests, compare raw
bytes, exercise the offline icon flow, and enforce the 2,000-member archive
limit.

## Maintenance and validation state

Start maintenance by reading the repository README and AGENTS, this document,
and the adapter build README. Compare existing changes before updating the
explicit allowlist. Keep the upstream commit fixed for this release; edit the
maintained checkout, never the installed CC Switch copy. Store Git metadata
outside the cloud-synchronized working tree.

The `.3` source retains the discovery and handoff behavior; publication requires the
reproducible build, integrity checks, independent behavioral trials, and runtime
verification. Source edits alone do not establish those results. The external
`pptx` release pin records the accepted commit and manifest digest.
