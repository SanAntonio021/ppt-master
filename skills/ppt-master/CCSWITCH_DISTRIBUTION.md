# CC Switch Distribution Adapter

The Fork's default `main` branch preserves the official PPT Master 6.1.0 workflow at upstream commit
`c40bca58e168fcef2facdc7612cc352d1233679b`. It changes only the distribution
shape required by CC Switch: the five loose icon trees are represented by
deterministic stored ZIP shards plus manifests, while runtime commands keep the
same project-local outputs and authoring behavior.

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

The immutable release label is `v6.1.0-ccswitch.2`. CC Switch follows the
fast-forward-only default `main` branch; the tag exists for provenance and rebuild
comparison. Git LFS is not used.

## Rebuild

Repository-level build and verification commands live in
`tools/ccswitch_adapter/README.md`. They rebuild from the exact official source
tree, apply the small adapter allowlist, regenerate both manifests, compare raw
bytes, exercise the offline icon flow, and enforce the 2,000-member archive
limit.
