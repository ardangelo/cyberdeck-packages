"""Prepared kernel identity contract exercised through the public CLI."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / 'scripts/kernel-context.py'
CONF = 'k230_canmv_01studio_defconfig'
REVISION = '7d4e1f444f461dbe3833bd99a4640e7b6c2cd529'


class KernelContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sdk = Path(self.temporary.name) / 'sdk'
        self.kernel = self.sdk / 'output' / CONF / 'build/linux-test'
        self.toolchain = Path(self.temporary.name) / 'toolchain'
        self.compiler = self.toolchain / 'bin/riscv64-unknown-linux-gnu-gcc'
        self.file('buildroot-overlay/configs/' + CONF,
                  'BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION="' + REVISION + '"\n'
                  'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="board/canaan/k230-soc/fragment/linux.fragment"\n')
        self.file('buildroot-overlay/board/canaan/k230-soc/fragment/linux.fragment', 'CONFIG_DRM=y\n')
        self.file('buildroot-overlay/linux/board.patch', 'diff --git a/a b/a\n')
        self.file('output/' + CONF + '/.config',
                  'BR2_DEFCONFIG="buildroot-overlay/configs/' + CONF + '"\n'
                  'BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION="' + REVISION + '"\n'
                  'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="board/canaan/k230-soc/fragment/linux.fragment"\n'
                  'BR2_TOOLCHAIN_EXTERNAL_PATH="' + str(self.toolchain) + '"\n'
                  'BR2_TOOLCHAIN_EXTERNAL_PREFIX="riscv64-unknown-linux-gnu"\n')
        self.file('output/' + CONF + '/Makefile',
                  'printvars:\n\t@printf "LINUX_DIR=%s\\n" "$(CURDIR)/build/linux-test"\n')
        self.file('output/' + CONF + '/build/linux-test/Makefile',
                  'kernelrelease:\n\t@printf "6.6.0-k230\\n"\n')
        for item in ('.config', 'Module.symvers', 'include/config/auto.conf',
                     'include/generated/utsrelease.h', 'scripts/mod/modpost'):
            self.file('output/' + CONF + '/build/linux-test/' + item, item + '\n')
        self.compiler.parent.mkdir(parents=True)
        self.compiler.write_text('#!/bin/sh\nprintf "Xuantie compiler 14.0\\n"\n')
        self.compiler.chmod(0o755)

    def file(self, name, data):
        path = self.sdk / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def context(self, root=None):
        result = subprocess.run(['python3', str(CLI), '--sdk-root', str(root or self.sdk),
                                 '--conf', CONF], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def rejected(self):
        result = subprocess.run(['python3', str(CLI), '--sdk-root', str(self.sdk),
                                 '--conf', CONF], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_abi_changes_for_every_kernel_identity_input_without_release_change(self):
        baseline = self.context()
        self.assertEqual(set(baseline), {'schema_version', 'conf', 'kernel_source_revision',
            'kernel_release', 'kernel_config_sha256', 'module_symvers_sha256',
            'kernel_inputs_sha256', 'compiler_identity', 'compiler_sha256', 'abi_sha256'})
        self.assertEqual(baseline['kernel_source_revision'], REVISION)
        self.assertEqual(baseline['kernel_release'], '6.6.0-k230')
        for path, change, field in [
            (self.kernel / '.config', 'CONFIG_DRM=m\n', 'kernel_config_sha256'),
            (self.kernel / 'Module.symvers', 'a changed symbol\n', 'module_symvers_sha256'),
            (self.sdk / 'buildroot-overlay/linux/board.patch', 'different authored patch\n', 'kernel_inputs_sha256'),
            (self.sdk / 'buildroot-overlay/board/canaan/k230-soc/fragment/linux.fragment', 'CONFIG_DRM=m\n', 'kernel_inputs_sha256'),
            (self.sdk / 'buildroot-overlay/configs' / CONF, 'BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION="' + REVISION + '"\nBR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="board/canaan/k230-soc/fragment/linux.fragment"\n# selected input changed\n', 'kernel_inputs_sha256'),
            (self.compiler, '#!/bin/sh\nprintf "other compiler 14.0\\n"\n', 'compiler_identity'),
            (self.compiler, '#!/bin/sh\nprintf "Xuantie compiler 14.0\\n"\n# new compiler bytes\n', 'compiler_sha256'),
        ]:
            original = path.read_text()
            path.write_text(change)
            changed = self.context()
            self.assertNotEqual(changed[field], baseline[field], str(path))
            self.assertNotEqual(changed['abi_sha256'], baseline['abi_sha256'])
            self.assertEqual(changed['kernel_release'], baseline['kernel_release'])
            path.write_text(original)
        fields = {key: value for key, value in baseline.items() if key != 'abi_sha256'}
        expected = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(baseline['abi_sha256'], expected)

    def test_kernel_revision_provenance_and_ignored_generated_inputs(self):
        baseline = self.context()
        self.file('buildroot-overlay/linux/build/scratch', 'not authored\n')
        self.file('buildroot-overlay/linux/generated.ko', 'not source\n')
        self.assertEqual(self.context(), baseline)
        replacement = 'a' * 40
        for config in (self.sdk / 'buildroot-overlay/configs' / CONF,
                       self.sdk / 'output' / CONF / '.config'):
            config.write_text(config.read_text().replace(REVISION, replacement))
        changed = self.context()
        self.assertEqual(changed['kernel_source_revision'], replacement)
        self.assertNotEqual(changed['abi_sha256'], baseline['abi_sha256'])

    def test_relocation_and_symlink_target_stability(self):
        link = self.sdk / 'buildroot-overlay/linux/board-link'
        link.symlink_to('board.patch')
        original = self.context()
        destination = Path(self.temporary.name) / 'moved'
        shutil.move(self.sdk, destination)
        # Buildroot output and compiler are relocatable; only the input bytes matter.
        config = destination / 'output' / CONF / '.config'
        other_toolchain = Path(self.temporary.name) / 'other-toolchain'
        shutil.copytree(self.toolchain, other_toolchain)
        config.write_text(config.read_text().replace(str(self.toolchain), str(other_toolchain)))
        self.assertEqual(self.context(destination), original)
        link = destination / 'buildroot-overlay/linux/board-link'
        link.unlink()
        link.symlink_to('different.patch')
        self.assertNotEqual(self.context(destination)['abi_sha256'], original['abi_sha256'])

    def test_rejects_wrong_selected_config_or_incomplete_kernel(self):
        config = self.sdk / 'output' / CONF / '.config'
        config.write_text(config.read_text().replace('BR2_DEFCONFIG="buildroot-overlay/configs/' + CONF,
                                                      'BR2_DEFCONFIG="buildroot-overlay/configs/other'))
        self.rejected()
        config.write_text(config.read_text().replace('configs/other', 'configs/' + CONF))
        (self.kernel / 'scripts/mod/modpost').unlink()
        self.rejected()

    def test_rejects_unselected_kernel_inputs_and_missing_compiler(self):
        config = self.sdk / 'output' / CONF / '.config'
        selected = config.read_text()
        config.write_text(selected.replace('linux.fragment', 'other.fragment'))
        self.rejected()
        config.write_text(selected)
        self.compiler.unlink()
        self.rejected()


if __name__ == '__main__':
    unittest.main()
