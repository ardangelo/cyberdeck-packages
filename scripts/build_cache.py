#!/usr/bin/env python3
"""Compute content-addressed keys for unprivileged package build caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from release_support import release_number

SELECTORS = {"status-ready", "cyberdeck-service", "sharp-drm"}
# Publication, verification and target-runtime programs never execute in a package build.
NON_BUILD_SCRIPTS = {
    "archive-release.py", "github_api.py", "publish-repository.py",
    "verify-artifacts.py", "smoke-packages.py", "smoke-apt.py",
}


def _files(root: Path, directories: tuple[str, ...]) -> list[Path]:
    """Include tracked and untracked non-ignored working-tree inputs, not Git blob IDs."""
    command = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
               "--exclude-standard", "--", *directories]
    entries = subprocess.check_output(command).split(b"\0")
    paths = {Path(os.fsdecode(name)) for name in entries if name}
    return sorted(paths, key=lambda p: os.fsencode(p.as_posix()))


def _digest_files(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        name = os.fsencode(relative.as_posix())
        metadata = path.lstat() if path.is_symlink() or path.exists() else None
        digest.update(name + b"\0")
        if metadata is None:
            # A deleted tracked build input must not retain its previous cache key.
            digest.update(b"deleted\0")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)) + b"\0")
            path = path.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"build input symlink must resolve to a file inside the checkout: {relative}")
            metadata = path.stat()
        if stat.S_ISREG(metadata.st_mode):
            digest.update(f"file:{stat.S_IMODE(metadata.st_mode):04o}".encode() + b"\0")
            content = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content.update(chunk)
            digest.update(content.digest())
            digest.update(b"\0")
        else:
            raise ValueError(f"unsupported build input: {relative}")
    return digest.hexdigest()


def _key(label: str, inputs: dict) -> str:
    raw = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"cyberdeck-{label}-v1-{hashlib.sha256(raw).hexdigest()}"



def _image_key(root: Path, lock: dict, kind: str) -> str:
    dockerfile = _digest_files(root, _files(root, (f"containers/{kind}.Dockerfile",)))
    if kind == "kernel":
        arguments = {"base": lock["containers"]["kernel"], "toolchain": lock["toolchain"]}
    else:
        arguments = {
            "base": lock["containers"]["service"],
            "snapshot": lock["debian_snapshot"],
            "tools": {name: lock["tools"][name] for name in
                      ("temurin17", "android_commandline", "android_platform36")},
        }
    return _key("image", {"kind": kind, "dockerfile": dockerfile, "args": arguments})

def cache_keys(root: Path, selector: str, number: int, lock: dict) -> dict[str, str]:
    """Derive reusable build products from the exact selected local and locked inputs."""
    if selector not in SELECTORS:
        raise ValueError(f"unknown package selector: {selector}")
    if type(number) is not int or number < 0:
        raise ValueError("invalid release number")
    root = Path(root).resolve()
    kind = "kernel" if selector == "sharp-drm" else "service"
    scripts = [p for p in _files(root, ("scripts",))
               if p.parts[0] == "scripts" and p.as_posix().removeprefix("scripts/") not in NON_BUILD_SCRIPTS]
    helpers = _digest_files(root, scripts)
    image = _image_key(root, lock, kind)
    kernel = _key("kernel", {
        "sdk": lock["sdk"], "toolchain": lock["toolchain"],
        "image": _image_key(root, lock, "kernel"), "helpers": helpers,
    })
    recipes = _digest_files(root, _files(root, (f"packages/{selector}",))) if selector != "cyberdeck-service" else None
    package = _key("package", {"selector": selector, "number": number, "lock": lock,
                               "image": image, "recipes": recipes, "helpers": helpers})
    # Gradle state is not a package artifact. Its prefix may cross service revisions
    # and release numbers, while strict dependency verification protects restored bytes.
    gradle_restore_prefix = _key("gradle-toolchain", {
        "image": _image_key(root, lock, "service"),
        "gradle_distribution": lock["tools"]["gradle"],
    }) + "-"
    gradle = gradle_restore_prefix + hashlib.sha256(json.dumps(lock["service"], sort_keys=True).encode()).hexdigest()
    return {"package": package, "image": image, "kernel": kernel,
            "gradle": gradle, "gradle_restore_prefix": gradle_restore_prefix}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=sorted(SELECTORS), required=True)
    parser.add_argument("--release-number", required=True)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.development and os.environ.get("CI") != "true":
        parser.error("development release requires CI")
    try:
        number = release_number(args.release_number, development=args.development)
        root = Path(__file__).resolve().parents[1]
        lock = json.loads((root / "source-lock.json").read_text())
        keys = cache_keys(root, args.package, number, lock)
        if args.github_output:
            with args.github_output.open("a") as destination:
                for name, value in keys.items():
                    destination.write(f"{name}={value}\n")
        else:
            print(json.dumps(keys, sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"build-cache: {error}\n")


if __name__ == "__main__":
    main()
