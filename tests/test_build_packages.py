"""Behavioral checks for invalid release, lock and corresponding-source inputs."""

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build-packages.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("build_packages", SCRIPT)
packages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packages)


class ReleaseInputs(unittest.TestCase):
    def test_invalid_release_numbers_do_not_invoke_build(self):
        with tempfile.TemporaryDirectory() as directory:
            for number in ("0", "00", "01", "-1", "1.0", "1;true", "1$(true)", " +1", "", "２"):
                with self.subTest(number=number):
                    result = subprocess.run([sys.executable, str(SCRIPT), "--package", "status-ready", "--release-number", number, "--output", directory], text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(packages.release_number("0", development=True), 0)
            self.assertEqual(packages.release_number("13"), 13)

    def test_missing_or_floating_inputs_rejected(self):
        lock = json.loads((ROOT / "source-lock.json").read_text())
        packages.validate_lock(lock)
        for mutation in (
            lambda obj: obj["tools"].pop("gradle"),
            lambda obj: obj["containers"].update(kernel="ubuntu:22.04"),
            lambda obj: obj["service"].update(revision="master"),
            lambda obj: obj["toolchain"].update(sha256="0" * 63),
        ):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(lock))
                mutation(changed)
                with self.assertRaises(ValueError):
                    packages.validate_lock(changed)

    def test_refuses_reused_selected_output(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "status-ready"
            selected.mkdir()
            (selected / "existing").write_text("operator artifact")
            lock = packages.validate_lock(json.loads((ROOT / "source-lock.json").read_text()))
            with self.assertRaisesRegex(ValueError, "selected output must be empty"):
                packages.build("status-ready", 2, Path(directory), lock)
            self.assertEqual((selected / "existing").read_text(), "operator artifact")


    def test_corrupt_corresponding_source_blocks_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            report = work / "extracted/mobileapp/libpebble3/build/reports/dependency-sources"
            report.mkdir(parents=True)
            (report / "sources.jar").write_bytes(b"corrupted")
            (report / "sources.json").write_text(json.dumps({
                "schema_version": 1,
                "artifacts": [{"artifact": "vendor:library:1", "sources": [{
                    "file": "sources.jar", "sha256": "0" * 64
                }]}],
            }))
            with self.assertRaisesRegex(ValueError, "corresponding source digest mismatch"):
                packages.corresponding_sources(work / "extracted", work, "cyberdeck-service", "0.1+cyberdeck.1", 1)
            self.assertEqual(list(work.glob("*.tar.gz")), [])


    def test_service_notice_index_rejects_missing_and_corrupt_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notices = root / "usr/share/doc/cyberdeck-service/dependency-licenses"
            notices.mkdir(parents=True)
            body = b"Publisher license grant\n"
            filename = "publisher-license.txt"
            digest = hashlib.sha256(body).hexdigest()
            (notices / "dependencies.json").write_text(json.dumps({
                "schema_version": 1,
                "license_texts": [],
                "artifacts": [{"artifact": "vendor:library:1", "notices": [
                    {"file": filename, "sha256": digest}
                ]}],
            }))
            with self.assertRaisesRegex(ValueError, "missing or corrupt installed dependency notice"):
                packages.verify_installed_notices(root)
            (notices / filename).write_bytes(body)
            packages.verify_installed_notices(root)
            (notices / filename).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "missing or corrupt installed dependency notice"):
                packages.verify_installed_notices(root)


if __name__ == "__main__":
    unittest.main()
