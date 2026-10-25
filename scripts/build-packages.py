#!/usr/bin/env python3
"""Build pinned Debian source and binary packages in disposable container worktrees."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from release_support import release_number, require, sha256
from sources import archive, export, fetch, git, validate_revision

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"[0-9a-f]{64}\Z")
CONF = re.compile(r"[A-Za-z0-9_.+-]+_defconfig\Z")
CONTAINER = re.compile(r"(?:debian:trixie-slim|ubuntu:22\.04)@sha256:[0-9a-f]{64}\Z")
PACKAGES = {
    "status-ready": ("k230-status-ready", "1.0", {"k230-status-ready": "all"}),
    "cyberdeck-service": ("cyberdeck-service", "0.1", {"cyberdeck-service": "riscv64"}),
    "sharp-drm": ("sharp-drm", "1.7", {"k230-sharp-drm": "riscv64", "k230-kernel-abi": "riscv64"}),
}


def keys(value: Any, expected: set[str], label: str) -> None:
    require(isinstance(value, dict) and set(value) == expected, f"{label} requires exactly {sorted(expected)}")


def validate_lock(value: Any) -> dict[str, Any]:
    keys(value, {"schema_version", "service", "sdk", "sharp", "toolchain", "debian_snapshot", "containers", "tools"}, "source lock")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "unsupported source-lock schema")
    for name in ("service", "sdk", "sharp"):
        expected = {"repository", "revision"} | ({"conf"} if name == "sdk" else set())
        entry = value[name]
        keys(entry, expected, name)
        require(isinstance(entry["repository"], str) and isinstance(entry["revision"], str), f"{name} requires repository and full revision")
        validate_revision(entry["repository"], entry["revision"])
    require(isinstance(value["sdk"]["conf"], str) and CONF.fullmatch(value["sdk"]["conf"]) is not None, "invalid SDK configuration")
    keys(value["toolchain"], {"url", "sha256", "prefix"}, "toolchain")
    require(value["toolchain"]["prefix"] == "riscv64-unknown-linux-gnu", "unsupported cross compiler prefix")
    require(value["debian_snapshot"] == "20250809T000000Z", "unsupported Debian snapshot")
    keys(value["containers"], {"service", "kernel"}, "containers")
    for name, prefix in (("service", "debian:trixie-slim"), ("kernel", "ubuntu:22.04")):
        ref = value["containers"][name]
        require(isinstance(ref, str) and ref.startswith(prefix + "@") and CONTAINER.fullmatch(ref) is not None, f"{name} must use a base image digest")
    keys(value["tools"], {"temurin17", "android_commandline", "android_platform36", "gradle"}, "tools")
    entries = [value["toolchain"], *value["tools"].values()]
    for entry in entries:
        expected = {"url", "sha256", "prefix"} if entry is value["toolchain"] else {"url", "sha256"}
        keys(entry, expected, "download")
        url, digest = entry["url"], entry["sha256"]
        require(isinstance(url, str) and isinstance(digest, str), "download URL and SHA-256 must be strings")
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password and not parsed.fragment and HEX.fullmatch(digest) is not None, "download must have pinned HTTPS URL and full SHA-256")
    return value


def run(command: list[str], *, cwd: Path | None = None, capture: bool = False, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout.strip() if capture else ""


def epoch_of(source: Path) -> int:
    return int(git(source, "show", "-s", "--format=%ct", "HEAD"))


def stage_debian(destination: Path, selector: str, version: str, epoch: int, context: dict[str, Any] | None) -> None:
    if selector != "cyberdeck-service":
        shutil.copytree(ROOT / "packages" / selector / "debian", destination / "debian")
    debian = destination / "debian"
    changelog = debian / "changelog"
    text = changelog.read_text()
    source_name = PACKAGES[selector][0]
    updated, count = re.subn(r"\A" + re.escape(source_name) + r" \([^\n)]+\)", f"{source_name} ({version})", text, count=1)
    require(count == 1, f"invalid {selector} changelog header")
    date = datetime.fromtimestamp(epoch, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    updated, count = re.subn(r"(?m)^( -- .+?>  ).*$", lambda m: m.group(1) + date, updated, count=1)
    require(count == 1, f"invalid {selector} changelog trailer")
    changelog.write_text(updated)
    require((debian / "source/format").read_text().strip() == "3.0 (quilt)", "source format must be 3.0 (quilt)")
    if context is not None:
        substitutions = {"@KERNEL_RELEASE@": context["kernel_release"], "@ABI_SHA256@": context["abi_sha256"]}
        preinsts = {script.name: script for script in debian.glob("*.preinst")}
        require(set(preinsts) == {"k230-sharp-drm.preinst", "k230-kernel-abi.preinst"}, "both ABI-specific packages require preinst guards")
        for script in preinsts.values():
            require("@ABI_SHA256@" in script.read_text(), f"missing ABI guard in {script.name}")
        for script in debian.glob("*.post*"):
            require("@KERNEL_RELEASE@" in script.read_text(), f"missing release in {script.name}")
        for script in (*debian.glob("*.preinst"), *debian.glob("*.postinst"), *debian.glob("*.postrm")):
            content = script.read_text()
            for token, value in substitutions.items():
                content = content.replace(token, value)
            require(not re.search(r"@[A-Z_]+@", content), f"unresolved template in {script.name}")
            script.write_text(content)


def build_image(selector: str, lock: dict[str, Any]) -> str:
    kind = "kernel" if selector == "sharp-drm" else "service"
    image = f"cyberdeck-packages-{kind}:{lock['containers'][kind].split('@sha256:')[1][:20]}"
    arguments = {"BASE_IMAGE": lock["containers"][kind]}
    if kind == "service":
        arguments["DEBIAN_SNAPSHOT"] = lock["debian_snapshot"]
        for key, arg in (("temurin17", "JDK17"), ("android_commandline", "ANDROID_COMMANDLINE"), ("android_platform36", "ANDROID_PLATFORM")):
            for field in ("url", "sha256"):
                arguments[f"{arg}_{field.upper()}"] = lock["tools"][key][field]
    else:
        arguments.update(TOOLCHAIN_URL=lock["toolchain"]["url"], TOOLCHAIN_SHA256=lock["toolchain"]["sha256"], TOOLCHAIN_PREFIX=lock["toolchain"]["prefix"])
    command = ["docker", "build", "--pull", "-f", str(ROOT / "containers" / f"{kind}.Dockerfile"), "-t", image]
    for key, value in arguments.items():
        command.extend(["--build-arg", f"{key}={value}"])
    run([*command, str(ROOT)])
    return image


def docker(image: str, work: Path, command: list[str], *, environment: dict[str, str] | None = None, capture: bool = True) -> str:
    args = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--network", "bridge", "--mount", f"type=bind,source={work},target=/build", "--mount", f"type=bind,source={ROOT / 'scripts'},target=/package-scripts,readonly", "-w", "/build"]
    for name, value in {"HOME": "/build", "GRADLE_USER_HOME": "/build/gradle-home", **(environment or {})}.items():
        args.extend(["-e", f"{name}={value}"])
    return run([*args, image, *command], capture=capture)


def corresponding_sources(binary: Path, work: Path, source_name: str, upstream: str, epoch: int) -> None:
    reports = binary / "mobileapp/libpebble3/build/reports/dependency-sources"
    index = json.loads((reports / "sources.json").read_text())
    keys(index, {"schema_version", "artifacts"}, "corresponding source index")
    require(index["schema_version"] == 1 and isinstance(index["artifacts"], list) and index["artifacts"], "missing corresponding source records")
    declared: set[str] = set()
    for item in index["artifacts"]:
        require(isinstance(item, dict) and isinstance(item.get("sources"), list) and item["sources"], "incomplete corresponding source entry")
        for entry in item["sources"]:
            filename, digest = entry["file"], entry["sha256"]
            require(isinstance(filename, str) and Path(filename).name == filename and filename not in declared, "unsafe or duplicate corresponding source filename")
            require(isinstance(digest, str) and HEX.fullmatch(digest) is not None and (reports / filename).is_file() and sha256(reports / filename) == digest, f"corresponding source digest mismatch: {filename}")
            declared.add(filename)
    native = json.loads((reports / "native-sources.json").read_text())
    require(native["schema_version"] == 1 and isinstance(native["archives"], list) and len(native["archives"]) >= 5, "missing native source records")
    for item in native["archives"]:
        filename = item["file"]
        require(Path(filename).name == filename and isinstance(item["sha256"], str) and HEX.fullmatch(item["sha256"]) is not None and (reports / filename).is_file() and item["sha256"] == sha256(reports / filename), f"native source digest mismatch: {filename}")
    for item in json.loads((reports / "cargo-notice-records.json").read_text()):
        for notice in item["notices"]:
            filename = notice["file"]
            require(Path(filename).name == filename and sha256(reports / "cargo-notices" / filename) == notice["sha256"], f"Cargo notice digest mismatch: {filename}")
    require(len(declared) >= 245, "incomplete Maven/Cargo/native source export")
    archive(reports, work / f"{source_name}_{upstream}.corresponding-sources.tar.gz", f"{source_name}-corresponding-sources", epoch)


def source_artifacts(work: Path) -> list[Path]:
    return [
        item for item in sorted(work.iterdir())
        if item.is_file() and item.suffix in (".dsc", ".gz", ".xz", ".changes", ".buildinfo", ".deb")
    ]


def verify_installed_notices(root: Path) -> None:
    notices = root / "usr/share/doc/cyberdeck-service/dependency-licenses"
    index = json.loads((notices / "dependencies.json").read_text())
    require(index["schema_version"] == 1 and isinstance(index["artifacts"], list) and index["artifacts"], "missing service dependency license index")
    entries = [*index["license_texts"], *(notice for artifact in index["artifacts"] for notice in artifact["notices"])]
    require(all(artifact["notices"] for artifact in index["artifacts"]), "dependency without a retained license notice")
    for entry in entries:
        filename, digest = entry["file"], entry["sha256"]
        require(isinstance(filename, str) and filename == Path(filename).name and isinstance(digest, str) and HEX.fullmatch(digest) is not None and (notices / filename).is_file() and sha256(notices / filename) == digest, f"missing or corrupt installed dependency notice: {filename}")


def inspect_binary(work: Path, image: str, path: Path, selector: str, version: str) -> dict[str, Any]:
    fields = docker(image, work, ["dpkg-deb", "--show", "--showformat=${Package}\\t${Version}\\t${Architecture}\\t${Depends}\\t__END__", f"/build/{path.relative_to(work)}"]).split("\t")
    require(len(fields) == 5 and fields[-1] == "__END__", f"invalid Debian control fields in {path.name}")
    name, actual_version, architecture, depends, _ = fields
    require(name in PACKAGES[selector][2] and actual_version == version and architecture == PACKAGES[selector][2][name], f"unexpected binary tuple: {path.name} ({name}, {actual_version}, {architecture})")
    listing = docker(image, work, ["dpkg-deb", "--contents", f"/build/{path.relative_to(work)}"])
    if name == "k230-status-ready":
        require("./usr/bin/k230-status-ready" in listing and "k230-status-ready.service" in listing, "readiness binary missing helper/unit")
    elif name == "cyberdeck-service":
        require("./usr/bin/cyberdeck_daemon" in listing and "libpebble3-jvm-fat.jar" in listing and "dependency-licenses" in listing, "service binary missing native/JAR/notices")
        require(all(dependency in depends for dependency in ("openjdk-21-jre-headless", "kbd", "libc6", "libstdc++6")), "service binary missing runtime dependencies")
        docker(image, work, ["dpkg-deb", "--extract", f"/build/{path.relative_to(work)}", "/build/service-inspection"], capture=False)
        verify_installed_notices(work / "service-inspection")
    elif name == "k230-sharp-drm":
        require("sharp-drm.ko" in listing and "k230-kernel-abi (= " + version + ")" in depends, "Sharp module/ABI dependency missing")
    else:
        require("kernel-context.json" in listing, "ABI marker missing context")
    return {"package": name, "version": actual_version, "architecture": architecture, "depends": depends}


def check_gradle_pin(service: Path, lock: dict[str, Any]) -> None:
    wrapper = service / "mobileapp/gradle/wrapper/gradle-wrapper.properties"
    properties = dict(line.split("=", 1) for line in wrapper.read_text().splitlines() if "=" in line)
    require(properties.get("distributionUrl", "").replace("\\:", ":") == lock["tools"]["gradle"]["url"], "canonical Gradle URL differs from source lock")
    require(properties.get("distributionSha256Sum") == lock["tools"]["gradle"]["sha256"], "canonical Gradle SHA-256 differs from source lock")

def build(selector: str, number: int, output_root: Path, lock: dict[str, Any]) -> dict[str, Any]:
    selected = output_root / selector
    require(not selected.exists() or not any(selected.iterdir()), f"selected output must be empty: {selected}")
    selected.mkdir(parents=True, exist_ok=True)
    source_name, upstream, expected = PACKAGES[selector]
    with tempfile.TemporaryDirectory(prefix=f"cyberdeck-{selector}-", dir=selected) as temporary:
        work = Path(temporary)
        image = build_image(selector, lock)
        sdk: Path | None = None
        submodules: list[dict[str, str]] = []
        if selector == "cyberdeck-service":
            origin = work / "service-git"
            submodules = fetch(**lock["service"], destination=origin, recursive=True)
            check_gradle_pin(origin, lock)
            source = origin
        else:
            sdk = work / "sdk"
            fetch(lock["sdk"]["repository"], lock["sdk"]["revision"], sdk, recursive=selector == "sharp-drm")
            source = sdk
        epoch = epoch_of(source)
        if sdk is not None:
            sdk_epoch = epoch
        original = work / "upstream"
        original.mkdir()
        if selector == "status-ready":
            shutil.copy2(sdk / "buildroot-overlay/package/k230-status-ready/k230-status-ready", original / "k230-status-ready")
            shutil.copy2(sdk / "LICENSE", original / "LICENSE")
            (original / "source-revision.txt").write_text(lock["sdk"]["revision"] + "\n")
        elif selector == "cyberdeck-service":
            export(source, original, debian=False)
        else:
            sharp = work / "sharp-git"
            fetch(lock["sharp"]["repository"], lock["sharp"]["revision"], sharp)
            export(sharp, original, debian=False)
            epoch = epoch_of(sharp)
        context = None
        if selector == "sharp-drm":
            conf = lock["sdk"]["conf"]
            docker(image, work, ["make", "-C", "/build/sdk", f"CONF={conf}", "linux"], environment={"SOURCE_DATE_EPOCH": str(sdk_epoch)}, capture=False)
            raw = docker(image, work, ["python3", "/package-scripts/kernel-context.py", "--sdk-root", "/build/sdk", "--conf", conf])
            context = json.loads(raw)
            require(context["conf"] == conf and HEX.fullmatch(context["abi_sha256"]) is not None, "invalid configured kernel context")
            (work / "kernel-context.json").write_text(json.dumps(context, sort_keys=True, indent=2) + "\n")
            version = f"{upstream}+cyberdeck.{number}+abi.{context['abi_sha256']}-1"
        else:
            version = f"{upstream}+cyberdeck.{number}-1"
        # In 3.0 (quilt), every component before the Debian revision is the
        # upstream version, including the release and (for Sharp) ABI suffixes.
        upstream_version = version.rsplit("-", 1)[0]
        archive(original, work / f"{source_name}_{upstream_version}.orig.tar.gz", f"{source_name}-{upstream_version}", epoch)
        staged = work / f"{source_name}-{upstream_version}"
        shutil.copytree(original, staged, symlinks=True)
        if selector == "cyberdeck-service":
            # Canonical Debian metadata is exported from the same pinned commit.
            debian_source = work / "canonical-with-debian"
            export(source, debian_source)
            shutil.copytree(debian_source / "debian", staged / "debian")
        stage_debian(staged, selector, version, epoch, context)
        variables = {"SOURCE_DATE_EPOCH": str(epoch), "DEB_BUILD_OPTIONS": "nocheck"}
        if context is not None:
            variables.update({"K230_KERNEL_CONTEXT": "/build/kernel-context.json", "K230_KERNEL_RELEASE": context["kernel_release"], "ARCH": "riscv"})
            # The configured Buildroot output resolves LINUX_DIR; these are build paths,
            # deliberately not fields in the release context's ABI identity.
            config = sdk / "output" / lock["sdk"]["conf"] / ".config"
            require(config.is_file(), "SDK linux target did not produce configured Buildroot output")
            printvars = docker(image, work, ["make", "-s", "-C", f"/build/sdk/output/{lock['sdk']['conf']}", "printvars", "VARS=LINUX_DIR"])
            matches = re.findall(r"^LINUX_DIR=(.+)$", printvars, re.MULTILINE)
            require(len(matches) == 1 and matches[0].startswith("/build/sdk/"), "cannot resolve configured Linux directory")
            variables["K230_KERNEL_DIR"] = matches[0]
            configured = config.read_text()
            cross_path = re.findall(r'^BR2_TOOLCHAIN_EXTERNAL_PATH="([^"]+)"$', configured, re.MULTILINE)
            cross_prefix = re.findall(r'^BR2_TOOLCHAIN_EXTERNAL_PREFIX="([^"]+)"$', configured, re.MULTILINE)
            require(len(cross_path) == len(cross_prefix) == 1 and cross_prefix[0] == lock["toolchain"]["prefix"] and cross_path[0].startswith("/opt/toolchain/"), "configured cross compiler differs from locked Xuantie toolchain")
            variables["K230_CROSS_COMPILE"] = cross_path[0].rstrip("/") + "/bin/" + cross_prefix[0] + "-"
        # Create source package first, extract its .dsc, then compile precisely that source.
        docker(image, work, ["sh", "-c", "cd /build/" + staged.name + " && exec dpkg-buildpackage -S -us -uc"], environment=variables, capture=False)
        docker(image, work, ["dpkg-source", "-x", f"/build/{source_name}_{version}.dsc", "/build/extracted"], environment=variables, capture=False)
        binary = work / "extracted"
        host = ["--host-arch=riscv64"] if selector != "status-ready" else []
        docker(image, work, ["sh", "-c", "cd /build/extracted && exec dpkg-buildpackage --build=binary -us -uc " + " ".join(host)], environment=variables, capture=False)
        if selector == "cyberdeck-service":
            corresponding_sources(binary, work, source_name, upstream_version, epoch)
        if context is not None:
            module = binary / "sharp-drm.ko"
            require(module.is_file(), "Kbuild did not produce Sharp module")
            vermagic = docker(image, work, ["modinfo", "-F", "vermagic", "/build/extracted/sharp-drm.ko"])
            require(vermagic.split()[0] == context["kernel_release"], "Sharp module vermagic disagrees with configured kernel")
        require(not any(work.glob("*.ddeb")) and not any(work.glob("*.udeb")), "undeclared debug or installer binary output")
        paths = source_artifacts(work)
        artifacts = []
        binaries = set()
        for path in paths:
            kind = "deb" if path.suffix == ".deb" else "changes" if path.suffix == ".changes" else "buildinfo" if path.suffix == ".buildinfo" else "source"
            extra = inspect_binary(work, image, path, selector, version) if kind == "deb" else {}
            if kind == "deb":
                require(extra["package"] not in binaries, "duplicate binary package output")
                binaries.add(extra["package"])
            target = selected / path.name
            shutil.copy2(path, target)
            artifacts.append({"file": target.name, "sha256": sha256(target), "size": target.stat().st_size, "kind": kind, **extra})
        require(binaries == set(expected), f"declared binary outputs mismatch: expected {sorted(expected)}, received {sorted(binaries)}")
        require(any(a["file"].endswith(".dsc") for a in artifacts) and any(a["kind"] == "buildinfo" for a in artifacts) and sum(a["kind"] == "changes" for a in artifacts) == 2, "incomplete Debian source/binary outputs")
        if context is not None:
            shutil.copy2(work / "kernel-context.json", selected / "kernel-context.json")
        record = {"schema_version": 1, "package": selector, "release_number": number, "conf": lock["sdk"]["conf"], "source_date_epoch": epoch, "sources": {"lock": lock, "submodules": submodules}, "artifacts": artifacts}
        if context is not None:
            record["kernel_context"] = {"file": "kernel-context.json", "sha256": sha256(selected / "kernel-context.json")}
        (selected / f"{selector}-build.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=PACKAGES, required=True)
    parser.add_argument("--release-number", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--development", action="store_true", help="allow release number zero in CI only")
    args = parser.parse_args()
    try:
        require(not args.development or os.environ.get("CI") == "true", "development release requires CI")
        number = release_number(args.release_number, development=args.development)
        lock = validate_lock(json.loads((ROOT / "source-lock.json").read_text()))
        record = build(args.package, number, args.output.resolve(), lock)
        print(json.dumps({"package": record["package"], "artifacts": len(record["artifacts"])}, sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError, KeyError) as error:
        parser.exit(1, f"build-packages: {error}\n")


if __name__ == "__main__":
    main()
