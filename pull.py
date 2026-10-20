#!/usr/bin/env python3
"""Pull files from a Val Town val to local disk. Mirror of deploy.py. See README.md.

Destinations (pick one):
  (default)            loose files into the current directory
  --to PATH            loose files into PATH (created if missing)
  --zip                one zip in the current directory (<val>-<UTC stamp>.zip)
  --zip --to PATH      PATH ending in .zip = that file; otherwise a directory to put the zip in

The val id is a UUID; pass it with --val, or set VT_VAL, or edit DEFAULT_VAL.

Exit code = number of files that failed / conflicted / differed (--diff), capped at 255.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

# ---- configuration ---------------------------------------------------------

DEFAULT_VAL = "b321dc81-fd21-4e46-8e09-feb1275549e6"  # Cuefield; override via --val / VT_VAL
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MANIFEST = ".pull-manifest.json"        # per-destination record of last content pulled
ZIP_MANIFEST = "MANIFEST.sha256.json"   # written inside zips
DEPLOY_MANIFEST = ".deploy-manifest.json"  # ignored when present remotely (don't pull our own bookkeeping)
RESERVED_LOCAL = {MANIFEST, ZIP_MANIFEST, DEPLOY_MANIFEST}
DEFAULT_JOBS = 4
DEFAULT_RETRIES = 3

TOKEN = ""
VAL_ID = ""
RETRIES = DEFAULT_RETRIES


# ---- transport -------------------------------------------------------------

def api_base():
    return f"https://api.val.town/v2/vals/{VAL_ID}/files"


def call(url):
    """GET with retry/backoff honouring Retry-After. Returns (status, body-bytes)."""
    delay = 1.0
    last_err = b""
    for i in range(RETRIES + 1):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOKEN}", "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if (e.code == 429 or 500 <= e.code < 600) and i < RETRIES:
                ra = e.headers.get("Retry-After", "")
                wait = float(ra) if ra.replace(".", "", 1).isdigit() else delay
                time.sleep(min(wait, 30) + random.random() * 0.25)
                delay *= 2
                continue
            return e.code, body
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


def content_url(path):
    return f"{api_base()}/content?path={enc(path)}"


def list_url(path="", recursive=True, limit=100, offset=0):
    q = urllib.parse.urlencode({"path": path, "recursive": str(recursive).lower(),
                                "limit": limit, "offset": offset})
    return f"{api_base()}?{q}"


def list_remote():
    """All non-directory, non-reserved paths in the val; paginates until a short page."""
    paths, offset, limit = [], 0, 100
    while True:
        code, body = call(list_url("", True, limit, offset))
        if code != 200:
            sys.exit(f"list failed: {code} {body.decode('utf-8', 'replace')[:200]}")
        data = json.loads(body.decode("utf-8", "replace")).get("data", [])
        for d in data:
            if d.get("type") == "directory":
                continue
            pth = d.get("path", "")
            base = pth.rsplit("/", 1)[-1]
            if pth in RESERVED_LOCAL or base in RESERVED_LOCAL:
                continue
            paths.append(pth)
        if len(data) < limit:
            return paths
        offset += limit


def fetch(path):
    """-> (requested, resolved_name, status, bytes). On 404 for 'a/b.ts' with legacy compat,
    falls back to 'b.ts' (because old deploy.py flattened to basenames)."""
    code, body = call(content_url(path))
    name = path
    if code == 404 and ("/" in path or "\\" in path):
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        c2, b2 = call(content_url(base))
        if c2 == 200:
            code, body, name = c2, b2, base
    return path, name, code, body


# ---- content identity ------------------------------------------------------

def norm(b):
    return b.replace(b"\r\n", b"\n").strip()


def nsha(b):
    return hashlib.sha256(norm(b)).hexdigest()


# ---- path safety -----------------------------------------------------------

def safe_rel(p):
    """Return a normalised POSIX relative path, or None if unsafe.
    Rejects absolute paths (POSIX and Windows), drive letters, traversal (..),
    empty segments, and reserved names."""
    if not p:
        return None
    p = p.replace("\\", "/").strip()
    if p.startswith("/"):
        return None
    if len(p) >= 2 and p[1] == ":":
        return None
    if ":" in p:
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    out = "/".join(parts)
    base = parts[-1]
    if out in RESERVED_LOCAL or base in RESERVED_LOCAL:
        return None
    return out


def dest_for(outdir, rel):
    root = os.path.abspath(outdir)
    dest = os.path.abspath(os.path.join(root, *rel.split("/")))
    try:
        return dest if os.path.commonpath([root, dest]) == root else None
    except ValueError:
        return None


def atomic_write(path, data):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pull-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest(outdir):
    try:
        with open(os.path.join(outdir, MANIFEST), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(outdir, man):
    atomic_write(os.path.join(outdir, MANIFEST),
                 json.dumps(man, indent=1, sort_keys=True).encode("utf-8"))


# ---- selection -------------------------------------------------------------

def matches(path, globs):
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(base, g) for g in globs)


def select_targets(a, remote):
    wild = [n for n in a.names if any(c in n for c in "*?[")]
    plain = [n for n in a.names if n not in wild]
    targets = []
    if a.all or a.list:
        targets += remote
    for w in wild:
        targets += [r for r in remote if fnmatch.fnmatchcase(r, w)]
    # Normalise plain names to POSIX and look them up exactly (folder-aware)
    for n in plain:
        np = n.replace("\\", "/").strip().lstrip("/")
        # Accept both the verbatim path and a match where basename equals — prefer exact.
        if np in remote:
            targets.append(np)
        else:
            targets.append(np)  # let fetch() try it, with basename fallback on 404
    # de-dup preserving order
    seen = set()
    uniq = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    targets = uniq
    if a.match:
        targets = [t for t in targets if matches(t, a.match)]
    if a.exclude:
        targets = [t for t in targets if not matches(t, a.exclude)]
    return targets


# ---- modes -----------------------------------------------------------------

def write_files(items, outdir, a, man):
    """Per-file state machine. Returns failure count; mutates man."""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag = "[dry-run] " if a.dry_run else ""
    fails = 0
    for rel, body in items.items():
        dest = dest_for(outdir, rel)
        if dest is None:
            print(f"{rel}: path escapes destination — refused"); fails += 1; continue
        try:
            if not os.path.exists(dest):
                status = "NEW"
            else:
                with open(dest, "rb") as f:
                    local = f.read()
                if norm(local) == norm(body):
                    status = "SAME"
                elif rel in man and nsha(local) == man[rel].get("sha"):
                    status = "UPDATED"
                elif a.force:
                    status = "FORCED"
                else:
                    status = "CONFLICT"
            if not a.dry_run:
                if status == "FORCED":
                    shutil.copy2(dest, f"{dest}.bak-{stamp}")
                if status in ("NEW", "UPDATED", "FORCED"):
                    atomic_write(dest, body)
                elif status == "CONFLICT":
                    atomic_write(dest + ".remote", body)
                if status != "CONFLICT" and os.path.exists(dest + ".remote"):
                    try:
                        os.remove(dest + ".remote")
                    except OSError:
                        pass
        except OSError as e:
            print(f"{rel}: {e}"); fails += 1; continue
        extra = {
            "CONFLICT": f" — local edited; remote saved as {rel}.remote (use --force to overwrite, --diff to inspect)",
            "FORCED": f" — previous version kept as {rel}.bak-{stamp}",
        }.get(status, "")
        print(f"{tag}{status:<8} {rel} ({len(body)} bytes){extra}")
        if status == "CONFLICT":
            fails += 1
        elif not a.dry_run:
            man[rel] = {"sha": nsha(body), "at": iso}
    return fails


def show_diff(items, outdir):
    n = 0
    for rel, body in items.items():
        dest = dest_for(outdir, rel)
        if dest is None or not os.path.isfile(dest):
            print(f"{rel}: only on remote"); n += 1; continue
        with open(dest, "rb") as f:
            local = f.read()
        if norm(local) == norm(body):
            continue
        n += 1
        d = difflib.unified_diff(
            local.decode("utf-8", "replace").splitlines(),
            body.decode("utf-8", "replace").splitlines(),
            f"local/{rel}", f"remote/{rel}", lineterm="")
        print("\n".join(d))
    return n


def slug_from_val(vid: str) -> str:
    """Short, filesystem-safe tag for default zip name."""
    return vid[:8]


def zip_target(to, vid):
    default = f"{slug_from_val(vid)}-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".zip"
    if not to:
        return default
    return to if to.lower().endswith(".zip") else os.path.join(to, default)


def write_zip(path, items):
    """Deterministic: entries sorted, timestamps fixed at 1980-01-01, unix attrs fixed."""
    meta = {rel: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for rel, b in items.items()}
    entries = sorted(items.items()) + [
        (ZIP_MANIFEST, json.dumps(meta, indent=1, sort_keys=True).encode("utf-8"))]
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pull-", suffix=".tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in entries:
                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.create_system = 3
                zi.external_attr = 0o644 << 16
                z.writestr(zi, data, compresslevel=9)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest(), os.path.getsize(path)


def prune(outdir, remote, man, dry):
    """Delete local files previously pulled that no longer exist remotely."""
    rs = set(remote)
    for rel in sorted(k for k in man if k not in rs):
        if rel in RESERVED_LOCAL:
            continue
        code, _ = call(content_url(rel))
        if code != 404:
            print(f"KEPT     {rel}: not in listing but GET returned {code} — not deleting"); continue
        dest = dest_for(outdir, rel)
        if dest is None or not os.path.isfile(dest):
            man.pop(rel, None); continue
        with open(dest, "rb") as f:
            edited = nsha(f.read()) != man[rel].get("sha")
        if edited:
            print(f"KEPT     {rel}: deleted remotely but edited locally"); continue
        print(f"{'[dry-run] ' if dry else ''}PRUNED   {rel}")
        if not dry:
            try:
                os.remove(dest)
            except OSError as e:
                print(f"         {rel}: {e}")
            man.pop(rel, None)
        # Remove empty parent directories
        if not dry:
            d = os.path.dirname(dest)
            while d and os.path.abspath(d) != os.path.abspath(outdir):
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)


# ---- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pull files from a Val Town val to local disk.")
    p.add_argument("names", nargs="*", help="remote paths or wildcards (quote wildcards in bash)")
    p.add_argument("--val", metavar="UUID", help=f"val id (default: {DEFAULT_VAL[:8]}…; env VT_VAL overrides)")
    p.add_argument("--all", action="store_true", help="every file in the val")
    p.add_argument("--list", action="store_true", help="print remote paths, write nothing")
    p.add_argument("--to", "--out", dest="to", metavar="PATH",
                   help="destination directory (or .zip file with --zip); default: current directory")
    p.add_argument("--prefix", metavar="P",
                   help="prepend P/ to every local path (strip remote prefix first with --strip)")
    p.add_argument("--strip", type=int, default=0, metavar="N",
                   help="strip N leading directory segments from remote paths before writing")
    p.add_argument("--zip", action="store_true", help="write one zip instead of loose files")
    p.add_argument("--force", action="store_true", help="overwrite locally edited files (keeps .bak-<stamp>)")
    p.add_argument("--diff", action="store_true", help="unified diff local vs remote; write nothing")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    p.add_argument("--prune", action="store_true", help="delete unedited local files that were deleted remotely")
    p.add_argument("--match", action="append", default=[], metavar="GLOB",
                   help="keep only matching paths (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="drop matching paths (repeatable)")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts (e.g. with --prune)")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"parallel downloads (default {DEFAULT_JOBS})")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                   help=f"retries on 429/5xx/network error (default {DEFAULT_RETRIES})")
    return p


def apply_prefix_strip(rel: str, strip: int, prefix: str) -> str | None:
    parts = [s for s in rel.split("/") if s not in ("", ".")]
    if strip > 0:
        if strip >= len(parts):
            return None
        parts = parts[strip:]
    if prefix:
        parts = prefix.strip("/").split("/") + parts
    return "/".join(parts)


def main():
    global TOKEN, VAL_ID, RETRIES
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = build_parser()
    a = p.parse_args()

    if a.jobs < 1 or a.retries < 0:
        p.error("--jobs must be >= 1 and --retries >= 0")
    if a.strip < 0:
        p.error("--strip must be >= 0")
    if a.zip and (a.diff or a.prune or a.force):
        p.error("--zip cannot combine with --diff, --prune or --force")
    if a.diff and (a.prune or a.force):
        p.error("--diff cannot combine with --prune or --force")

    # Resolve val id
    val_id = a.val or os.environ.get("VT_VAL", "").strip() or DEFAULT_VAL
    if not UUID_RE.match(val_id):
        p.error(f"--val must be a UUID like dd19ff18-a516-473d-a3e0-9f8e66d14a90, got {val_id!r}")
    VAL_ID = val_id

    TOKEN = os.environ.get("VT", "").strip()
    RETRIES = a.retries
    if not TOKEN:
        sys.exit("VT is not set (export VT=vtwn_... or set VT=<token>)")

    outdir = a.to or "."
    need_list = a.all or a.list or a.prune or any(any(c in n for c in "*?[") for n in a.names)
    remote = list_remote() if need_list else []
    targets = select_targets(a, remote)

    if a.list:
        for t in targets:
            print(t)
        return 0
    if not targets:
        sys.exit("no files given (name files, or use --all / --list; check --match/--exclude)")

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(fetch, targets))

    fails, items = 0, {}
    for req, name, code, body in results:
        if code != 200:
            print(f"{req}: GET {code} {body.decode('utf-8', 'replace')[:120]}"); fails += 1; continue
        rel = safe_rel(name)
        if rel is None:
            print(f"{req}: unsafe remote path — refused"); fails += 1; continue
        rel = apply_prefix_strip(rel, a.strip, a.prefix or "")
        if rel is None:
            print(f"{req}: stripped away by --strip {a.strip}"); continue
        if rel in items:
            print(f"{req}: resolves to same local path as earlier file ({rel}) — skipped"); fails += 1; continue
        items[rel] = body

    if a.diff:
        sys.exit(min(fails + show_diff(items, outdir), 255))

    if a.zip:
        zpath = zip_target(a.to, VAL_ID)
        if a.dry_run:
            print(f"[dry-run] would write {zpath} with {len(items)} files")
        elif items:
            digest, size = write_zip(zpath, items)
            print(f"{zpath}: {len(items)} files, {size} bytes, sha256 {digest}")
    else:
        os.makedirs(outdir, exist_ok=True)
        man = load_manifest(outdir)
        # Tag manifest with which val this directory tracks; warn if switching vals
        track_val = man.get(".val")
        if track_val and track_val != VAL_ID:
            print(f"warning: destination was previously pulled from val {track_val[:8]}…, now pulling {VAL_ID[:8]}…",
                  file=sys.stderr)
        before = json.dumps(man, sort_keys=True)
        fails += write_files(items, outdir, a, man)
        if a.prune and fails == 0:
            # Don't prune without an explicit go-ahead when operating on a populated dir we never saw
            have_manifest = bool(man) and any(k != ".val" for k in man)
            if not have_manifest and not a.yes and not a.dry_run:
                print("refusing to prune without an existing manifest (nothing to prove files were ours); "
                      "run once without --prune first, or pass -y", file=sys.stderr)
            else:
                # The set of files we just pulled is the set we know about after mapping; prune
                # relative to the (post-prefix/strip) local keys.
                prune(outdir, list(items.keys()), man, a.dry_run)
        if not a.dry_run:
            man[".val"] = VAL_ID
        if not a.dry_run and json.dumps(man, sort_keys=True) != before:
            try:
                save_manifest(outdir, man)
            except OSError as e:
                print(f"manifest not saved: {e}")

    print(f"{len(targets) - fails}/{len(targets)} ok")
    return min(fails, 255)


if __name__ == "__main__":
    sys.exit(main())
