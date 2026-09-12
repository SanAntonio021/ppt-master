# CC Switch Adapter Build Tools

These standard-library tools reproduce the `ccswitch` distribution from the
complete official PPT Master 6.3.2 checkout at
`5e8746b08de2d625c371acfa413e17fd27a067f5`. They do not update the official
source, install a skill, or write CC Switch runtime directories.

## Inputs

- `--upstream-root`: a clean Git checkout at the exact official commit. The
  builder rejects partial/promisor configuration, untracked files, index drift,
  raw bytes that differ from official Git blobs (including CRLF conversion),
  and failed `git fsck`.
- `--adapter-root`: this Fork checkout. Only paths listed in
  `adapter-files.json` are copied from it.
- `--output`: a new directory outside both inputs. Existing output directories
  are rejected.

## Fixed build order

1. Inventory and hash the complete official checkout.
2. Copy the official tree and replace only the five loose icon directories
   with deterministic per-library `ZIP_STORED` shards.
3. Generate `templates/icons/icons.manifest.json`.
4. Apply the explicit adapter script/document allowlist.
5. Generate `skills/ppt-master/distribution.manifest.json` from final raw bytes.

Every shard targets 16 MiB, may not exceed 20 MiB, and the packed icon resource
set may not exceed 64 MiB. ZIP timestamps, order, mode bits, compression, JSON
ordering, encoding, and line endings are deterministic. Git LFS is forbidden.

## Local reproducibility and smoke

Run two builds into fresh directories:

```powershell
python tools/ccswitch_adapter/build_distribution.py `
  --upstream-root C:\path\to\official-v6.3.2 `
  --adapter-root C:\path\to\ppt-master-ccswitch `
  --output C:\path\to\build-1 `
  --json-out C:\path\to\evidence\build-1.json

python tools/ccswitch_adapter/build_distribution.py `
  --upstream-root C:\path\to\official-v6.3.2 `
  --adapter-root C:\path\to\ppt-master-ccswitch `
  --output C:\path\to\build-2 `
  --json-out C:\path\to\evidence\build-2.json

python tools/ccswitch_adapter/verify_distribution.py `
  --candidate-root C:\path\to\build-1 `
  --rebuilt-root C:\path\to\build-2 `
  --simulate-archive C:\path\to\evidence\simulated-codeload.zip `
  --json-out C:\path\to\evidence\verification.json
```

The verifier independently checks both manifests and every packed member,
compares the two repository trees by relative path/raw size/raw SHA-256, rejects
LFS pointers, and enforces the 2,000-member archive cap. Its offline phase first
proves that `sitecustomize.py` blocks a known socket probe, then exercises all
five libraries through search, sync, overwrite, and embed in one process-local
`IconStore`. Negative cases cover attribution removal, unexpected residual
files, shard corruption, path traversal, Windows reserved names, case
collisions, symbolic links, compressed ZIP-bomb input, and invalid CLI
arguments.

## Real codeload gate

After the temporary remote candidate ref exists, download its GitHub codeload
ZIP and compare it against the exact candidate tree:

```powershell
python tools/ccswitch_adapter/verify_distribution.py `
  --candidate-root C:\path\to\candidate-tree `
  --archive C:\path\to\candidate-codeload.zip `
  --json-out C:\path\to\evidence\real-codeload.json
```

Only after that gate passes may the immutable tag be created and the default
`main` branch be fast-forwarded. Candidate deletion, installation, pin state changes, and
rollback remain release-operator actions outside these build tools.
