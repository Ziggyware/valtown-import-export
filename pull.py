#!/usr/bin/env python3
"""Pull files from the free-ai val to local disk. Mirror of deploy.py. See README.md.

Destinations (pick one):
  (default)            loose files into the current directory
  --to PATH            loose files into PATH (created if missing)
  --zip                one zip in the current directory (free-ai-<UTC stamp>.zip)
  --zip --to PATH      PATH ending in .zip = that file; otherwise a directory to put the zip in

Exit code = number of files that failed / conflicted / differed (--diff), capped at 255."""
import argparse, concurrent.futures as cf, difflib, fnmatch, hashlib, json, os, random, shutil
import sys, tempfile, time, urllib.error, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone

"""Cuefield"""
VAL = "b321dc81-fd21-4e46-8e09-feb1275549e6"
API = f"https://api.val.town/v2/vals/{VAL}/files"
MANIFEST = ".pull-manifest.json"      # per-destination record of the last content pulled for each file
ZIP_MANIFEST = "MANIFEST.sha256.json"  # written inside zips
TOKEN = ""
RETRIES = 3


# ---------- transport: bytes in/out, retry with exponential backoff + jitter, honours Retry-After ----------
def call(url):
    delay = 1.0
    for i in range(RETRIES + 1):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "accept": "application/json"})
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
        except OSError as e:  # URLError, timeouts, resets
            if i < RETRIES:
                time.sleep(min(delay, 30) + random.random() * 0.25)
                delay *= 2
                continue
            return 0, str(e).encode()


def content_url(path):
    return f"{API}/content?{urllib.parse.urlencode({'path': path})}"


def list_remote():
    """All non-directory paths in the val; paginates until a short page."""
    paths, offset, limit = [], 0, 100
    while True:
        q = urllib.parse.urlencode({"path": "", "recursive": "true", "limit": limit, "offset": offset})
        code, body = call(f"{API}?{q}")
        if code != 200:
            sys.exit(f"list failed: {code} {body.decode('utf-8', 'replace')[:200]}")
        data = json.loads(body.decode("utf-8", "replace")).get("data", [])
        paths += [d["path"] for d in data if d.get("type") != "directory"]
        if len(data) < limit:
            return paths
        offset += limit


def fetch(path):
    """-> (requested, resolved_name, status, bytes). On 404 for 'a/b.ts', retries 'b.ts' (deploy.py flattens to basename)."""
    code, body = call(content_url(path))
    name = path
    if code == 404 and ("/" in path or "\\" in path):
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        c2, b2 = call(content_url(base))
        if c2 == 200:
            code, body, name = c2, b2, base
    return path, name, code, body


# ---------- content identity ----------
def norm(b):
    return b.replace(b"\r\n", b"\n").strip()


def nsha(b):
    return hashlib.sha256(norm(b)).hexdigest()


# ---------- path safety (zip-slip / traversal guard) ----------
def safe_rel(p):
    p = p.replace("\\", "/").strip()
    if not p or p.startswith("/") or ":" in p:  # absolute, drive letter, NTFS alternate stream
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def dest_for(outdir, rel):
    root = os.path.abspath(outdir)
    dest = os.path.abspath(os.path.join(root, *rel.split("/")))
    try:
        return dest if os.path.commonpath([root, dest]) == root else None
    except ValueError:  # different drives on Windows
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
    atomic_write(os.path.join(outdir, MANIFEST), json.dumps(man, indent=1, sort_keys=True).encode("utf-8"))


# ---------- target selection ----------
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
    targets += plain
    targets = list(dict.fromkeys(targets))
    if a.match:
        targets = [t for t in targets if matches(t, a.match)]
    if a.exclude:
        targets = [t for t in targets if not matches(t, a.exclude)]
    return targets


# ---------- modes ----------
def write_files(items, outdir, a, man):
    """Per-file state machine. Returns failure count; mutates man.
    NEW      no local file                                     -> write
    SAME     local == remote (CRLF/outer whitespace ignored)   -> nothing
    UPDATED  local == what we last pulled (unedited)           -> overwrite (fast-forward)
    FORCED   local differs, --force                            -> copy to .bak-<stamp>, overwrite
    CONFLICT local differs, not fast-forwardable               -> write <file>.remote beside it, count as failure"""
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
                    os.remove(dest + ".remote")  # sidecar from an earlier conflict is now resolved
        except OSError as e:
            print(f"{rel}: {e}"); fails += 1; continue
        extra = {"CONFLICT": f" — local edited; remote saved as {rel}.remote (use --force to overwrite, --diff to inspect)",
                 "FORCED": f" — previous version kept as {rel}.bak-{stamp}"}.get(status, "")
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
        d = difflib.unified_diff(local.decode("utf-8", "replace").splitlines(), body.decode("utf-8", "replace").splitlines(),
                                 f"local/{rel}", f"remote/{rel}", lineterm="")
        print("\n".join(d))
    return n


def zip_target(to):
    default = "free-ai-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".zip"
    if not to:
        return default
    return to if to.lower().endswith(".zip") else os.path.join(to, default)


def write_zip(path, items):
    """Deterministic: entries sorted, timestamps fixed at 1980-01-01, unix attrs fixed. Same inputs + same zlib build => same bytes."""
    meta = {rel: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for rel, b in items.items()}
    entries = sorted(items.items()) + [(ZIP_MANIFEST, json.dumps(meta, indent=1, sort_keys=True).encode("utf-8"))]
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
    """Delete local files we previously pulled that no longer exist remotely. A file is removed only if
    (1) absent from the listing, (2) a direct GET returns 404 (guards against a truncated listing), (3) unedited since pulled."""
    rs = set(remote)
    for rel in sorted(k for k in man if k not in rs):
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
            os.remove(dest)
            man.pop(rel, None)


def main():
    global TOKEN, RETRIES
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to a legacy codepage
    p = argparse.ArgumentParser(description="Pull files from the free-ai val.")
    p.add_argument("names", nargs="*", help="remote paths or wildcards (quote wildcards in bash)")
    p.add_argument("--all", action="store_true", help="every file in the val")
    p.add_argument("--list", action="store_true", help="print remote paths, write nothing")
    p.add_argument("--to", "--out", dest="to", metavar="PATH", help="destination directory (or .zip file with --zip); default: current directory")
    p.add_argument("--zip", action="store_true", help="write one zip instead of loose files")
    p.add_argument("--force", action="store_true", help="overwrite locally edited files (keeps .bak-<stamp>)")
    p.add_argument("--diff", action="store_true", help="unified diff local vs remote; write nothing")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    p.add_argument("--prune", action="store_true", help="delete unedited local files that were deleted remotely")
    p.add_argument("--match", action="append", default=[], metavar="GLOB", help="keep only matching paths (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="drop matching paths (repeatable)")
    p.add_argument("--jobs", type=int, default=4, help="parallel downloads (default 4)")
    p.add_argument("--retries", type=int, default=3, help="retries on 429/5xx/network error (default 3)")
    a = p.parse_args()

    if a.jobs < 1 or a.retries < 0:
        p.error("--jobs must be >= 1 and --retries >= 0")
    if a.zip and (a.diff or a.prune or a.force):
        p.error("--zip cannot combine with --diff, --prune or --force")
    if a.diff and (a.prune or a.force):
        p.error("--diff cannot combine with --prune or --force")
    TOKEN = os.environ.get("VT", "").strip()
    RETRIES = a.retries
    if not TOKEN:
        sys.exit("VT is not set (set VT=vtwn_... / export VT=vtwn_...)")

    outdir = a.to or "."
    need_list = a.all or a.list or a.prune or any(any(c in n for c in "*?[") for n in a.names)
    remote = list_remote() if need_list else []
    targets = select_targets(a, remote)

    if a.list:
        print("\n".join(targets)); return
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
        items[rel] = body

    if a.diff:
        sys.exit(min(fails + show_diff(items, outdir), 255))

    if a.zip:
        zpath = zip_target(a.to)
        if a.dry_run:
            print(f"[dry-run] would write {zpath} with {len(items)} files")
        elif items:
            digest, size = write_zip(zpath, items)
            print(f"{zpath}: {len(items)} files, {size} bytes, sha256 {digest}")
    else:
        man = load_manifest(outdir)
        before = json.dumps(man, sort_keys=True)
        fails += write_files(items, outdir, a, man)
        if a.prune:
            prune(outdir, remote, man, a.dry_run)
        if not a.dry_run and json.dumps(man, sort_keys=True) != before:
            try:
                save_manifest(outdir, man)
            except OSError as e:
                print(f"manifest not saved: {e}")

    print(f"{len(targets) - fails}/{len(targets)} ok")
    sys.exit(min(fails, 255))


if __name__ == "__main__":
    main()
