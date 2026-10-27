"""Cache entries must be complete and tied to real package/SDK/image bytes."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_state
from release_support import canonical, sha256

LOCK = json.loads((ROOT / "source-lock.json").read_text())
PACKAGES = {"status-ready": ("k230-status-ready", "1.0", {"k230-status-ready": "all"})}


def status_entry(root: Path, number: int = 2) -> Path:
    version = f"1.0+cyberdeck.{number}-1"
    selected = root / "selected"
    selected.mkdir()
    fixture = root / "fixture"
    control = fixture / "DEBIAN"
    control.mkdir(parents=True)
    (control / "control").write_text(f"Package: k230-status-ready\nVersion: {version}\nArchitecture: all\nMaintainer: Fixture <fixture@example.org>\nDescription: isolated cache fixture\n")
    (fixture / "usr/bin").mkdir(parents=True)
    (fixture / "usr/bin/k230-status-ready").write_text("#!/bin/sh\nexit 0\n")
    deb = selected / f"k230-status-ready_{version}_all.deb"
    subprocess.run(["dpkg-deb", "--build", str(fixture), str(deb)], check=True, capture_output=True)
    artifacts = [{"file": deb.name, "sha256": sha256(deb), "size": deb.stat().st_size, "kind": "deb",
                  "package": "k230-status-ready", "version": version, "architecture": "all", "depends": ""}]
    for filename, kind in (
        (f"k230-status-ready_{version}.dsc", "source"),
        (f"k230-status-ready_{version}.debian.tar.xz", "source"),
        (f"k230-status-ready_{version[:-2]}.orig.tar.gz", "source"),
        (f"k230-status-ready_{version}_source.changes", "changes"),
        (f"k230-status-ready_{version}_all.changes", "changes"),
        (f"k230-status-ready_{version}_source.buildinfo", "buildinfo"),
        (f"k230-status-ready_{version}_all.buildinfo", "buildinfo"),
    ):
        file = selected / filename
        file.write_bytes(b"fixture source/build payload\n")
        artifacts.append({"file": filename, "sha256": sha256(file), "size": file.stat().st_size, "kind": kind})
    (selected / "status-ready-build.json").write_text(json.dumps({"schema_version": 1, "package": "status-ready",
        "release_number": number, "conf": LOCK["sdk"]["conf"], "source_date_epoch": 1740000000,
        "sources": {"lock": LOCK, "submodules": []}, "artifacts": artifacts}))
    return selected


def context() -> dict:
    identity = {"schema_version": 1, "conf": LOCK["sdk"]["conf"],
        "kernel_source_revision": "a" * 40, "kernel_release": "6.6.0-test",
        "kernel_config_sha256": hashlib.sha256(b"CONFIG_TEST_KERNEL=y\n").hexdigest(),
        "module_symvers_sha256": hashlib.sha256(b"symbols from a completed kernel\n").hexdigest(),
        "kernel_inputs_sha256": "d" * 64, "compiler_identity": "Xuantie test compiler",
        "compiler_sha256": "e" * 64}
    return identity | {"abi_sha256": hashlib.sha256(canonical(identity)).hexdigest()}


class CacheState(unittest.TestCase):
    def test_package_reuses_only_complete_matching_bytes_and_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = status_entry(root)
            cache = root / "packages" / "entry"
            build_state.save_package(cache, selected, "status-ready", 2, LOCK, PACKAGES)
            restored = root / "restored"
            record = build_state.load_package(cache, restored, "status-ready", 2, LOCK, PACKAGES)
            self.assertEqual(record["release_number"], 2)
            self.assertEqual({item.name for item in restored.iterdir()}, {item.name for item in selected.iterdir()})
            with self.assertRaisesRegex(ValueError, "metadata does not match"):
                build_state.load_package(cache, root / "wrong-number", "status-ready", 3, LOCK, PACKAGES)
            (cache / "surprise").write_text("not a declared output")
            with self.assertRaisesRegex(ValueError, "unexpected or missing"):
                build_state.load_package(cache, root / "extra", "status-ready", 2, LOCK, PACKAGES)
            (cache / "surprise").unlink()
            binary = next(cache.glob("*.deb"))
            binary.write_bytes(binary.read_bytes()[:-8])
            with self.assertRaisesRegex(ValueError, "corrupt cached file"):
                build_state.load_package(cache, root / "truncated", "status-ready", 2, LOCK, PACKAGES)

    def test_invalid_image_archive_is_rejected_before_docker_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = "cyberdeck-packages-service:verified"
            cache = root / "images" / "entry"
            cache.mkdir(parents=True)
            (cache / "image.tar").write_bytes(b"truncated Docker image")
            (cache / "state.json").write_text(json.dumps({"schema_version": 1, "key": "entry", "kind": "service",
                "container": LOCK["containers"]["service"], "image": image, "image_id": "sha256:" + "a" * 64,
                "archive_sha256": "b" * 64, "archive_size": (cache / "image.tar").stat().st_size}))
            def forbidden(*args, **kwargs):
                self.fail("corrupt image reached Docker")
            with self.assertRaisesRegex(ValueError, "corrupt cached file"):
                build_state.load_image(cache, "entry", "service", LOCK, image, forbidden)

    def test_prepared_kernel_snapshot_preserves_tree_and_rejects_corrupt_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            conf = LOCK["sdk"]["conf"]
            prepared = sdk / "output" / conf
            kernel = prepared / "build" / ("linux-" + context()["kernel_source_revision"])
            (kernel / "scripts/mod").mkdir(parents=True)
            (kernel / ".config").write_text("CONFIG_TEST_KERNEL=y\n")
            (kernel / "include/config").mkdir(parents=True)
            (kernel / "include/config/auto.conf").write_text("CONFIG_TEST_KERNEL=y\n")
            (kernel / "Module.symvers").write_text("symbols from a completed kernel\n")
            (kernel / "scripts/mod/modpost").write_bytes(b"real Kbuild host tool fixture")
            (kernel / "include/generated").mkdir(parents=True)
            (kernel / "include/generated/utsrelease.h").write_text("#define UTS_RELEASE \"6.6.0-test\"\n")
            (prepared / ".config").write_text("BR2_LINUX_KERNEL=y\n")
            (prepared / "Makefile").write_text("printvars:\n\t@echo LINUX_DIR=/build/sdk/output/kernel\n")
            buildroot = sdk / "output/buildroot-2025.02.1"
            buildroot.mkdir()
            (buildroot / "skeleton-link").symlink_to("/etc/init.d/sysv-rcs")
            cache = root / "kernels" / "entry"
            build_state.save_kernel(cache, "entry", sdk, conf, context(), LOCK)
            restored = root / "restored-sdk"
            restored.mkdir()
            self.assertEqual(build_state.load_kernel(cache, "entry", restored, conf, LOCK), context())
            self.assertEqual((restored / "output" / conf / "build" / kernel.name / "Module.symvers").read_text(),
                             "symbols from a completed kernel\n")
            self.assertEqual((restored / "output/buildroot-2025.02.1/skeleton-link").readlink(), Path("/etc/init.d/sysv-rcs"))
            archive = cache / "sdk.tar.gz"
            changed = root / "extra-sdk.tar.gz"
            with tarfile.open(archive, "r:gz") as original, tarfile.open(changed, "w:gz") as altered:
                for entry in original:
                    altered.addfile(entry, original.extractfile(entry) if entry.isfile() else None)
                extra = tarfile.TarInfo("output/unexpected-board/.config")
                extra.size = 4
                altered.addfile(extra, io.BytesIO(b"oops"))
            archive.write_bytes(changed.read_bytes())
            state = json.loads((cache / "state.json").read_text())
            state["archive_sha256"] = sha256(archive)
            state["archive_size"] = archive.stat().st_size
            (cache / "state.json").write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "unexpected.*SDK entry"):
                build_state.load_kernel(cache, "entry", root / "extra-sdk", conf, LOCK)
            archive.write_bytes(archive.read_bytes()[:-3])
            with self.assertRaisesRegex(ValueError, "corrupt cached file"):
                build_state.load_kernel(cache, "entry", root / "unrestored-sdk", conf, LOCK)



if __name__ == "__main__":
    unittest.main()
