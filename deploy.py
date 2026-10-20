#!/usr/bin/env python3
"""Deploy files to a Val Town val, preserving folder structures.

Usage
-----
  export VT=vtwn_...                       # your Val Town API token
  python deploy.py app.tsx helpers.ts      # one or more files (glob-expanded)
  python deploy.py src/                    # a directory, uploaded recursively
  python deploy.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 .
  python deploy.py --verify app.tsx        # GET each file back and compare
  python deploy.py --prune --all -C src/   # mirror: upload everything under src/,
                                           # delete remote files not in the set

The val id is a UUID; pass it with --val, or set VT_VAL, or edit DEFAULT_VAL.
Folder structure is preserved: `python deploy.py src/lib/foo.ts` uploads to
`src/lib/foo.ts` inside the val. Use --strip N to drop leading path segments,
or --prefix P to prepend a remote directory.

Exit code = number of files that failed, capped at 255.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import PurePosixPath

# ---- configuration ---------------------------------------------------------

DEFAULT_VAL = "b321dc81-fd21-4e46-8e09-feb1275549e6"  # Cuefield; override via --val / VT_VAL
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MAX_FILE_CHARS = 80_000           # Val Town rejects content longer than this per file
DEFAULT_JOBS = 4
DEFAULT_RETRIES = 3
RESERVED_REMOTE = {"MANIFEST.sha256.json", ".pull-manifest.json", ".deploy-manifest.json"}
SCRIPT_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

TOKEN = ""
VAL_ID = ""
RETRIES = DEFAULT_RETRIES


# ---- HTTP transport --------------------------------------------------------

def api_base():
    return f"https://api.val.town/v2/vals/{VAL_ID}/files"


def call(method: str, url: str, body=None):
    """Send a JSON request. Returns (status, body-bytes). Retries on 429 / 5xx / network errors
    with exponential backoff + jitter, honouring Retry-After."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    delay = 1.0
    last_err = b""
    for i in range(RETRIES + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            err = e.read()
            if (e.code == 429 or 500 <= e.code < 600) and i < RETRIES:
                ra = e.headers.get("Retry-After", "")
                wait = float(ra) if ra.replace(".", "", 1).isdigit() else delay
                time.sleep(min(wait, 30) + random.random() * 0.25)
                delay *= 2
                continue
            return e.code, err
        except OSError as e:
            last_err = str(e).encode()
            if i < RETRIES:
                time.sleep(min(delay, 30) + random.random() * 0.25)
                delay *= 2
                continue
            return 0, last_err
    return 0, last_err


def enc(p: str) -> str:
    return urllib.parse.quote(p, safe="/-._~")


def files_list_url(path="", recursive=True, limit=100, offset=0):
    q = urllib.parse.urlencode({"path": path, "recursive": str(recursive).lower(),
                                "limit": limit, "offset": offset})
    return f"{api_base()}?{q}"


def file_url(path: str) -> str:
    return f"{api_base()}?path={enc(path)}"


def content_url(path: str) -> str:
    return f"{api_base()}/content?path={enc(path)}"


# ---- remote operations -----------------------------------------------------

def list_remote() -> list[str]:
    """All non-directory paths in the val; paginates until a short page."""
    paths, offset, limit = [], 0, 100
    while True:
        code, body = call("GET", files_list_url("", True, limit, offset))
        if code != 200:
            sys.exit(f"list failed: {code} {body.decode('utf-8', 'replace')[:200]}")
        data = json.loads(body.decode("utf-8", "replace")).get("data", [])
        for d in data:
            if d.get("type") != "directory":
                paths.append(d["path"])
        if len(data) < limit:
            return paths
        offset += limit


def ensure_parent_dirs(remote_path: str) -> str | None:
    """Create every ancestor directory of `remote_path`. Returns None on success,
    or a string describing the first failure."""
    parts = remote_path.split("/")
    for i in range(1, len(parts)):
        d = "/".join(parts[:i])
        code, _ = call("POST", file_url(d), {"type": "directory"})
        if code not in (200, 201, 409):  # 409 = already exists
            return f"mkdir {d} -> {code}"
    return None


def guess_type(remote_path: str) -> str:
    return "script" if PurePosixPath(remote_path).suffix.lower() in SCRIPT_EXT else "file"


def upload_one(remote_path: str, content: str) -> tuple[str, int, str]:
    """Upload `content` to `remote_path`. Returns (path, status, message).
    Attempts PUT (update), and on 404 creates parent directories then POSTs."""
    code, body = call("PUT", file_url(remote_path), {"content": content})
    if 200 <= code < 300:
        return remote_path, code, "updated"
    if code != 404:
        return remote_path, code, body.decode("utf-8", "replace")[:160]
    err = ensure_parent_dirs(remote_path)
    if err:
        return remote_path, 0, err
    code, body = call("POST", file_url(remote_path), {"content": content, "type": guess_type(remote_path)})
    if 200 <= code < 300:
        return remote_path, code, "created"
    return remote_path, code, body.decode("utf-8", "replace")[:160]


def verify_one(remote_path: str, content: str) -> tuple[str, bool, str]:
    """GET the remote content and compare (CRLF/outer-whitespace normalized)."""
    code, body = call("GET", content_url(remote_path))
    if code != 200:
        return remote_path, False, f"GET {code} {body.decode('utf-8','replace')[:120]}"
    remote = body.decode("utf-8", "replace")
    local_norm = content.replace("\r\n", "\n").strip()
    remote_norm = remote.replace("\r\n", "\n").strip()
    if remote_norm == local_norm:
        return remote_path, True, "MATCH"
    rl = remote.replace("\r\n", "\n").split("\n")
    ll = content.replace("\r\n", "\n").split("\n")
    first = next((i for i, (a, b) in enumerate(zip(rl, ll)) if a != b), min(len(rl), len(ll)))
    ctx_r = rl[first][:70] if first < len(rl) else "<eof>"
    ctx_l = ll[first][:70] if first < len(ll) else "<eof>"
    return remote_path, False, f"DIFFERENT at line {first+1}: remote={ctx_r!r} local={ctx_l!r}"


def delete_one(remote_path: str) -> tuple[str, int, str]:
    code, body = call("DELETE", file_url(remote_path) + "&recursive=false")
    if code in (200, 204, 404):
        return remote_path, code, "deleted" if code != 404 else "already-gone"
    return remote_path, code, body.decode("utf-8", "replace")[:160]


# ---- path mapping ----------------------------------------------------------

def safe_remote(p: str) -> str | None:
    """Validate and normalize a remote path. Rejects absolute paths, drive letters,
    traversal segments, empty segments, and reserved names. Returns POSIX-style
    path relative to val root, or None."""
    if not p:
        return None
    p = p.replace("\\", "/").strip()
    # Absolute paths (POSIX or Windows-style) are rejected.
    if p.startswith("/"):
        return None
    if len(p) >= 2 and p[1] == ":":
        return None
    if ":" in p:
        return None
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    if not parts:
        return None
    rel = "/".join(parts)
    if rel in RESERVED_REMOTE or any(part in RESERVED_REMOTE for part in parts):
        return None
    return rel


def to_posix(p: str) -> str:
    return p.replace("\\", "/")


def compute_remote(local_path: str, local_root: str | None, strip: int, prefix: str) -> str | None:
    """Map a local file path to its remote path.

    * If the argument is a file and local_root is None -> use the file's path (relative to cwd)
    * If the argument is a directory (recursive) -> use the path *relative* to that directory
    * --strip removes N leading segments from the resulting relative path
    * --prefix prepends a remote directory
    """
    if local_root is None:
        # Argument was given as a file directly: relativise to cwd
        try:
            rel = os.path.relpath(local_path, start=os.getcwd())
        except ValueError:
            rel = local_path
    else:
        rel = os.path.relpath(local_path, start=local_root)
    rel = to_posix(rel)
    if strip > 0:
        segs = [s for s in rel.split("/") if s not in ("", ".")]
        if strip >= len(segs):
            return None  # stripped away entirely
        rel = "/".join(segs[strip:])
    if prefix:
        rel = prefix.rstrip("/") + "/" + rel
    return safe_remote(rel)


# ---- local discovery -------------------------------------------------------

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
             ".svelte-kit", ".nuxt", "dist", "build", "coverage", ".turbo",
             ".parcel-cache", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def expand_args(args: list[str], recursive: bool) -> list[tuple[str, str | None]]:
    """Expand globs and directories into (absolute-local-path, local-root-or-None).

    A single file  ->  (abs, None)  [root is cwd for remote mapping]
    A directory    ->  (abs, dir)   [root is the directory itself; paths relative to it]
    A glob pattern ->  (abs, cwd)   [paths relative to cwd]
    """
    out: list[tuple[str, str | None]] = []
    cwd = os.getcwd()
    seen: set[str] = set()

    def add(p: str, root: str | None):
        ap = os.path.abspath(p)
        if ap in seen:
            return
        seen.add(ap)
        out.append((ap, root))

    for a in args:
        matches = _glob(a)
        if not matches:
            # literal path that doesn't exist yet — let later steps surface the error
            add(a, None)
            continue
        for m in matches:
            if os.path.isdir(m):
                if not recursive:
                    print(f"warning: {m!r} is a directory; pass --recursive to upload its contents", file=sys.stderr)
                    continue
                for dirpath, dirnames, filenames in os.walk(m):
                    # prune skip-dirs in place so os.walk doesn't descend
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        if os.path.islink(fp):
                            # avoid following symlinks that escape the root
                            try:
                                real = os.path.realpath(fp)
                                if not real.startswith(os.path.realpath(m) + os.sep):
                                    print(f"skipping symlink escaping root: {fp}", file=sys.stderr)
                                    continue
                            except OSError:
                                pass
                        add(fp, m)
            elif os.path.isfile(m):
                add(m, None)
            else:
                print(f"warning: {m!r} is not a file or directory", file=sys.stderr)
    return out


def _glob(pattern: str) -> list[str]:
    import glob as _globmod
    # Only treat as glob if it contains an unquoted magic char
    if any(c in pattern for c in "*?["):
        return _globmod.glob(pattern, recursive=True)
    return [pattern] if os.path.exists(pattern) else []


# ---- content hashing -------------------------------------------------------

def sha_of_text(s: str) -> str:
    return hashlib.sha256(s.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def read_local(abs_path: str) -> tuple[str | None, str | None]:
    """Read a file as UTF-8 text (Val Town stores text). Returns (content, error)."""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read(), None
    except UnicodeDecodeError:
        return None, "not valid UTF-8 (binary?)"
    except OSError as e:
        return None, str(e)


# ---- manifest (remember what was last deployed, for --prune) ---------------

DEPLOY_MANIFEST = ".deploy-manifest.json"


def load_state(manifest_dir: str) -> dict:
    p = os.path.join(manifest_dir, DEPLOY_MANIFEST)
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(manifest_dir: str, state: dict) -> None:
    p = os.path.join(manifest_dir, DEPLOY_MANIFEST)
    os.makedirs(manifest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=manifest_dir, prefix=".deploy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deploy local files to a Val Town val, preserving folder structure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python deploy.py app.tsx lib/helpers.ts           # upload individual files
  python deploy.py src/ --recursive                 # upload src/ tree as src/...
  python deploy.py src/ --recursive --strip 1       # upload src/ tree but drop src/ prefix
  python deploy.py --val dd19ff18-a516-473d-a3e0-9f8e66d14a90 --all -C .
  python deploy.py --list                           # list what's on the val
  python deploy.py --verify src/app.tsx             # compare local vs remote
  python deploy.py --all -C site/ --prefix site --prune   # mirror site/ -> site/
""",
    )
    p.add_argument("paths", nargs="*", help="files, directories, or globs to upload")
    p.add_argument("--val", metavar="UUID", help=f"val id (default: {DEFAULT_VAL[:8]}…; env VT_VAL overrides)")
    p.add_argument("-C", "--directory", metavar="DIR", help="run as if started from DIR (like make -C)")
    p.add_argument("-r", "--recursive", action="store_true", help="descend into directories")
    p.add_argument("--all", action="store_true", help="upload every non-ignored file under -C DIR (recursive)")
    p.add_argument("--prefix", metavar="P", help="prepend P/ to every remote path")
    p.add_argument("--strip", type=int, default=0, metavar="N", help="strip N leading directory segments from remote paths")
    p.add_argument("--match", action="append", default=[], metavar="GLOB", help="only upload paths matching GLOB (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip paths matching GLOB (repeatable)")
    p.add_argument("--verify", action="store_true", help="GET each file back and compare bytes; write nothing")
    p.add_argument("--list", action="store_true", help="list remote files and exit")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    p.add_argument("--prune", action="store_true", help="after upload, delete remote files not in the upload set")
    p.add_argument("-y", "--yes", action="store_true", help="assume yes for prune confirmation")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"parallel uploads (default {DEFAULT_JOBS})")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"retries on 429/5xx/network (default {DEFAULT_RETRIES})")
    p.add_argument("--force-basename", action="store_true",
                   help="legacy mode: upload every file by its basename (flattens folders)")
    return p


def matches_any(path: str, globs: list[str]) -> bool:
    if not globs:
        return True
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(base, g) for g in globs)


def main() -> int:
    global TOKEN, VAL_ID, RETRIES
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = build_parser()
    a = p.parse_args()

    c_dir_abs = None
    if a.directory:
        try:
            c_dir_abs = os.path.abspath(a.directory)
            os.chdir(c_dir_abs)
        except OSError as e:
            p.error(f"-C {a.directory!r}: {e}")

    # Resolve val id
    val_id = a.val or os.environ.get("VT_VAL", "").strip() or DEFAULT_VAL
    if not UUID_RE.match(val_id):
        p.error(f"--val must be a UUID like dd19ff18-a516-473d-a3e0-9f8e66d14a90, got {val_id!r}")
    VAL_ID = val_id

    if a.jobs < 1:
        p.error("--jobs must be >= 1")
    if a.retries < 0:
        p.error("--retries must be >= 0")
    if a.strip < 0:
        p.error("--strip must be >= 0")
    if a.prefix and not safe_remote(a.prefix + "/x"):
        p.error(f"--prefix {a.prefix!r} is not a safe remote path")

    TOKEN = os.environ.get("VT", "").strip()
    RETRIES = a.retries
    if not TOKEN:
        sys.exit("VT is not set (export VT=vtwn_... or set VT=<token>)")

    if a.list:
        for pth in list_remote():
            print(pth)
        return 0

    # Determine local files
    if a.all:
        if not c_dir_abs:
            p.error("--all requires -C DIR to know the root")
        plan_inputs = expand_args([c_dir_abs], recursive=True)
    else:
        if not a.paths:
            p.error("no files given (name files/dirs/globs, or use --all / --list)")
        plan_inputs = expand_args(a.paths, recursive=a.recursive)

    # Build plan: (abs_local, remote_path)
    plan: list[tuple[str, str, str]] = []  # (abs, rel_display, remote)
    for abs_path, root in plan_inputs:
        if not os.path.isfile(abs_path):
            if os.path.exists(abs_path):
                print(f"skip (not a file): {abs_path}", file=sys.stderr)
            else:
                print(f"not found: {abs_path}", file=sys.stderr)
            continue
        if a.force_basename:
            remote = safe_remote(os.path.basename(abs_path))
        else:
            remote = compute_remote(abs_path, root, a.strip, a.prefix or "")
        if remote is None:
            print(f"skip (no remote path): {abs_path}", file=sys.stderr)
            continue
        if not matches_any(remote, a.match):
            continue
        if a.exclude and matches_any(remote, a.exclude):
            continue
        if remote in RESERVED_REMOTE:
            print(f"skip (reserved name): {remote}", file=sys.stderr)
            continue
        rel_display = os.path.relpath(abs_path)
        plan.append((abs_path, rel_display, remote))

    # De-dup by remote path (last wins; warn)
    by_remote: dict[str, tuple[str, str]] = {}
    for abs_path, rel_disp, remote in plan:
        if remote in by_remote and by_remote[remote][0] != abs_path:
            print(f"warning: {rel_disp} and {os.path.relpath(by_remote[remote][0])} "
                  f"both map to remote {remote!r}; using {rel_disp}", file=sys.stderr)
        by_remote[remote] = (abs_path, rel_disp)

    if not by_remote:
        sys.exit("nothing to upload (check paths, --match/--exclude)")

    # Read content (up-front so we can fail fast on binary / oversize)
    batch: dict[str, tuple[str, str]] = {}  # remote -> (abs, content)
    fails = 0
    for remote, (abs_path, rel_disp) in sorted(by_remote.items()):
        content, err = read_local(abs_path)
        if err:
            print(f"FAIL     {rel_disp} -> {remote}: {err}")
            fails += 1
            continue
        n = len(content)
        if n > MAX_FILE_CHARS:
            print(f"SKIP     {rel_disp} -> {remote}: {n} chars > {MAX_FILE_CHARS} (Val Town limit); split the file")
            fails += 1
            continue
        batch[remote] = (abs_path, content)

    if a.dry_run:
        for remote, (abs_path, content) in sorted(batch.items()):
            rel_disp = os.path.relpath(abs_path)
            print(f"[dry-run] upload {rel_disp} -> {remote} ({len(content)} chars)")
        if a.prune:
            existing = set(list_remote())
            plan_keys = set(batch.keys())
            for doomed in sorted(existing - plan_keys):
                if not doomed.startswith("."):
                    print(f"[dry-run] delete {doomed}")
        print(f"{len(batch)} file(s) planned, {fails} failed at read")
        return min(fails, 255)

    if a.verify:
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(verify_one, r, c): (r, a_) for r, (a_, c) in batch.items()}
            ok = 0
            for fut in cf.as_completed(futs):
                rpath, same, msg = fut.result()
                _, abs_path = futs[fut]
                rel_disp = os.path.relpath(abs_path)
                print(f"{'OK' if same else 'DIFF'}     {rel_disp} -> {rpath}: {msg}")
                if same:
                    ok += 1
                else:
                    fails += 1
        print(f"{ok}/{len(batch)} match")
        return min(fails, 255)

    # Upload
    print(f"deploying {len(batch)} file(s) to val {VAL_ID} with {a.jobs} worker(s)")
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(upload_one, r, c): (r, a_) for r, (a_, c) in batch.items()}
        succeeded: set[str] = set()
        for fut in cf.as_completed(futs):
            remote = futs[fut][0]
            abs_path = futs[fut][1]
            rel_disp = os.path.relpath(abs_path)
            try:
                rpath, code, msg = fut.result()
            except Exception as e:
                print(f"FAIL     {rel_disp} -> {remote}: {e}")
                fails += 1
                continue
            ok = 200 <= code < 300
            tag = {"created": "NEW", "updated": "UPDATED"}.get(msg, ("OK" if ok else "FAIL"))
            print(f"{tag:<8} {rel_disp} -> {rpath} ({code} {msg})" if ok
                  else f"FAIL     {rel_disp} -> {rpath}: {code} {msg}")
            if ok:
                succeeded.add(rpath)
            else:
                fails += 1

    # Prune
    if a.prune and not fails:
        existing = set(list_remote())
        doomed = sorted(p for p in existing - succeeded if p not in RESERVED_REMOTE)
        if doomed:
            if not a.yes:
                print(f"\nAbout to delete {len(doomed)} remote file(s) not in this deploy:")
                for d in doomed[:20]:
                    print(f"  {d}")
                if len(doomed) > 20:
                    print(f"  ... and {len(doomed)-20} more")
                try:
                    resp = input("Proceed? [y/N] ").strip().lower()
                except EOFError:
                    resp = "n"
                if resp not in ("y", "yes"):
                    print("prune aborted")
                    doomed = []
            if doomed:
                with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
                    futs = {ex.submit(delete_one, d): d for d in doomed}
                    for fut in cf.as_completed(futs):
                        rpath, code, msg = fut.result()
                        ok = code in (200, 204, 404)
                        print(f"{'DEL' if ok else 'FAIL'}      {rpath} ({code} {msg})")
                        if not ok:
                            fails += 1

    # Save state for next run's bookkeeping (only the set we intended to upload)
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = {
        "val": VAL_ID,
        "at": iso,
        "files": {r: {"sha": sha_of_text(c), "bytes": len(c)}
                  for r, (_, c) in batch.items() if r in succeeded},
    }
    try:
        save_state(".", state)
    except OSError as e:
        print(f"note: could not write {DEPLOY_MANIFEST}: {e}", file=sys.stderr)

    ok_count = len(succeeded)
    print(f"\n{ok_count}/{len(batch)} ok" + (f", {fails} failed" if fails else ""))
    return min(fails, 255)


if __name__ == "__main__":
    sys.exit(main())
