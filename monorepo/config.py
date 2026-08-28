#!/usr/bin/env python3
"""
Scan a directory of mirror clones and emit migration.json.

Folder names are expected as:  company_group_reponame.git

Derived fields per repo:
    repo_name         folder name, minus '.git', minus the first N '_' segments
    module_short_name repo_name split on '.', first 2 parts dropped, rejoined
                      with '.'; falls back to repo_name if <= 2 parts
    service_cmd       repo_name with each segment's first letter uppercased,
                      plus '.Cmd'
    path              destination inside the monorepo (kebab-cased short name)

Usage:
    python build_config.py                      # scan ., write migration.json
    python build_config.py --dir mirrors        # scan somewhere else
    python build_config.py --drop-prefix 3      # deeper subgroup nesting
    python build_config.py --merge              # keep hand-edits in existing file
    python build_config.py --stdout             # print, don't write
"""

import argparse
import json
import re
import sys
from pathlib import Path

SEGMENT_START = re.compile(r"(^|[._\-])([a-z])")


def repo_name_from(folder: str, drop_prefix: int) -> str:
    """company_group_Company.Risk.Ingest.git -> Company.Risk.Ingest"""
    stem = folder[:-4] if folder.endswith(".git") else folder
    parts = stem.split("_")
    if len(parts) > drop_prefix:
        return "_".join(parts[drop_prefix:])
    return parts[-1]          # not enough segments — take what's there


def module_short_name(repo_name: str) -> str:
    """Company.Risk.Ingest.Api -> Ingest.Api ; Company.Risk -> Company.Risk"""
    parts = repo_name.split(".")
    if len(parts) <= 2:
        return repo_name
    return ".".join(parts[2:])


def service_cmd(repo_name: str) -> str:
    """company.risk.ingest -> Company.Risk.Ingest.Cmd (existing caps preserved)"""
    capped = SEGMENT_START.sub(lambda m: m.group(1) + m.group(2).upper(), repo_name)
    return f"{capped}.Cmd"


def to_path(short_name: str) -> str:
    """Ingest.Api -> services/ingest-api"""
    slug = re.sub(r"[._\s]+", "-", short_name).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return f"services/{slug}"


def build(scan_dir: Path, drop_prefix: int) -> dict:
    folders = sorted(
        d.name for d in scan_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        sys.exit(f"no directories found in {scan_dir}")

    cfg = {}
    for folder in folders:
        repo = repo_name_from(folder, drop_prefix)
        short = module_short_name(repo)
        cfg[folder] = {
            "include_in_migration": True,
            "type": "docker",
            "path": to_path(short),
            "params": {
                "module_short_name": short,
                "repo_name": repo,
                "service_cmd": service_cmd(repo),
            },
        }
    return cfg


def merge_existing(new: dict, path: Path) -> dict:
    """Preserve hand-edits: existing entries win, new folders get appended."""
    if not path.is_file():
        return new
    old = json.loads(path.read_text(encoding="utf-8"))
    merged, added, kept = {}, 0, 0
    for folder, entry in new.items():
        if folder in old:
            merged[folder] = old[folder]
            kept += 1
        else:
            merged[folder] = entry
            added += 1
    dropped = [f for f in old if f not in new]
    print(f"merge: {kept} kept, {added} added, {len(dropped)} no longer on disk")
    for f in dropped:
        print(f"  dropped: {f}")
    return merged


def report(cfg: dict):
    w1 = max((len(k) for k in cfg), default=10)
    w2 = max((len(v["params"]["repo_name"]) for v in cfg.values()), default=10)
    w3 = max((len(v["params"]["module_short_name"]) for v in cfg.values()), default=10)
    print(f"\n{'folder'.ljust(w1)}  {'repo_name'.ljust(w2)}  "
          f"{'module_short_name'.ljust(w3)}  service_cmd")
    print("-" * (w1 + w2 + w3 + 30))
    for folder, entry in cfg.items():
        p = entry["params"]
        print(f"{folder.ljust(w1)}  {p['repo_name'].ljust(w2)}  "
              f"{p['module_short_name'].ljust(w3)}  {p['service_cmd']}")

    paths = {}
    for folder, entry in cfg.items():
        paths.setdefault(entry["path"], []).append(folder)
    clashes = {p: f for p, f in paths.items() if len(f) > 1}
    if clashes:
        print("\nPATH COLLISIONS — fix these before running migrate.py:")
        for p, folders in clashes.items():
            print(f"  {p} <- {', '.join(folders)}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".", help="directory of mirror clones")
    ap.add_argument("--out", default="migration.json")
    ap.add_argument("--drop-prefix", type=int, default=2,
                    help="how many leading '_' segments are company/group")
    ap.add_argument("--merge", action="store_true",
                    help="keep entries already present in --out")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    scan_dir = Path(args.dir).resolve()
    if not scan_dir.is_dir():
        sys.exit(f"not a directory: {scan_dir}")

    cfg = build(scan_dir, args.drop_prefix)
    out = Path(args.out)
    if args.merge:
        cfg = merge_existing(cfg, out)

    report(cfg)
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"

    if args.stdout:
        print(text)
    else:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} ({len(cfg)} repos)")


if __name__ == "__main__":
    main()
