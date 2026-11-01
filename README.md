# mc-pack-host

One script that takes your pack config, downloads every pack, verifies it, computes SHA-1s,
re-hosts it on a GitHub Release, and prints a ready-to-paste config with new URLs + hashes.

No dependencies besides Python 3.8+ (and the [`gh` CLI](https://cli.github.com) if you want auto-upload).

## What it does per pack

1. Downloads it (4 retries, browser User-Agent).
2. Rejects non-zips. This catches Google Drive "quota exceeded" / confirmation HTML pages
   that would otherwise get hashed as if they were a pack.
3. Warns if `pack.mcmeta` is not at the zip root (Minecraft rejects those).
4. Computes SHA-1 + size.
5. Saves it as `packs/upload/<sha1>.zip` (hash-named, so it is immutable and cacheable forever).
6. Optionally uploads it to a GitHub Release.
7. Prints a summary and the new YAML (also written to `packs.new.yml`).

## Usage

```bash
# 1. Create a public repo, e.g. you/mc-packs (the release assets must be public).
gh auth login

# 2. Put your config in packs.yml (already filled in with your 27 packs).

# 3a. Dry run: download + hash only, URLs unchanged
python sync_packs.py

# 3b. Full run: upload to the GitHub Release and print the new config
python sync_packs.py --repo you/mc-packs --upload
```

Copy the YAML printed at the bottom (or `packs.new.yml`) over your server config.

### Other hosts (R2, Bunny, your own nginx)

```bash
python sync_packs.py --base-url https://packs.example.com/
# then upload everything in packs/upload/ to that host
# e.g. rclone copy packs/upload r2:my-bucket/
```

## Options

| Flag | Meaning |
|------|---------|
| `--config` | Input YAML (default `packs.yml`) |
| `--out-dir` | Working dir (default `packs`) |
| `--repo owner/name` | Build GitHub Release URLs |
| `--tag` | Release tag (default `packs`) |
| `--upload` | Upload via `gh` (skips files already on the release) |
| `--base-url` | Use another host for the new URLs |
| `--refresh` | Re-download even if cached locally |
| `--workers` | Parallel downloads (default 4) |

## Notes

- Re-running is safe and cheap: downloads are cached in `packs/raw/` and already-uploaded assets are skipped.
- If a pack fails, its old URL is kept and `sha1` is left empty in the output, and it is listed in the summary.
- GitHub Releases is fine for a small server, but it is not a CDN. If you grow, upload the same
  `packs/upload/` folder to Cloudflare R2 with a custom domain and rerun with `--base-url`.
