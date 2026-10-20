# valtown-import-export

Two-file, zero-dependency Python 3.8+ toolkit for round-tripping files between
your local disk and a [Val Town](https://val.town) val, **preserving folder
structures** and supporting any val by id.

- `deploy.py` — upload local files to a val (create/update, mkdir parents, parallel, verify, prune)
- `pull.py` — download remote files to local disk (stateful mirror, conflicts, zip, prune)

Both scripts use the Val Town v2 API, speak JSON over `urllib`, need no
third-party packages, and take a val id as a UUID:

```
dd19ff18-a516-473d-a3e0-9f8e66d14a90
```

## Setup

```bash
# bash / zsh
export VT=vtwn_...           # Val Town API token (needs read/write on the val)
export VT_VAL=dd19ff18-a516-473d-a3e0-9f8e66d14a90   # optional; or pass --val
```
```powershell
# PowerShell
$env:VT = "vtwn_..."
$env:VT_VAL = "dd19ff18-a516-473d-a3e0-9f8e66d14a90"
```

If you don't set `VT_VAL` or pass `--val`, the scripts fall back to a built-in
default val id (originally the "Cuefield" val). Edit `DEFAULT_VAL` at the top
of each script to repoint permanently.

---

## `deploy.py` — upload to a val

```
python deploy.py [files/dirs/globs ...] [options]
```

### Picking a val

| What | How |
|---|---|
| Flag on every run | `--val dd19ff18-a516-473d-a3e0-9f8e66d14a90` |
| Env var | `VT_VAL=dd19ff18-a516-473d-a3e0-9f8e66d14a90` |
| Default | edit `DEFAULT_VAL` at top of script |

The argument is validated as a UUID and rejected early with a clear error if it
doesn't match the `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` shape.

### Folder structures

Files are uploaded **with their path relative to where you invoked the script**:

```bash
python deploy.py src/app.tsx src/lib/helpers.ts
# remote paths:  src/app.tsx, src/lib/helpers.ts

python deploy.py -r src/                # -r = recursive; dir tree preserved
# remote paths:  app.tsx, components/Card.tsx, lib/helpers.ts
# (because src/ is the root of the traversal, not part of each file's rel)

python deploy.py --all -C src/          # everything under src/, src/ is root
# same result as -r src/ but src/ is also the working directory

python deploy.py src/ -r --prefix v2    # nest the upload under v2/
# remote paths:  v2/app.tsx, v2/components/Card.tsx, v2/lib/helpers.ts

python deploy.py -r src/ --strip 1      # strip 1 leading segment
# remote paths:  Card.tsx, helpers.ts  (app.tsx had only 1 segment and is skipped)
```

Globs are supported (`src/**/*.ts`, etc.). On Windows, forward and back slashes
both work. Parent directories are created automatically on the val. The file's
`type` is set to `script` for `.ts/.tsx/.js/.jsx/.mjs/.cjs` and `file` for
everything else.

### Compare before deploying

Every deploy lists the remote files and downloads the selected existing files
into an automatically cleaned-up temporary directory before making any remote
writes. It reports `NEW`, `UPDATED`, or `SAME` for each selected path, then
uploads only new or updated files. Unchanged files remain in the deploy manifest
and are protected from `--prune`.

The comparison uses exact UTF-8 bytes against the text read locally (Python's
normal newline handling applies); unlike `--verify`, it does not strip whitespace.
A failed snapshot download aborts deployment without uploading or pruning.
`--dry-run` performs the same comparison using read-only API requests, while
`--verify` remains a separate read-only verification mode. Remote files can still
change after the snapshot; this is not an atomic deployment or a remote lock.

### Options

| Flag | Effect |
|---|---|
| `--val UUID` | target val id (overrides env and default) |
| `-C DIR`, `--directory DIR` | run as if `cd`'d into DIR first (like `make -C`) |
| `-r`, `--recursive` | descend into any directory arguments |
| `--all` | upload every non-ignored file under `-C DIR` (recursive) |
| `--prefix P` | prepend `P/` to every remote path |
| `--strip N` | drop `N` leading segments from computed remote paths |
| `--match GLOB` / `--exclude GLOB` | filter paths (repeatable) |
| `--list` | list what's on the val and exit |
| `--verify` | GET each file back and compare (CRLF/outer-whitespace normalized) |
| `--dry-run` | download and compare, then print planned uploads/deletions; no writes |
| `--prune` | after a successful upload, delete remote files not in this upload (prompts; `-y` to skip) |
| `-y`, `--yes` | assume yes to prompts |
| `--jobs N` | parallel uploads (default 4) |
| `--retries N` | retries on 429/5xx/network errors with exponential backoff + jitter (default 3) |
| `--force-basename` | legacy mode: flatten everything to its basename (old behaviour) |

### Safety & quality

- **UTF-8 only**: binary files are refused with a clear error rather than being
  mangled into a JSON string.
- **80 000 char guard**: Val Town rejects files over this limit; they're flagged
  locally instead of after a wasted upload.
- **Path safety**: absolute paths, drive letters, `..`, and reserved names
  (`MANIFEST.sha256.json`, `.pull-manifest.json`, `.deploy-manifest.json`) are
  refused.
- **Symlink escape guard**: recursive uploads skip symlinks that point outside
  the directory being uploaded.
- **Noise ignored**: common non-source directories (`.git`, `node_modules`,
  `dist`, `build`, `__pycache__`, `.next`, etc.) are skipped during recursion.
- **Duplicate detection**: if two local files map to the same remote path the
  last wins with a warning.
- **Retry with backoff**: 429/5xx/network errors are retried with exponential
  backoff, respecting `Retry-After`.
- **Atomic state**: a `.deploy-manifest.json` is written in the current
  directory after a successful run so the next `--prune` knows what was you.
- **Exit code** = number of files that failed, capped at 255; `0` on full success.

### Examples

```bash
# Point at a specific val and mirror a local folder into it under site/
python deploy.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 --all -C ./site --prefix site --prune -y

# Preview an upload (no writes)
python deploy.py -r src/ --dry-run

# Recursive into a directory, flatten into val root
python deploy.py -r dist/ --strip 1 --prefix static

# Verify nothing drifted
python deploy.py --verify src/app.tsx src/lib/helpers.ts
```

---

## `pull.py` — download from a val

Mirror of `deploy.py` for the other direction. See the inline help (`python
pull.py -h`) for the full option set; highlights:

| Flag | Effect |
|---|---|
| `--val UUID` | target val id (same semantics as deploy) |
| `--to PATH` | destination directory (or exact `.zip` file with `--zip`) |
| `--zip` | produce one deterministic zip instead of loose files |
| `--all` | pull every file |
| `--list` | list remote paths and exit |
| `--match GLOB` / `--exclude GLOB` | filters (repeatable) |
| `--diff` | unified diff local vs remote; writes nothing |
| `--dry-run` | report what would happen |
| `--prune` | delete unedited local files that no longer exist remotely (empty parent dirs are cleaned up) |
| `--force` | overwrite locally edited files, keeping a `.bak-<timestamp>` |
| `--prefix P` / `--strip N` | remap remote paths onto local layout |
| `--jobs N`, `--retries N` | parallelism / retries (defaults 4 / 3) |

### State machine per file

Every pull compares remote bytes with the local file and with a per-destination
`.pull-manifest.json` tracking what you pulled last time:

| State | Meaning | Action |
|---|---|---|
| `NEW` | no local file | write it |
| `SAME` | local equals remote (CRLF/outer-whitespace ignored) | nothing |
| `UPDATED` | local equals last pull, remote moved on | overwrite (fast-forward) |
| `CONFLICT` | local edited and cannot fast-forward | write `<file>.remote`, count as failure |
| `FORCED` | `CONFLICT` + `--force` | copy to `<file>.bak-<stamp>`, then overwrite |

A `.remote` sidecar is deleted automatically once the conflict is resolved.

### Zip mode

- Entries sorted by path, timestamps fixed at 1980-01-01, fixed permissions —
  pulling the same content twice produces a byte-identical zip (same zlib
  build), so you can compare zips by hash.
- Includes `MANIFEST.sha256.json` with every file's size + SHA-256.
- Prints the zip's overall SHA-256.
- Default filename is `<val-slug>-<UTC stamp>.zip` (e.g. `dd19ff18-20261020-143000.zip`).

### Safety

- **Atomic writes** via temp file + rename (no truncated files on interrupt).
- **Path-traversal guard** (zip-slip, absolute paths, drive letters, `..`).
- **Prune refuses to run** without a manifest (you can't prune what you never
  pulled) unless you pass `-y`.
- **Manifest tracks val id**: pulling the same directory from two different vals
  prints a warning instead of silently overwriting.
- Reserved names (`MANIFEST.sha256.json`, `.pull-manifest.json`,
  `.deploy-manifest.json`) are never written or pulled.

### Examples

```bash
python pull.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 --list
python pull.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 --all --to ./mirror
python pull.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 --all --zip
python pull.py --all --diff                  # see how local differs
python pull.py --all --to ./mirror --prune   # exact mirror, remove deleted files
python pull.py "*.ts"                        # wildcard match
```

---

## Notes / limits

- Val Town file content is stored as UTF-8 text. Binary files are not supported
  by the v2 files API; `deploy.py` refuses them with a clear message.
- Single-file content is limited to 80 000 characters by Val Town.
- Names are POSIX paths with `/` separators; backslashes are normalised.
- The scripts accept `VT` (token) and `VT_VAL` (default target val id) from the
  environment; either can be overridden with a command-line flag.
- Standard library only — no `pip install` step needed.
