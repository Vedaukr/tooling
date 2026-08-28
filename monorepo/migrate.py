#!/usr/bin/env python3
"""
Merge many GitLab repos into one monorepo, preserving commit history.

Prereqs:
    pip install git-filter-repo
    git config --global core.longpaths true
    git config --global core.autocrlf false

Usage:
    python migrate.py                # full run: rewrite -> merge -> generate CI
    python migrate.py --ci-only      # regenerate .gitlab-ci.yml files only
    python migrate.py --dry-run      # print the plan, touch nothing

Layout assumed:
    mirrors/    output of the pull-all script (bare --mirror clones)
    templates/  docker.yml, module.yml
    migration.json
    work/       scratch, safe to delete
    monorepo/   the result
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIRRORS = ROOT / "mirrors"
WORK = ROOT / "work"
MONO = ROOT / "monorepo"
TEMPLATES = ROOT / "templates"
CONFIG = ROOT / "migration.json"

DEFAULT_BRANCH = "main"
KEEP_TAGS = False  # True => tags are kept, namespaced as <name>/<tag>


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def git(*args, cwd=None, check=True):
    p = subprocess.run(
        ["git", *args], cwd=cwd, text=True,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)}\n  cwd: {cwd}\n  {p.stdout}\n  {p.stderr}"
        )
    return p.stdout.strip()


def log(msg):
    print(f"  {msg}", flush=True)


def load_config():
    with CONFIG.open(encoding="utf-8") as f:
        cfg = json.load(f)

    seen_paths = {}
    for repo, c in cfg.items():
        if not c.get("include_in_migration"):
            continue
        if c.get("type") not in ("docker", "module"):
            sys.exit(f"{repo}: type must be 'docker' or 'module', got {c.get('type')!r}")
        path = c.get("path") or default_path(repo)
        c["path"] = path.strip("/")
        if path in seen_paths:
            sys.exit(f"path collision: {repo} and {seen_paths[path]} both map to {path}")
        seen_paths[path] = repo
        c.setdefault("params", {})
    return cfg


def default_path(repo_dir):
    """myorg_group_service.git -> service"""
    stem = repo_dir[:-4] if repo_dir.endswith(".git") else repo_dir
    return stem.split("_")[-1].replace(".", "-")


def selected(cfg):
    return {k: v for k, v in cfg.items() if v.get("include_in_migration")}


# --------------------------------------------------------------------------- #
# step 1: rewrite each mirror so its content lives under its target path
# --------------------------------------------------------------------------- #

def detect_branch(repo):
    """Mirrors preserve HEAD, so this is usually exact."""
    head = git("symbolic-ref", "--quiet", "HEAD", cwd=repo, check=False)
    if head.startswith("refs/heads/"):
        name = head[len("refs/heads/"):]
        if git("rev-parse", "--verify", "--quiet", head, cwd=repo, check=False):
            return name
    for candidate in ("main", "master", "develop", "trunk"):
        if git("rev-parse", "--verify", "--quiet",
               f"refs/heads/{candidate}", cwd=repo, check=False):
            return candidate
    heads = [r for r in git("for-each-ref", "--format=%(refname:short)",
                            "refs/heads", cwd=repo).splitlines() if r]
    if not heads:
        raise RuntimeError(f"{repo}: no branches at all")
    return heads[0]


def prune_refs(repo, keep_branch, name):
    """Delete every ref except the branch we're keeping. Optionally namespace tags."""
    refs = [r for r in git("for-each-ref", "--format=%(refname)", cwd=repo).splitlines() if r]
    keep = f"refs/heads/{keep_branch}"
    deleted = 0

    for ref in refs:
        if ref == keep:
            continue
        if KEEP_TAGS and ref.startswith("refs/tags/"):
            continue
        git("update-ref", "-d", ref, cwd=repo)
        deleted += 1

    if KEEP_TAGS:
        for ref in refs:
            if not ref.startswith("refs/tags/"):
                continue
            tag = ref[len("refs/tags/"):]
            sha = git("rev-parse", ref, cwd=repo)
            git("update-ref", f"refs/tags/{name}/{tag}", sha, cwd=repo)
            git("update-ref", "-d", ref, cwd=repo)

    if keep_branch != DEFAULT_BRANCH:
        sha = git("rev-parse", keep, cwd=repo)
        git("update-ref", f"refs/heads/{DEFAULT_BRANCH}", sha, cwd=repo)
        git("update-ref", "-d", keep, cwd=repo)
        git("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}", cwd=repo)

    return deleted


def rewrite(repo_dir, conf):
    src = MIRRORS / repo_dir
    if not src.is_dir():
        sys.exit(f"missing mirror: {src}")

    dst = WORK / repo_dir
    if dst.exists():
        shutil.rmtree(dst, onerror=force_remove)
    shutil.copytree(src, dst)

    branch = detect_branch(dst)
    n = prune_refs(dst, branch, conf["path"].replace("/", "-"))
    log(f"branch={branch}  pruned {n} refs")

    subprocess.run(
        [sys.executable, "-m", "git_filter_repo",
         "--to-subdirectory-filter", conf["path"], "--force"],
        cwd=dst, check=True,
    )

    count = git("rev-list", "--count", DEFAULT_BRANCH, cwd=dst)
    log(f"rewrote into {conf['path']}/  ({count} commits)")


def force_remove(func, path, _exc):
    """Windows: .git objects are read-only, shutil.rmtree chokes on them."""
    import os, stat
    os.chmod(path, stat.S_IWRITE)
    func(path)


# --------------------------------------------------------------------------- #
# step 2: merge everything into one repo
# --------------------------------------------------------------------------- #

def init_monorepo():
    if MONO.exists():
        shutil.rmtree(MONO, onerror=force_remove)
    MONO.mkdir(parents=True)
    git("init", f"--initial-branch={DEFAULT_BRANCH}", cwd=MONO)
    git("config", "core.autocrlf", "false", cwd=MONO)
    git("config", "core.longpaths", "true", cwd=MONO)
    (MONO / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    (MONO / "README.md").write_text("# Monorepo\n", encoding="utf-8")
    git("add", "-A", cwd=MONO)
    git("commit", "-m", "chore: initialise monorepo", cwd=MONO)


def merge(repo_dir, conf):
    remote = conf["path"].replace("/", "-")
    src = (WORK / repo_dir).resolve()
    git("remote", "add", remote, src.as_posix(), cwd=MONO)
    git("fetch", "--no-tags" if not KEEP_TAGS else "--tags", remote, cwd=MONO)
    git("merge", "--allow-unrelated-histories", "--no-ff",
        "-m", f"chore: merge {repo_dir} into {conf['path']}",
        f"{remote}/{DEFAULT_BRANCH}", cwd=MONO)
    git("remote", "remove", remote, cwd=MONO)
    log(f"merged -> {conf['path']}/")


# --------------------------------------------------------------------------- #
# step 3: generate CI files
# --------------------------------------------------------------------------- #

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(template_text, values, where):
    missing = set()

    def sub(m):
        key = m.group(1)
        if key not in values:
            missing.add(key)
            return m.group(0)
        return str(values[key])

    out = PLACEHOLDER.sub(sub, template_text)
    if missing:
        sys.exit(f"{where}: template needs params {sorted(missing)}")
    return out


def generate_ci(cfg):
    cache = {}
    includes = []

    for repo_dir, conf in selected(cfg).items():
        kind = conf["type"]
        if kind not in cache:
            tpl = TEMPLATES / f"{kind}.yml"
            if not tpl.is_file():
                sys.exit(f"missing template: {tpl}")
            cache[kind] = tpl.read_text(encoding="utf-8")

        values = dict(conf["params"])
        values.setdefault("path", conf["path"])
        values.setdefault("name", conf["path"].replace("/", "-"))

        target_dir = MONO / conf["path"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / ".gitlab-ci.yml"
        target.write_text(render(cache[kind], values, repo_dir), encoding="utf-8")
        includes.append(f"{conf['path']}/.gitlab-ci.yml")
        log(f"{conf['path']}/.gitlab-ci.yml")

    root = ["stages:", "  - build", "  - package", "", "include:"]
    root += [f"  - local: {p}" for p in sorted(includes)]
    (MONO / ".gitlab-ci.yml").write_text("\n".join(root) + "\n", encoding="utf-8")
    log(".gitlab-ci.yml (root)")


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci-only", action="store_true",
                    help="regenerate CI files in an existing monorepo")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    chosen = selected(cfg)
    skipped = len(cfg) - len(chosen)

    print(f"{len(chosen)} repos to migrate, {skipped} skipped\n")
    for repo_dir, conf in chosen.items():
        print(f"  {repo_dir:45} -> {conf['path']:30} [{conf['type']}]")
    print()

    if args.dry_run:
        return

    if args.ci_only:
        if not (MONO / ".git").is_dir():
            sys.exit("no monorepo yet — run without --ci-only first")
        print("== generating CI ==")
        generate_ci(cfg)
        git("add", "-A", cwd=MONO)
        if git("status", "--porcelain", cwd=MONO):
            git("commit", "-m", "ci: regenerate pipeline definitions", cwd=MONO)
            print("\ncommitted")
        else:
            print("\nno changes")
        return

    WORK.mkdir(exist_ok=True)

    print("== rewriting history ==")
    for repo_dir, conf in chosen.items():
        print(f"{repo_dir}")
        rewrite(repo_dir, conf)

    print("\n== merging ==")
    init_monorepo()
    for repo_dir, conf in chosen.items():
        merge(repo_dir, conf)

    print("\n== generating CI ==")
    generate_ci(cfg)
    git("add", "-A", cwd=MONO)
    git("commit", "-m", "ci: add generated pipeline definitions", cwd=MONO)

    total = git("rev-list", "--count", DEFAULT_BRANCH, cwd=MONO)
    print(f"\ndone — {MONO} has {total} commits on {DEFAULT_BRANCH}")
    print("verify with:  git -C monorepo log --oneline --graph | head -40")


if __name__ == "__main__":
    main()
