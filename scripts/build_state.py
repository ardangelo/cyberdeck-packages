"""Integrity-checked build outputs, Docker images and prepared SDK snapshots.

Only a missing key is a cache miss. An existing incomplete or changed entry is
an error; the caller must not quietly rebuild over untrusted state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

from release_support import inspect_deb, require, sha256, validate_context

HEX = re.compile(r"[0-9a-f]{64}\Z")

def _load(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), f"missing cache metadata: {path}")
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f"invalid cache metadata: {path}")
    return value


def _regular(path: Path, digest: str, size: int) -> None:
    require(isinstance(digest, str) and HEX.fullmatch(digest) is not None and
            type(size) is int and size >= 0 and path.is_file() and not path.is_symlink() and
            path.stat().st_size == size and sha256(path) == digest,
            f"missing or corrupt cached file: {path}")


def _files(directory: Path, expected: set[str]) -> None:
    require(directory.is_dir() and not directory.is_symlink() and
            {path.name for path in directory.iterdir()} == expected,
            f"unexpected or missing cache files in {directory}")


def _new(cache: Path) -> Path:
    cache.parent.mkdir(parents=True, exist_ok=True)
    require(not cache.exists() and not cache.is_symlink(), f"cache entry already exists: {cache}")
    return Path(tempfile.mkdtemp(prefix=f".{cache.name}-", dir=cache.parent))


def _finish(staging: Path, cache: Path) -> None:
    # This is only called after the complete entry is written. The cache key
    # directory is deliberately never repaired/replaced in place.
    require(not cache.exists() and not cache.is_symlink(), f"cache entry already exists: {cache}")
    os.rename(staging, cache)


def _names(record: dict, selector: str, number: int, lock: dict, packages: dict) -> set[str]:
    from release_support import basename
    require(type(record.get("schema_version")) is int and record["schema_version"] == 1 and
            record.get("package") == selector and
            type(record.get("release_number")) is int and record["release_number"] == number and
            record.get("conf") == lock["sdk"]["conf"] and
            isinstance(record.get("sources"), dict) and record["sources"].get("lock") == lock and
            type(record.get("source_date_epoch")) is int and record["source_date_epoch"] > 0,
            "cached package build metadata does not match selected inputs")
    require(isinstance(record.get("sources", {}).get("submodules"), list), "invalid cached gitlink provenance")
    if selector != "cyberdeck-service":
        require(record["sources"]["submodules"] == [], "unexpected source gitlinks")
    artifacts = record.get("artifacts")
    require(isinstance(artifacts, list) and artifacts, "cached package has no artifacts")
    seen: set[str] = set()
    binaries: set[str] = set()
    sources = 0
    changes = 0
    buildinfo = 0
    if selector == "sharp-drm":
        marker = record.get("kernel_context")
        require(isinstance(marker, dict) and set(marker) == {"file", "sha256"} and
                marker["file"] == "kernel-context.json" and
                isinstance(marker["sha256"], str) and HEX.fullmatch(marker["sha256"]) is not None,
                "missing cached kernel context")
        seen.add(marker["file"])
    else:
        require("kernel_context" not in record, "unexpected cached kernel context")
    for item in artifacts:
        require(isinstance(item, dict), "invalid cached artifact record")
        name = basename(item.get("file"))
        require(name not in seen, f"duplicate cached file: {name}")
        seen.add(name)
        kind = item.get("kind")
        require(kind in {"deb", "source", "changes", "buildinfo"} and
                isinstance(item.get("sha256"), str) and HEX.fullmatch(item["sha256"]) is not None and
                type(item.get("size")) is int and 0 < item["size"] < 2**31,
                f"invalid cached artifact: {name}")
        if kind == "source":
            require(name.endswith((".dsc", ".gz", ".xz")), f"invalid cached source suffix: {name}")
        else:
            require(name.endswith({"deb": ".deb", "changes": ".changes", "buildinfo": ".buildinfo"}[kind]),
                    f"invalid cached artifact suffix: {name}")
        if kind == "deb":
            require(set(item) == {"file", "sha256", "size", "kind", "package", "version", "architecture", "depends"}, "invalid binary metadata fields")
            package = item["package"]
            require(package in packages[selector][2] and package not in binaries and
                    item["architecture"] == packages[selector][2][package], "cached binary package set mismatch")
            expected_version = f"{packages[selector][1]}+cyberdeck.{number}"
            if selector == "sharp-drm":
                # Read the hashed context below before finalizing the ABI suffix.
                require(item["version"].startswith(expected_version + "+abi."), "cached binary release mismatch")
            else:
                require(item["version"] == expected_version + "-1", "cached binary release mismatch")
            binaries.add(package)
        else:
            require(set(item) == {"file", "sha256", "size", "kind"}, "invalid source metadata fields")
            sources += kind == "source"
            changes += kind == "changes"
            buildinfo += kind == "buildinfo"
    require(binaries == set(packages[selector][2]) and sources == (4 if selector == "cyberdeck-service" else 3) and
            changes == 2 and buildinfo == 2,
            "cached source/binary artifact set is incomplete")
    return seen


def load_package(cache: Path, selected: Path | None, selector: str, number: int, lock: dict, packages: dict) -> dict:
    record = _load(cache / f"{selector}-build.json")
    names = _names(record, selector, number, lock, packages)
    _files(cache, names | {f"{selector}-build.json"})
    for item in record["artifacts"]:
        path = cache / item["file"]
        _regular(path, item["sha256"], item["size"])
        if item["kind"] == "deb":
            require(inspect_deb(path) == {field: item[field] for field in ("package", "version", "architecture", "depends")},
                    f"cached Debian control differs from record: {path.name}")
    if selector == "sharp-drm":
        marker = cache / "kernel-context.json"
        require(marker.is_file() and not marker.is_symlink(), "missing cached kernel context")
        _regular(marker, record["kernel_context"]["sha256"], marker.stat().st_size)
        context = validate_context(json.loads(marker.read_text()))
        require(context["conf"] == lock["sdk"]["conf"], "cached kernel configuration mismatch")
        version = f"1.7+cyberdeck.{number}+abi.{context['abi_sha256']}-1"
        require(all(item["version"] == version for item in record["artifacts"] if item["kind"] == "deb"),
                "cached Sharp binary ABI/version mismatch")
    else:
        version = f"{packages[selector][1]}+cyberdeck.{number}-1"
    source_name = packages[selector][0]
    upstream = version.rsplit("-", 1)[0]
    artifact_names = {item["file"] for item in record["artifacts"]}
    expected_sources = {f"{source_name}_{version}.dsc", f"{source_name}_{version}.debian.tar.xz",
                        f"{source_name}_{upstream}.orig.tar.gz"}
    if selector == "cyberdeck-service":
        expected_sources.add(f"{source_name}_{upstream}.corresponding-sources.tar.gz")
    require({item["file"] for item in record["artifacts"] if item["kind"] == "source"} == expected_sources and
            all(name.startswith(f"{source_name}_{version}_") for name in artifact_names
                if name.endswith((".changes", ".buildinfo"))),
            "cached source and build metadata does not match selected release")
    if selected is not None:
        selected.mkdir(parents=True, exist_ok=True)
        for name in sorted(names | {f"{selector}-build.json"}):
            shutil.copy2(cache / name, selected / name)
    return record


def save_package(cache: Path, selected: Path, selector: str, number: int, lock: dict, packages: dict) -> None:
    # Independently inspect the completed package before publishing a reusable entry.
    load_package(selected, None, selector, number, lock, packages)
    staging = _new(cache)
    try:
        for file in selected.iterdir():
            shutil.copy2(file, staging / file.name)
        _finish(staging, cache)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_image(cache: Path, key: str, kind: str, lock: dict, image: str, run) -> None:
    record = _load(cache / "state.json")
    require(type(record.get("schema_version")) is int and record["schema_version"] == 1 and
            set(record) == {"schema_version", "key", "kind", "container", "image",
                            "image_id", "archive_sha256", "archive_size"} and
            record["key"] == key and record["kind"] == kind and
            record["container"] == lock["containers"][kind] and record["image"] == image,
            "cached Docker identity mismatch")
    require(isinstance(record["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", record["image_id"]), "invalid cached Docker image ID")
    require(type(record["archive_size"]) is int and record["archive_size"] > 0,
            "invalid cached Docker archive size")
    _files(cache, {"state.json", "image.tar"})
    _regular(cache / "image.tar", record["archive_sha256"], record["archive_size"])
    run(["docker", "load", "--input", str(cache / "image.tar")])
    require(run(["docker", "image", "inspect", "--format={{.Id}}", image], capture=True) == record["image_id"],
            "loaded Docker image identity mismatch")


def save_image(cache: Path, key: str, kind: str, lock: dict, image: str, run) -> None:
    image_id = run(["docker", "image", "inspect", "--format={{.Id}}", image], capture=True)
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None, "invalid built Docker image ID")
    staging = _new(cache)
    try:
        run(["docker", "save", "--output", str(staging / "image.tar"), image])
        archive = staging / "image.tar"
        (staging / "state.json").write_text(json.dumps({"schema_version": 1, "key": key, "kind": kind,
            "container": lock["containers"][kind], "image": image, "image_id": image_id,
            "archive_sha256": sha256(archive), "archive_size": archive.stat().st_size}, sort_keys=True) + "\n")
        _finish(staging, cache)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _snapshot_paths(sdk: Path, conf: str, context: dict) -> list[Path]:
    output = sdk / "output"
    configured = output / conf
    kernel = configured / "build" / f"linux-{context['kernel_source_revision']}"
    required = (".config", "include/config/auto.conf", "include/generated/utsrelease.h",
                "scripts/mod/modpost", "Module.symvers")
    require(kernel.is_dir() and not kernel.is_symlink() and
            all((kernel / name).is_file() for name in required),
            "kernel cache requires a complete configured build")
    require(sha256(kernel / ".config") == context["kernel_config_sha256"] and
            sha256(kernel / "Module.symvers") == context["module_symvers_sha256"],
            "prepared kernel configuration/symbols differ from cached context")
    paths = [output / "buildroot-2025.02.1", configured / "Makefile", configured / ".config", kernel]
    paths += sorted(configured.glob(".br2-external*"))
    config_helpers = configured / "build/buildroot-config"
    if config_helpers.exists():
        paths.append(config_helpers)
    require(all((path.exists() or path.is_symlink()) and
                path.parent.resolve().is_relative_to(output.resolve()) for path in paths),
            "missing or unsafe prepared SDK snapshot input")
    return paths


def save_kernel(cache: Path, key: str, sdk: Path, conf: str, context: dict, lock: dict) -> None:
    context = validate_context(context)
    require(context["conf"] == conf, "kernel snapshot configuration mismatch")
    paths = _snapshot_paths(sdk, conf, context)
    staging = _new(cache)
    try:
        archive = staging / "sdk.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=False, compresslevel=1) as output:
            for path in paths:
                output.add(path, arcname=path.relative_to(sdk).as_posix(), recursive=True)
        (staging / "state.json").write_text(json.dumps({"schema_version": 1, "key": key, "conf": conf,
            "sdk": lock["sdk"], "toolchain": lock["toolchain"], "context": context,
            "archive_sha256": sha256(archive), "archive_size": archive.stat().st_size}, sort_keys=True) + "\n")
        _finish(staging, cache)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_kernel(cache: Path, key: str, sdk: Path, conf: str, lock: dict) -> dict:
    record = _load(cache / "state.json")
    require(type(record.get("schema_version")) is int and record["schema_version"] == 1 and
            set(record) == {"schema_version", "key", "conf", "sdk", "toolchain", "context",
                            "archive_sha256", "archive_size"} and
            record.get("key") == key and record.get("conf") == conf and
            record.get("sdk") == lock["sdk"] and record.get("toolchain") == lock["toolchain"],
            "cached SDK inputs mismatch")
    context = validate_context(record["context"])
    require(context["conf"] == conf, "cached kernel configuration mismatch")
    _files(cache, {"state.json", "sdk.tar.gz"})
    archive = cache / "sdk.tar.gz"
    require(type(record["archive_size"]) is int and record["archive_size"] > 0,
            "invalid cached SDK archive size")
    _regular(archive, record.get("archive_sha256"), record.get("archive_size"))
    require(not (sdk / "output").exists(), "cannot overlay existing SDK build output")
    with tarfile.open(archive, "r:gz") as input_archive:
        names: set[str] = set()
        symlinks: set[str] = set()
        allowed = (f"output/buildroot-2025.02.1", f"output/{conf}/build/linux-{context['kernel_source_revision']}",
                   f"output/{conf}/build/buildroot-config", f"output/{conf}/Makefile", f"output/{conf}/.config")
        for member in input_archive:
            path = PurePosixPath(member.name)
            normalized = path.as_posix()
            require(member.name.rstrip("/") == normalized and not path.is_absolute() and
                    len(path.parts) >= 2 and path.parts[0] == "output" and
                    ".." not in path.parts and normalized not in names and
                    (member.isdir() or member.isfile() or member.issym()) and
                    (any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in allowed) or
                     (len(path.parts) == 3 and path.parts[1] == conf and
                      path.name.startswith(".br2-external"))) and
                    not any(str(parent) in symlinks for parent in path.parents),
                    "unsafe, unexpected or duplicate cached SDK entry")
            names.add(normalized)
            if member.issym():
                # Buildroot's standard skeleton has absolute symlinks such as
                # /etc/init.d/sysv-rcs; preserve the exact link text. Never
                # extract a subsequent archive member through such a link.
                symlinks.add(normalized)
        require(any(name.endswith("/Module.symvers") for name in names), "cached SDK has no compiled module symbols")
        require(not any(any(str(parent) in symlinks for parent in PurePosixPath(name).parents) for name in names),
                "cached SDK entry passes through a symlink")
        input_archive.extractall(sdk, filter="fully_trusted")
    _snapshot_paths(sdk, conf, context)
    return context
