#!/usr/bin/env python3
"""Identify the prepared SDK kernel ABI without including checkout/build locations."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


EXCLUDED_PARTS = {'.git', '.gradle', '.idea', '.vscode', '.claude', '.omp',
                  '__pycache__', '.cache', 'build', 'out', 'node_modules',
                  'qemu-linux', 'artifacts', 'debian'}
EXCLUDED_SUFFIXES = ('.o', '.a', '.so', '.ko', '.class', '.deb', '.dsc',
                     '.changes', '.buildinfo', '.mod', '.cmd')
EXCLUDED_NAMES = {'AGENTS.md', 'CLAUDE.md', 'local.properties',
                  'Module.symvers', 'modules.order'}
CONF_PATTERN = re.compile(r'[A-Za-z0-9_.+-]+_defconfig\Z')
REVISION_PATTERN = re.compile(r'[0-9a-f]{40}\Z')
RELEASE_PATTERN = re.compile(r'[A-Za-z0-9._+-]+\Z')


def run(command):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True)
    return result.stdout.strip()


def config_value(config, key):
    pattern = re.compile(r'^' + re.escape(key) + r'=(?:"([^"]*)"|(.*))$')
    matches = [match.group(1) if match.group(1) is not None else match.group(2)
               for line in config.read_text().splitlines()
               if (match := pattern.fullmatch(line))]
    if len(matches) != 1 or not matches[0]:
        raise ValueError(f'{config}: expected exactly one {key}')
    return matches[0]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def included(path):
    return (not any(part in EXCLUDED_PARTS or part.startswith('.tmp_versions')
                    for part in path.parts)
            and path.name not in EXCLUDED_NAMES
            and not path.name.endswith(EXCLUDED_SUFFIXES))


def kernel_inputs(sdk, defconfig, config):
    fragments = shlex.split(config_value(config, 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'))
    if not fragments:
        raise ValueError('selected kernel configuration has no board fragment')
    configured = shlex.split(config_value(defconfig, 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'))
    if fragments != configured:
        raise ValueError('prepared kernel board fragment differs from selected defconfig')
    # Buildroot application selections do not identify the prepared kernel ABI.
    paths = set()
    for fragment in fragments:
        relative = Path('buildroot-overlay') / fragment
        if fragment.startswith('/') or '..' in Path(fragment).parts:
            raise ValueError(f'unsafe board fragment: {fragment}')
        paths.add(relative)
    linux_overlay = sdk / 'buildroot-overlay/linux'
    if not linux_overlay.is_dir():
        raise ValueError(f'missing kernel overlay: {linux_overlay}')
    for path in linux_overlay.rglob('*'):
        relative = path.relative_to(sdk)
        if included(relative) and (path.is_file() or path.is_symlink()):
            paths.add(relative)
    digest = hashlib.sha256()
    for relative in sorted(paths, key=lambda value: value.as_posix()):
        path = sdk / relative
        if not path.is_file() and not path.is_symlink():
            raise ValueError(f'missing kernel input: {path}')
        digest.update(relative.as_posix().encode('utf-8'))
        digest.update(b'\0')
        if path.is_symlink():
            digest.update(os.readlink(path).encode('utf-8'))
        else:
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()


def resolve_context(sdk_root, conf):
    if not CONF_PATTERN.fullmatch(conf):
        raise ValueError(f'invalid CONF: {conf!r}')
    sdk = sdk_root.resolve()
    defconfig = sdk / 'buildroot-overlay/configs' / conf
    config = sdk / 'output' / conf / '.config'
    if not defconfig.is_file() or not config.is_file():
        raise ValueError(f'missing selected defconfig or prepared configuration: {conf}')
    if Path(config_value(config, 'BR2_DEFCONFIG')).name != conf:
        raise ValueError('prepared Buildroot configuration selects a different defconfig')
    revision = config_value(config, 'BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION')
    if (revision != config_value(defconfig, 'BR2_LINUX_KERNEL_CUSTOM_REPO_VERSION')
            or not REVISION_PATTERN.fullmatch(revision)):
        raise ValueError('selected kernel source revision differs from prepared configuration')
    variables = run(['make', '-s', '-C', str(sdk / 'output' / conf),
                     'printvars', 'VARS=LINUX_DIR'])
    matches = re.findall(r'^LINUX_DIR=(.+)$', variables, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('could not resolve one LINUX_DIR from Buildroot')
    kernel = Path(matches[0]).resolve()
    required = ('.config', 'include/config/auto.conf',
                'include/generated/utsrelease.h', 'scripts/mod/modpost',
                'Module.symvers')
    missing = [str(kernel / name) for name in required if not (kernel / name).is_file()]
    if missing:
        raise ValueError('incomplete prepared kernel tree: ' + ', '.join(missing))
    cross_path = Path(config_value(config, 'BR2_TOOLCHAIN_EXTERNAL_PATH'))
    prefix = config_value(config, 'BR2_TOOLCHAIN_EXTERNAL_PREFIX')
    if not re.fullmatch(r'[A-Za-z0-9_+.-]+', prefix):
        raise ValueError('invalid cross-compiler prefix')
    compiler = cross_path / 'bin' / (prefix + '-gcc')
    if not compiler.is_file():
        raise ValueError(f'configured cross-compiler is unavailable: {compiler}')
    cross_compile = str(cross_path / 'bin' / (prefix + '-'))
    release = run(['make', '-s', '-C', str(kernel), 'ARCH=riscv',
                   'CROSS_COMPILE=' + cross_compile, 'kernelrelease'])
    if not RELEASE_PATTERN.fullmatch(release):
        raise ValueError(f'invalid Kbuild kernel release: {release!r}')
    identity = run([str(compiler), '--version']).splitlines()
    if not identity:
        raise ValueError('cross-compiler returned no version identity')
    fields = {
        'schema_version': 1,
        'conf': conf,
        'kernel_source_revision': revision,
        'kernel_release': release,
        'kernel_config_sha256': sha256_file(kernel / '.config'),
        'module_symvers_sha256': sha256_file(kernel / 'Module.symvers'),
        'kernel_inputs_sha256': kernel_inputs(sdk, defconfig, config),
        'compiler_identity': identity[0],
        'compiler_sha256': sha256_file(compiler),
    }
    fields['abi_sha256'] = hashlib.sha256(json.dumps(fields, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--conf', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(resolve_context(args.sdk_root, args.conf), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'kernel-context: {error}\n')


if __name__ == '__main__':
    main()
