#!/usr/bin/env python3
"""
sync_packs.py - download, verify, hash and (optionally) re-host Minecraft resource packs.

Reads a config like:

    links:
      trim-rp:
        url: "https://example.com/pack.zip"
        sha1: ""
        priority: 0

For every entry it:
  1. downloads the pack (retries, browser UA, detects HTML error pages such as Google Drive quota pages)
  2. verifies it is a real zip and contains pack.mcmeta
  3. computes SHA-1 + size
  4. stores it as <out-dir>/upload/<sha1>.zip   (hash-named => immutable => cache forever)
  5. optionally uploads to a GitHub Release (needs the `gh` CLI, logged in)
  6. prints a summary table + a ready-to-paste YAML with new urls and sha1s
     and writes the same YAML to <config>.new.yml

No third-party Python dependencies.

Examples:
  python sync_packs.py                                   # download + hash only
  python sync_packs.py --repo you/mc-packs --upload      # also push to GitHub Releases
  python sync_packs.py --base-url https://packs.example.com/   # other host (R2/Bunny/nginx), you upload upload/*
"""
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# --------------------------------------------------------------------------- config
def parse_config(text):
    """Tiny parser for the exact `links:` layout (no PyYAML needed). Keeps file order."""
    entries, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^  ([\w.\-]+):\s*$", line)
        if m:
            cur = {"name": m.group(1), "url": "", "sha1": "", "priority": str(len(entries))}
            entries.append(cur)
            continue
        m = re.match(r'^\s{4}(url|sha1|priority):\s*"?([^"\n]*?)"?\s*$', line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    return [e for e in entries if e["url"]]


def render_yaml(entries):
    out = ["links:"]
    for e in entries:
        out += [
            f"  {e['name']}:",
            f'    url: "{e["new_url"]}"',
            f'    sha1: "{e["sha1"]}"',
            f"    priority: {e['priority']}",
        ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- download / verify
def download(url, dest, retries=4):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": UA})
            sha, size = hashlib.sha1(), 0
            with urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
            return sha.hexdigest(), size
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"download failed after {retries} tries: {last}")


def verify_zip(path):
    """Return a list of warnings; raise if the file is not a usable pack."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head != b"PK\x03\x04":
        raise RuntimeError("not a zip (probably an HTML error/confirmation page - Google Drive quota?)")
    if not zipfile.is_zipfile(path):
        raise RuntimeError("corrupt zip")
    warns = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "pack.mcmeta" not in names:
            warns.append("no pack.mcmeta at zip root (nested folder? Minecraft will reject it)")
    return warns


def process(entry, out_dir, refresh):
    raw = out_dir / "raw" / f"{entry['name']}.zip"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not raw.exists():
        download(entry["url"], raw)
    warns = verify_zip(raw)
    sha = hashlib.sha1(raw.read_bytes()).hexdigest()
    size = raw.stat().st_size
    up = out_dir / "upload" / f"{sha}.zip"
    up.parent.mkdir(parents=True, exist_ok=True)
    if not up.exists():
        shutil.copy2(raw, up)
    return sha, size, warns


# --------------------------------------------------------------------------- github release upload
def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def upload_to_github(repo, tag, files):
    if not shutil.which("gh"):
        sys.exit("`gh` CLI not found. Install from https://cli.github.com and run `gh auth login`.")
    try:
        existing = set(gh("release", "view", tag, "-R", repo, "--json", "assets", "-q", ".assets[].name").split())
    except subprocess.CalledProcessError:
        print(f"Creating release '{tag}' on {repo} ...")
        gh("release", "create", tag, "-R", repo, "--title", "Resource packs",
           "--notes", "Hash-named resource pack zips (immutable). Do not rename.")
        existing = set()
    todo = [f for f in files if f.name not in existing]
    print(f"GitHub: {len(existing)} already uploaded, {len(todo)} to upload")
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        gh("release", "upload", tag, "-R", repo, *map(str, batch))
        print(f"  uploaded {', '.join(f.name[:8] for f in batch)}")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="packs.yml")
    ap.add_argument("--out-dir", default="packs")
    ap.add_argument("--repo", help="GitHub repo as owner/name (builds release URLs)")
    ap.add_argument("--tag", default="packs", help="GitHub release tag (default: packs)")
    ap.add_argument("--upload", action="store_true", help="upload to the GitHub release via gh CLI")
    ap.add_argument("--base-url", help="other host: new url = BASE_URL + <sha1>.zip")
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached in out-dir/raw")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    entries = parse_config(Path(args.config).read_text(encoding="utf-8"))
    if not entries:
        sys.exit(f"No entries found in {args.config}")
    out_dir = Path(args.out_dir)
    print(f"Processing {len(entries)} packs with {args.workers} workers...\n")

    failures = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, e, out_dir, args.refresh): e for e in entries}
        for fut in as_completed(futs):
            e = futs[fut]
            try:
                e["sha1"], e["size"], e["warns"] = fut.result()
                print(f"  OK   {e['name']:<22} {e['size'] / 1e6:6.2f} MB  {e['sha1']}")
                for w in e["warns"]:
                    print(f"       WARN {w}")
            except Exception as err:  # noqa: BLE001
                e["error"] = str(err)
                failures.append(e)
                print(f"  FAIL {e['name']:<22} {err}")

    ok = [e for e in entries if "error" not in e]

    # new urls
    for e in entries:
        if "error" in e:
            e["new_url"], e["sha1"] = e["url"], ""   # keep old url, leave hash empty
        elif args.repo:
            e["new_url"] = f"https://github.com/{args.repo}/releases/download/{args.tag}/{e['sha1']}.zip"
        elif args.base_url:
            e["new_url"] = args.base_url.rstrip("/") + f"/{e['sha1']}.zip"
        else:
            e["new_url"] = e["url"]

    if args.upload:
        if not args.repo:
            sys.exit("--upload needs --repo owner/name")
        upload_to_github(args.repo, args.tag, [out_dir / "upload" / f"{e['sha1']}.zip" for e in ok])

    new_yaml = render_yaml(entries)
    new_path = Path(args.config).with_suffix(".new.yml")
    new_path.write_text(new_yaml, encoding="utf-8")

    total = sum(e.get("size", 0) for e in ok)
    print(f"\n{'=' * 70}\nSUMMARY: {len(ok)}/{len(entries)} ok, {len(failures)} failed, {total / 1e6:.1f} MB total")
    if failures:
        print("FAILED (fix these manually, old url kept, sha1 empty):")
        for e in failures:
            print(f"  - {e['name']}: {e['error']}")
    if not (args.repo or args.base_url):
        print("\nNOTE: no --repo/--base-url given, so urls below are unchanged (hashes only).")
    elif not args.upload:
        print(f"\nNOTE: files to host are in {out_dir / 'upload'}/ - upload them (or re-run with --upload).")
    print(f"\n{'=' * 70}\nNEW CONFIG (also saved to {new_path}):\n{'=' * 70}\n")
    print(new_yaml)


if __name__ == "__main__":
    main()
