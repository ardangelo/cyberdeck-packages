"""Behavioral boundaries of source-sensitive package and intermediate caches."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_cache import cache_keys


class BuildCacheKeys(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name, content in {
            ".gitignore": ".cache/\noutput/\n__pycache__/\n*.pyc\n",
            "scripts/build-packages.py": "build logic\n",
            "scripts/kernel-context.py": "kernel helper\n",
            "scripts/release_support.py": "shared helper\n",
            "scripts/archive-release.py": "publication logic\n",
            "scripts/smoke-packages.py": "target smoke\n",
            "containers/service.Dockerfile": "FROM service\n",
            "containers/kernel.Dockerfile": "FROM kernel\n",
            "packages/status-ready/debian/control": "status recipe\n",
            "packages/sharp-drm/debian/control": "sharp recipe\n",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.lock = json.loads((ROOT / "source-lock.json").read_text())

    def keys(self, selector="sharp-drm", number=1, lock=None):
        return cache_keys(self.root, selector, number, lock or self.lock)

    def test_publication_smoke_and_generated_state_cannot_invalidate_build(self):
        old = self.keys()
        (self.root / "scripts/archive-release.py").write_text("new publication code")
        (self.root / "scripts/smoke-packages.py").write_text("new smoke code")
        (self.root / ".cache/cyberdeck/packages/x").mkdir(parents=True)
        (self.root / ".cache/cyberdeck/packages/x/data").write_text("generated")
        (self.root / "scripts/__pycache__").mkdir()
        (self.root / "scripts/__pycache__/build.cpython.pyc").write_bytes(b"generated")
        self.assertEqual(self.keys(), old)

    def test_source_number_selected_recipes_and_modes(self):
        old = self.keys()
        self.assertNotEqual(self.keys(number=0)["package"], old["package"])
        self.assertNotEqual(self.keys(number=2)["package"], old["package"])
        self.assertEqual(self.keys(number=2)["kernel"], old["kernel"])
        recipe = self.root / "packages/sharp-drm/debian/control"
        recipe.write_text("new sharp recipe\n")
        changed = self.keys()
        self.assertNotEqual(changed["package"], old["package"])
        self.assertEqual(changed["kernel"], old["kernel"])
        recipe.write_text("sharp recipe\n")
        recipe.chmod(0o755)
        self.assertNotEqual(self.keys()["package"], old["package"])
        self.assertEqual(self.keys("status-ready")["kernel"], old["kernel"])

    def test_distinct_source_pins_only_invalidate_relevant_intermediates(self):
        old = self.keys()
        for source in ("service", "sharp"):
            lock = copy.deepcopy(self.lock)
            lock[source]["revision"] = "a" * 40
            self.assertNotEqual(self.keys(lock=lock)["package"], old["package"])
            self.assertEqual(self.keys(lock=lock)["kernel"], old["kernel"])
            self.assertEqual(self.keys(lock=lock)["image"], old["image"])
        sdk = copy.deepcopy(self.lock)
        sdk["sdk"]["revision"] = "b" * 40
        self.assertNotEqual(self.keys(lock=sdk)["kernel"], old["kernel"])
        toolchain = copy.deepcopy(self.lock)
        toolchain["toolchain"]["sha256"] = "c" * 64
        self.assertNotEqual(self.keys(lock=toolchain)["kernel"], old["kernel"])
        self.assertNotEqual(self.keys(lock=toolchain)["image"], old["image"])
        service_image = copy.deepcopy(self.lock)
        service_image["containers"]["service"] = "debian:trixie-slim@sha256:" + "d" * 64
        self.assertEqual(self.keys(lock=service_image)["kernel"], old["kernel"])

    def test_selected_container_and_unknown_helpers_are_hashed(self):
        old = self.keys()
        (self.root / "containers/service.Dockerfile").write_text("new service dockerfile\n")
        self.assertEqual(self.keys()["image"], old["image"])
        self.assertEqual(self.keys()["kernel"], old["kernel"])
        (self.root / "containers/kernel.Dockerfile").write_text("new kernel dockerfile\n")
        changed = self.keys()
        self.assertNotEqual(changed["image"], old["image"])
        self.assertNotEqual(changed["kernel"], old["kernel"])
        (self.root / "scripts/future-build-helper.py").write_text("new build behavior\n")
        self.assertNotEqual(self.keys()["kernel"], changed["kernel"])
        self.assertNotEqual(self.keys()["package"], changed["package"])
        changed = self.keys()
        (self.root / "scripts/nested").mkdir()
        (self.root / "scripts/nested/archive-release.py").write_text("nested helper is not the publication program\n")
        self.assertNotEqual(self.keys()["package"], changed["package"])

    def test_binary_build_inputs_cannot_alias_an_added_helper(self):
        helper = self.root / "scripts/a"
        helper.write_bytes(b"prefix\0scripts/b\0file:0644\0payload")
        initial = self.keys()
        helper.write_bytes(b"prefix")
        (self.root / "scripts/b").write_bytes(b"payload")
        self.assertNotEqual(self.keys()["package"], initial["package"])

    def test_symlink_target_bytes_are_inputs_and_outside_checkout_targets_rejected(self):
        target = self.root / "shared-input.txt"
        target.write_text("first build input\n")
        link = self.root / "scripts/kernel-input.py"
        link.symlink_to("../shared-input.txt")
        initial = self.keys()
        target.write_text("changed build input\n")
        self.assertNotEqual(self.keys()["kernel"], initial["kernel"])
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "external.py"
            external.write_text("untracked host input\n")
            link.unlink()
            link.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "symlink.*inside the checkout"):
                self.keys()

    def test_gradle_crosses_release_numbers_but_not_toolchain(self):
        old = self.keys("cyberdeck-service", 0)
        newer = self.keys("cyberdeck-service", 2)
        self.assertEqual(old["gradle"], newer["gradle"])
        self.assertNotEqual(old["package"], newer["package"])
        lock = copy.deepcopy(self.lock)
        lock["service"]["revision"] = "a" * 40
        changed = self.keys("cyberdeck-service", 2, lock)
        self.assertEqual(old["gradle_restore_prefix"], changed["gradle_restore_prefix"])
        self.assertNotEqual(old["gradle"], changed["gradle"])
        lock["tools"]["gradle"]["sha256"] = "b" * 64
        self.assertNotEqual(changed["gradle_restore_prefix"], self.keys("cyberdeck-service", 2, lock)["gradle_restore_prefix"])


if __name__ == "__main__":
    unittest.main()
