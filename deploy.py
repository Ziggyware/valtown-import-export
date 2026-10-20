#!/usr/bin/env python3
"""Deploy files to the free-ai val. Pure Python, UTF-8 throughout, body sent from memory (no shell arg limits).
usage:  set VT=<val town token>   (Windows)   /   export VT=<token>   (bash)
        python deploy.py app/*.ts app/*.tsx          # or any list of files; order is preserved
        python deploy.py --verify app/app.tsx        # GET back and compare
Exit code = number of files that failed."""
import json, os, sys, urllib.request, urllib.error, glob

"""Cuefield"""
VAL = "b321dc81-fd21-4e46-8e09-feb1275549e6"
API = f"https://api.val.town/v2/vals/{VAL}/files"
TOKEN = os.environ.get("VT", "").strip()
if not TOKEN:
    sys.exit("VT is not set (set VT=vtwn_... / export VT=vtwn_...)")

def call(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def put(path):
    name = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    n = len(content)
    if n > 80000:
        print(f"{name}: {n} chars > 80000 — Val Town rejects it; split the file"); return False
    code, out = call("PUT", f"{API}?path={name}", {"content": content})
    if code == 404:
        code, out = call("POST", f"{API}?path={name}", {"content": content, "type": "script"})
    ok = 200 <= code < 300
    print(f"{name} ({n} chars) -> {code} {'' if ok else out[:200]}")
    return ok

def verify(path):
    """Compare the deployed source with the local file. Uses the raw-content endpoint; the module link would return
    the transpiled build of a .ts/.tsx file, which is longer than the source and never matches."""
    name = os.path.basename(path)
    code, out = call("GET", f"{API}/content?path={name}")
    if code != 200:
        print(f"{name}: GET content {code} {out[:120]}"); return False
    with open(path, encoding="utf-8") as f: local = f.read()
    remote = out
    same = remote.replace("\r\n", "\n").strip() == local.replace("\r\n", "\n").strip()
    if not same:
        rl, ll = remote.replace("\r\n", "\n").split("\n"), local.replace("\r\n", "\n").split("\n")
        first = next((i for i, (a, b) in enumerate(zip(rl, ll)) if a != b), min(len(rl), len(ll)))
        print(f"{name}: remote {len(remote)} chars, local {len(local)} — DIFFERENT at line {first + 1}: remote={rl[first][:70] if first < len(rl) else '<eof>'!r} local={ll[first][:70] if first < len(ll) else '<eof>'!r}")
    else:
        print(f"{name}: {len(local)} chars — MATCH")
    return same

args = [a for a in sys.argv[1:] if a != "--verify"]
mode = verify if "--verify" in sys.argv else put
files = [p for a in args for p in (glob.glob(a) or [a])]
# app-helpers.ts must exist before app.tsx imports it: deploy helpers first when both are listed
files.sort(key=lambda p: 0 if p.endswith("app-helpers.ts") else 1 if not p.endswith("app.tsx") else 2)
if not files: sys.exit("no files given")
fails = sum(0 if mode(p) else 1 for p in files)
print(f"{len(files) - fails}/{len(files)} ok")
sys.exit(fails)
