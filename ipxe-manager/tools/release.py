#!/usr/bin/env python3
"""Prepare a local Git commit and annotated tag for an approved release."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"Command failed: {' '.join(args)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local iPXE project release commit and tag")
    parser.add_argument("version", help="Semantic version without the v prefix, for example 1.2.0")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("version must use MAJOR.MINOR.PATCH, for example 1.2.0")
    tag = f"v{args.version}"
    try:
        if run("git", "diff", "--quiet", check=False).returncode or run("git", "diff", "--cached", "--quiet", check=False).returncode:
            raise RuntimeError("Please finish or commit current changes first")
        if run("git", "tag", "--list", tag).stdout.strip():
            raise RuntimeError(f"Tag already exists: {tag}")
        subprocess.run([sys.executable, "tools/build_ipxe_packages.py"], cwd=ROOT, check=True)
        paths = [
            ".gitignore", "CHANGELOG.md", "RELEASE.md", "README.md", "MANUAL_DEPLOYMENT_TRAINING.md",
            "VERIFICATION.md", "ipxe-manager-函数执行顺序.md", "config.example.json", "install.py",
            "ipxe-manager.py", "ipxe-verify.py",
            "tools", "packages",
        ]
        run("git", "add", "--", *paths)
        if run("git", "diff", "--cached", "--quiet", check=False).returncode == 0:
            raise RuntimeError("There are no release changes to commit")
        run("git", "commit", "-m", f"release: {tag}")
        run("git", "tag", "-a", tag, "-m", f"Release {tag}")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Created local Git commit and tag: {tag}")
    print("Push only after release approval: git push origin HEAD --tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
