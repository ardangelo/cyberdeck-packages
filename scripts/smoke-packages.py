#!/usr/bin/env python3
"""Exercise a verified package cohort in a disposable QEMU RISC-V Debian rootfs.

Requires root, mmdebstrap, working RISC-V binfmt, qemu-riscv64-static,
dpkg-deb, kmod, Git, riscv64-linux-gnu-g++, native JDK 21 headers, and
permission to mount a temporary read-only proc. Never starts hardware services.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from release_support import (PACKAGE_ARCHES, basename, canonical, expected_versions, inspect_deb,
                             load_json, require, sha256, validate_cohort,
                             validate_context)

ROOT = Path(__file__).resolve().parents[1]
KEYRING = Path('/usr/share/keyrings/debian-archive-keyring.gpg')
JAR = Path('usr/lib/cyberdeck-service/libpebble3-jvm-fat.jar')
DRIVER = r'''#include "jni/uart_jni_bridge.hpp"
#include <iostream>
#include <stdexcept>
int main() {
    try {
        {
            cyberdeck::UartJniBridge bridge("/usr/lib/cyberdeck-service/libpebble3-jvm-fat.jar");
            bridge.set_endpoint_callback([](uint16_t, const std::vector<uint8_t>&) {});
            bridge.set_connected_callback([] {});
            bridge.set_session_opened_callback([] {});
            bridge.set_disconnected_callback([] {});
            if (!bridge.init("/dev/null")) throw std::runtime_error("JNI init(/dev/null) failed");
            bridge.disconnect();
        }
        std::cout << "CYBERDECK_JNI_SMOKE_OK" << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
'''


def command(args, *, env=None, cwd=None, check=True):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace', timeout=900)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}:\n{result.stdout[-8000:]}')
    return result


RUNTIME = 'binfmt-chroot'


def target(root, *argv, env=None, check=True):
    variables = ['/usr/bin/env', 'DEBIAN_FRONTEND=noninteractive', 'APT_LISTCHANGES_FRONTEND=none']
    variables += [f'{key}={value}' for key, value in (env or {}).items()]
    return command(['chroot', root, *variables, *argv], check=check)

@contextmanager
def proc_runtime(root):
    """Mount only a disposable rootfs's read-only proc; always unmount it."""
    proc = Path(root) / 'proc'
    proc.mkdir(exist_ok=True)
    require(not proc.is_mount(), 'disposable rootfs already has a proc mount')
    command(['mount', '-t', 'proc', '-o', 'ro', 'proc', proc])
    try:
        yield
    finally:
        command(['umount', proc])


@contextmanager
def prepared_rootfs(root, snapshot):
    bootstrap(root, snapshot)
    with proc_runtime(root):
        initialize_base(root)
        yield root


def target_helper(root, value, *, check=True):
    return target(root, '/usr/bin/k230-status-ready', value,
                  env={'K230_STATUS_READY_SYSFS': '/usr/share/k230/status-smoke-sysfs'},
                  check=check)


def inspect_input(directory):
    verified = load_json(directory / 'verified-input.json')
    require(type(verified['schema_version']) is int and verified['schema_version'] == 1,
            'verified input schema mismatch')
    number = verified['release_number']
    require(type(number) is int and number >= 0, 'invalid verified release number')
    lock = verified['sources']['lock']
    require(lock == load_json(ROOT / 'source-lock.json'), 'verified input uses a different source lock')
    context = validate_context(verified['kernel_context'])
    context_file = directory / 'kernel-context.json'
    require(context_file.is_file() and not context_file.is_symlink()
            and load_json(context_file) == context, 'kernel context asset differs from verified input')
    require(isinstance(lock['debian_snapshot'], str)
            and re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', lock['debian_snapshot']),
            'invalid Debian snapshot pin')
    records = verified['artifacts']
    require(isinstance(records, list) and len({row['file'] for row in records}) == len(records),
            'duplicate asset names')
    for row in records:
        name = basename(row['file'])
        path = directory / name
        require(path.is_file() and not path.is_symlink()
                and type(row['size']) is int and 0 < row['size'] == path.stat().st_size
                and sha256(path) == row['sha256'], f'asset bytes differ: {name}')
        if row['kind'] == 'deb':
            require(inspect_deb(path) == {field: row[field] for field in
                    ('package', 'version', 'architecture', 'depends')}, f'package metadata differs: {name}')
    binaries = validate_cohort(records, number, context['abi_sha256'])
    require(set(PACKAGE_ARCHES) == {entry['package'] for entry in binaries}, 'binary cohort mismatch')
    return verified, context, {entry['package']: entry for entry in binaries}


def bootstrap(root, snapshot):
    require(KEYRING.is_file(), 'missing host Debian archive trust keyring')
    mirror = f'https://snapshot.debian.org/archive/debian/{snapshot}/'
    source = f'deb [signed-by={KEYRING} check-valid-until=no] {mirror} trixie main'
    # Extract has no implicit Essential set. Select the Debian base explicitly;
    # APT resolves their exact versions from the locked signed snapshot.
    base = ('apt,base-files,base-passwd,bash,coreutils,dash,debianutils,diffutils,dpkg,'
            'findutils,grep,gzip,hostname,init-system-helpers,libc-bin,login,'
            'mount,passwd,perl-base,sed,sysvinit-utils,tar,util-linux,'
            'debian-archive-keyring,ca-certificates,systemd-sysv')
    command(['mmdebstrap', '--mode=root', '--variant=extract', '--architectures=riscv64',
             '--include=' + base, '--skip=cleanup/apt/cache',
             '--aptopt=Acquire::Check-Valid-Until "false"',
             '--keyring', KEYRING, 'trixie', root, source])
    (root / 'dev').mkdir(exist_ok=True)
    null = root / 'dev/null'
    null.unlink(missing_ok=True)
    os.mknod(null, stat.S_IFCHR | 0o666, os.makedev(1, 3))
    null.chmod(0o666)
    (root / 'etc/resolv.conf').write_bytes(Path('/etc/resolv.conf').read_bytes())
    (root / 'usr/bin').mkdir(parents=True, exist_ok=True)
    qemu = shutil.which('qemu-riscv64-static')
    require(qemu is not None, 'missing static RISC-V QEMU')
    shutil.copy2(qemu, root / 'usr/bin/qemu-riscv64-static')
    require((root / 'bin/true').is_file(), 'Debian base lacks target /bin/true')
    require(command(['chroot', root, '/bin/true'], check=False).returncode == 0,
            'RISC-V binfmt is not usable; register it before running the smoke')
    # Signed-By remains scoped to Debian; expiry exception belongs only to this frozen snapshot.
    (root / 'etc/apt/sources.list').write_text(source + '\n')
    (root / 'etc/apt/apt.conf.d/99mmdebstrap').unlink(missing_ok=True)


def initialize_base(root):
    policy = root / 'usr/sbin/policy-rc.d'
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text('#!/bin/sh\nexit 101\n')
    policy.chmod(0o755)
    archives = sorted((root / 'var/cache/apt/archives').glob('*.deb'))
    require(archives and any(path.name.startswith('base-passwd_') for path in archives),
            'signed base archives were not retained')
    # Complete mmdebstrap's essential stage: bulk dpkg install runs base-passwd
    # preinst before configuration. Its documented bootstrap-only force is
    # needed for Pre-Depends ordering in an already extracted, empty database.
    # Normal configure, APT dependency checking and audit must then all pass.
    target(root, '/usr/bin/dpkg', '--install', '--force-depends',
           *('/var/cache/apt/archives/' + path.name for path in archives))
    target(root, '/usr/bin/dpkg', '--configure', '-a')
    # Debian's frozen 2025 CA bundle may not know the current CDN chain.
    # Host CA trust authenticates HTTPS transport; the pinned Debian archive
    # keyring independently authenticates the Release and package indexes.
    host_ca = Path('/etc/ssl/certs/ca-certificates.crt')
    require(host_ca.is_file(), 'missing host HTTPS CA bundle')
    shutil.copyfile(host_ca, root / 'etc/ssl/certs/ca-certificates.crt')
    target(root, '/usr/bin/apt-get', '-o', 'APT::Update::Error-Mode=any', 'update')
    require((root / 'usr/share/keyrings/debian-archive-keyring.gpg').is_file(),
            'Debian archive keyring was not installed in target')
    target(root, '/usr/bin/apt-get', 'check')
    require(target(root, '/usr/bin/dpkg', '--audit').stdout.strip() == '',
            'Debian base bootstrap left an incomplete package database')
    for path in archives:
        path.unlink()


def status(root, name):
    return target(root, '/usr/bin/dpkg-query', '-W', '-f=${Status} ${Version}', name, check=False)


def installed(root, binaries):
    for name, row in binaries.items():
        result = status(root, name)
        require(result.returncode == 0 and result.stdout.strip() == f'install ok installed {row["version"]}',
                f'wrong installed tuple: {name}: {result.stdout}')
        arch = target(root, '/usr/bin/dpkg-query', '-W', '-f=${Architecture}', name).stdout.strip()
        require(arch == row['architecture'], f'wrong installed architecture: {name}: {arch}')
    audit = target(root, '/usr/bin/dpkg', '--audit')
    require(not audit.stdout.strip(), f'dpkg audit not empty: {audit.stdout}')


def systemd_enabled(root, unit):
    result = command(['systemctl', '--root', root, 'is-enabled', unit])
    require(result.stdout.strip() == 'enabled', f'{unit} not enabled: {result.stdout}')


def readiness(root, deb):
    sentinel = root / 'usr/share/k230/status-smoke-sysfs'
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text('unset')
    for value in ('1', '0'):
        target_helper(root, value)
        require(sentinel.read_text() == value + '\n' or sentinel.read_text() == value,
                f'readiness helper did not write {value}')
    previous = sentinel.read_bytes()
    invalid = target_helper(root, 'invalid', check=False)
    require(invalid.returncode != 0 and sentinel.read_bytes() == previous,
            'invalid readiness input unexpectedly modified sysfs')
    target(root, '/usr/bin/apt-get', '-y', 'remove', 'k230-status-ready')
    require(status(root, 'k230-status-ready').stdout.strip() !=
            f'install ok installed {deb["version"]}', 'readiness removal failed')
    require(command(['systemctl', '--root', root, 'is-enabled',
                     'k230-status-ready.service'], check=False).returncode != 0,
            'readiness unit remained enabled after removal')
    target(root, '/usr/bin/dpkg', '-i', '/tmp/cyberdeck-input/' + deb['file'])
    systemd_enabled(root, 'k230-status-ready.service')
    return {'writes': ['1', '0'], 'invalid_rejected_without_mutation': True,
            'remove_reinstall_enabled': True}


def abi_guards(root, binaries, context):
    release = context['kernel_release']
    module = root / 'lib/modules' / release / 'updates/sharp-drm.ko'
    require(module.is_file(), 'installed kernel module missing')
    original = sha256(module)
    abi = root / 'usr/share/k230/kernel-abi'
    correct = (context['abi_sha256'] + '\n').encode()
    require(abi.read_bytes() == correct, 'image ABI identity differs')
    results = []
    for label, content in [('absent', None), ('wrong', b'0' * 64 + b'\n'),
                           ('newline-malformed', correct + b'\n')]:
        if content is None:
            abi.unlink()
        else:
            abi.write_bytes(content)
        for name in ('k230-kernel-abi', 'k230-sharp-drm'):
            row = binaries[name]
            rejected = target(root, '/usr/bin/dpkg', '-i', '/tmp/cyberdeck-input/' + row['file'],
                              check=False)
            require(rejected.returncode != 0, f'{name} accepted {label} image ABI')
            require(sha256(module) == original, f'{name} changed installed module on {label} ABI')
            require(status(root, name).stdout.strip() == f'install ok installed {row["version"]}',
                    f'{name} changed dpkg state on rejected upgrade')
            results.append({'package': name, 'identity': label, 'rejected': True})
    abi.write_bytes(correct)
    for name in ('k230-kernel-abi', 'k230-sharp-drm'):
        target(root, '/usr/bin/dpkg', '-i', '/tmp/cyberdeck-input/' + binaries[name]['file'])
    require(sha256(module) == original, 'positive ABI reinstall changed module bytes')
    return results, module


def service_closure(root, row):
    floors = {}
    for name in ('libc6', 'libstdc++6'):
        match = re.search(r'(?:^|,\s*)' + re.escape(name) + r' \(>= ([^)]+)\)', row['depends'])
        require(match is not None, f'missing generated {name} ABI floor')
        installed_version = status(root, name)
        require(installed_version.returncode == 0
                and installed_version.stdout.startswith('install ok installed '),
                f'{name} is not installed')
        version = installed_version.stdout.strip().split()[-1]
        require(command(['dpkg', '--compare-versions', version, 'ge', match.group(1)],
                        check=False).returncode == 0, f'{name} below generated ABI floor')
        floors[name] = {'required': match.group(1), 'installed': version}
    closure = {}
    for name in ('openjdk-21-jre-headless', 'kbd', 'libc6', 'libstdc++6'):
        result = status(root, name)
        require(result.returncode == 0 and result.stdout.startswith('install ok installed '),
                f'missing runtime dependency: {name}')
        closure[name] = result.stdout.strip().split()[-1]
    version = command(['chroot', root, '/usr/bin/qemu-riscv64-static',
                       '/usr/lib/jvm/java-21-openjdk-riscv64/bin/java', '-version']).stdout
    require(re.search(r'version "21[.\"]', version), f'not target Java 21: {version}')
    return {'packages': closure, 'floors': floors, 'java_version': version.splitlines()[0]}


def module_checks(root, module, context):
    release = context['kernel_release']
    vermagic = command(['modinfo', '-F', 'vermagic', module]).stdout.split()[0]
    require(vermagic == release, 'module vermagic release differs from context')
    owners = target(root, '/usr/bin/dpkg-query', '-S', '/lib/modules/' + release + '/updates/sharp-drm.ko',
                    check=False)
    if owners.returncode:
        owners = target(root, '/usr/bin/dpkg-query', '-S', '/usr/lib/modules/' + release + '/updates/sharp-drm.ko')
    require(owners.stdout.startswith('k230-sharp-drm:'), 'module not owned by expected package')
    command(['depmod', '--basedir', root, release])
    dry = command(['modprobe', '--dry-run', '--verbose', '--dirname', root,
                   '--set-version', release, 'sharp-drm'])
    require('sharp-drm.ko' in dry.stdout and 'insmod' in dry.stdout,
            f'modprobe did not select packaged module: {dry.stdout}')
    marker = root / 'usr/share/k230-kernel-abi/kernel-context.json'
    require(load_json(marker) == context, 'packaged marker context differs from release context')
    return {'vermagic': vermagic, 'module_sha256': sha256(module),
            'marker_equal': True, 'modprobe_dry_run': dry.stdout.strip()}


def jni(root, lock, temporary):
    for path in ('/usr/bin/cyberdeck_daemon', '/' + JAR.as_posix()):
        require((root / path.lstrip('/')).is_file(), f'missing installed service payload: {path}')
        owner = target(root, '/usr/bin/dpkg-query', '-S', path).stdout
        require(owner.startswith('cyberdeck-service:'), f'service does not own {path}')
    source = temporary / 'service-source'
    command(['git', 'init', '-q', source])
    command(['git', '-C', source, 'fetch', '--quiet', '--depth=1',
             lock['service']['repository'], lock['service']['revision']])
    revision = command(['git', '-C', source, 'rev-parse', 'FETCH_HEAD']).stdout.strip()
    require(revision == lock['service']['revision'], 'canonical service revision differs')
    jni_source = temporary / 'jni/uart_jni_bridge.cpp'
    jni_source.parent.mkdir()
    header = jni_source.with_suffix('.hpp')
    for path, dest in [('src/jni/uart_jni_bridge.cpp', jni_source),
                       ('src/jni/uart_jni_bridge.hpp', header)]:
        dest.write_bytes(subprocess.check_output(['git', '-C', str(source), 'show', f'FETCH_HEAD:{path}']))
    driver = temporary / 'bridge-smoke.cpp'
    driver.write_text(DRIVER)
    jdk = Path('/usr/lib/jvm/java-21-openjdk-amd64')
    require((jdk / 'include/jni.h').is_file(), 'native Java 21 JNI headers missing')
    binary = root / 'usr/local/bin/bridge-smoke'
    binary.parent.mkdir(parents=True, exist_ok=True)
    command(['riscv64-linux-gnu-g++', '-O2', '-std=c++20', '-I', temporary,
             '-I', jdk / 'include', '-I', jdk / 'include/linux', driver, jni_source,
             '-ldl', '-o', binary])
    require((root / JAR).is_file(), 'installed fat JAR missing')
    # Explicit QEMU in a real chroot; no service daemon or hardware access.
    result = command(['chroot', root, '/usr/bin/qemu-riscv64-static',
                      '/usr/local/bin/bridge-smoke'],
                     env=os.environ | {'JAVA_HOME': '/usr/lib/jvm/java-21-openjdk-riscv64'})
    require('UartJniBridge: Native methods registered' in result.stdout
            and 'CYBERDECK_JNI_SMOKE_OK' in result.stdout,
            f'bridge did not register JNI and complete init/disconnect: {result.stdout[-4000:]}')
    return {'canonical_revision': revision, 'compiled_bridge': sha256(jni_source),
            'jar_sha256': sha256(root / JAR), 'native_methods_registered': True,
            'init_dev_null_disconnect_destroy': True}


def same_abi_upgrade(root, binaries, number, context, temporary):
    """Upgrade real payloads/scripts with fixture-only next-cohort control metadata."""
    versions = expected_versions(number + 1, context['abi_sha256'])
    destination = root / 'tmp/cyberdeck-upgrade-fixtures'
    destination.mkdir()
    upgrades = {}
    for name, row in sorted(binaries.items()):
        unpacked = temporary / ('upgrade-' + name)
        command(['dpkg-deb', '--raw-extract', root / 'tmp/cyberdeck-input' / row['file'], unpacked])
        control = unpacked / 'DEBIAN/control'
        text, count = re.subn(r'^Version: .+$', 'Version: ' + versions[name],
                              control.read_text(), flags=re.MULTILINE)
        require(count == 1, f'upgrade fixture lacks a unique version: {name}')
        if name == 'k230-sharp-drm':
            dependency = f'k230-kernel-abi (= {row["version"]})'
            require(text.count(dependency) == 1, 'upgrade fixture marker dependency differs')
            text = text.replace(dependency, f'k230-kernel-abi (= {versions["k230-kernel-abi"]})')
        control.write_text(text)
        package = destination / (name + '-upgrade-fixture.deb')
        command(['dpkg-deb', '--root-owner-group', '--build', unpacked, package])
        upgrades[name] = inspect_deb(package) | {'kind': 'deb', 'file': package.name,
                                               'size': package.stat().st_size, 'sha256': sha256(package)}
    validate_cohort(list(upgrades.values()), number + 1, context['abi_sha256'])
    target(root, '/usr/bin/apt-get', '-y', '--no-install-recommends', 'install',
           *('/tmp/cyberdeck-upgrade-fixtures/' + row['file'] for row in upgrades.values()))
    installed(root, upgrades)
    for unit in ('k230-status-ready.service', 'cyberdeck-service.service'):
        systemd_enabled(root, unit)
    return {'fixture_only': True,
            'from_versions': {name: row['version'] for name, row in sorted(binaries.items())},
            'installed_versions': {name: row['version'] for name, row in sorted(upgrades.items())},
            'dpkg_audit': 'empty'}


def smoke(directory, output):
    require(os.geteuid() == 0, 'smoke requires root for isolated rootfs and chroot')
    require(not output.exists(), 'evidence output must not exist')
    verified, context, binaries = inspect_input(directory)
    with tempfile.TemporaryDirectory(prefix='cyberdeck-package-smoke-') as directory_name, \
            prepared_rootfs(Path(directory_name) / 'rootfs',
                            verified['sources']['lock']['debian_snapshot']) as root:
        temporary = Path(directory_name)
        target_input = root / 'tmp/cyberdeck-input'
        target_input.mkdir(parents=True)
        for entry in binaries.values():
            shutil.copyfile(directory / entry['file'], target_input / entry['file'])
        identity = root / 'usr/share/k230/kernel-abi'
        identity.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text(context['abi_sha256'] + '\n')
        debs = ['/tmp/cyberdeck-input/' + binaries[name]['file'] for name in sorted(binaries)]
        target(root, '/usr/bin/apt-get', '-y', '--no-install-recommends', 'install', *debs)
        installed(root, binaries)
        systemd_enabled(root, 'k230-status-ready.service')
        systemd_enabled(root, 'cyberdeck-service.service')
        closure = service_closure(root, binaries['cyberdeck-service'])
        ready = readiness(root, binaries['k230-status-ready'])
        guards, module = abi_guards(root, binaries, context)
        module_proof = module_checks(root, module, context)
        payloads = [module, root / JAR, root / 'usr/bin/cyberdeck_daemon',
                    root / 'usr/bin/k230-status-ready']
        hashes = {path: sha256(path) for path in payloads}
        upgrade = same_abi_upgrade(root, binaries, verified['release_number'], context, temporary)
        require(all(sha256(path) == digest for path, digest in hashes.items()),
                'same-ABI upgrade changed a compiled payload or readiness helper')
        module_checks(root, module, context)
        bridge = jni(root, verified['sources']['lock'], temporary)
        evidence = {'schema_version': 1, 'release_number': verified['release_number'],
                    'snapshot': verified['sources']['lock']['debian_snapshot'],
                    'target_runtime': RUNTIME,
                    'kernel_context_sha256': sha256(directory / 'kernel-context.json'),
                    'abi_sha256': context['abi_sha256'],
                    'baseline_installed': {name: {key: row[key] for key in
                                 ('package', 'version', 'architecture', 'depends', 'sha256')}
                                  for name, row in sorted(binaries.items())},
                    'dpkg_audit_empty': True,
                    'systemd_units_enabled': ['k230-status-ready.service', 'cyberdeck-service.service'],
                    'dependency_closure': closure, 'readiness': ready,
                    'abi_guard_rejections': guards, 'module': module_proof, 'jni': bridge,
                    'same_abi_upgrade': upgrade}
        output.mkdir(parents=True, exist_ok=False)
        (output / 'package-smoke.json').write_bytes(canonical(evidence) + b'\n')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='verified same-run asset directory')
    parser.add_argument('--output', type=Path, required=True, help='new machine-readable evidence directory')
    args = parser.parse_args()
    try:
        proof = smoke(args.input.resolve(), args.output.resolve())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'package-smoke: {error}\n')
    print(f'RISC-V package smoke passed for release {proof["release_number"]}; evidence: {args.output}/package-smoke.json')


if __name__ == '__main__':
    main()
