#!/usr/bin/env python3
"""Inspect same-run package outputs before any archive/signing job can consume them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

from release_support import (basename, canonical, inspect_deb, load_json, release_number,
                             require, sha256, source_checks, validate_cohort, validate_context)
from sources import fetch

ROOT = Path(__file__).resolve().parents[1]
SELECTORS = {'status-ready', 'cyberdeck-service', 'sharp-drm'}


def payload(path, context):
    metadata = inspect_deb(path)
    package = metadata['package']
    names = set()
    marker = None
    with tempfile.TemporaryDirectory(prefix='cyberdeck-payload-') as temporary:
        module = Path(temporary) / 'sharp-drm.ko'
        with subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(path)], stdout=subprocess.PIPE) as process:
            with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
                for entry in archive:
                    name = PurePosixPath(entry.name).as_posix()
                    require(not name.startswith('/') and '..' not in PurePosixPath(name).parts, 'unsafe binary payload path')
                    names.add(name)
                    require(name not in {'usr/share/k230/kernel-abi', 'usr/share/k230/kernel-context.json'}, 'package owns image kernel identity')
                    if package == 'k230-kernel-abi' and name == 'usr/share/k230-kernel-abi/kernel-context.json':
                        require(entry.isfile(), 'marker context is not a regular file')
                        marker = json.load(archive.extractfile(entry))
                    if name == f'lib/modules/{context["kernel_release"]}/updates/sharp-drm.ko':
                        require(entry.isfile(), 'module payload is not a regular file')
                        with module.open('xb') as output:
                            shutil.copyfileobj(archive.extractfile(entry), output)
            require(process.wait() == 0, 'cannot inspect binary payload')
        if package == 'k230-sharp-drm':
            require(module.is_file(), 'package lacks expected kernel module')
            release = subprocess.check_output(['modinfo', '-F', 'vermagic', str(module)], text=True).split()[0]
            require(release == context['kernel_release'], 'compiled module vermagic/context mismatch')
            require({'etc/modules-load.d/k230-sharp-drm.conf', 'etc/modprobe.d/k230-sharp-drm.conf'} <= names, 'module configuration payload missing')
        elif package == 'k230-kernel-abi':
            require(marker == context, 'packaged ABI marker/context mismatch')
        elif package == 'cyberdeck-service':
            require({'usr/bin/cyberdeck_daemon', 'usr/lib/cyberdeck-service/libpebble3-jvm-fat.jar',
                     'usr/share/doc/cyberdeck-service/dependency-licenses/dependencies.json'} <= names,
                    'service daemon, JAR or uncompressed dependency index missing')
            require(not any(name.endswith(('/fake_uinput', '/jni_test_daemon')) for name in names), 'undeclared service test binary installed')
            require(any(f'{directory}/cyberdeck-service.service' in names for directory in ('usr/lib/systemd/system', 'lib/systemd/system')), 'service unit missing')
        elif package == 'k230-status-ready':
            require('usr/bin/k230-status-ready' in names, 'readiness helper missing')
            require(any(f'{directory}/k230-status-ready.service' in names for directory in ('usr/lib/systemd/system', 'lib/systemd/system')), 'readiness unit missing')
            require(not any(name.startswith('etc/systemd/system/') for name in names), 'readiness ships manual enablement state')


def verify(artifacts, output, number):
    lock = load_json(ROOT / 'source-lock.json')
    records = [load_json(path) | {'_directory': path.parent} for path in artifacts.rglob('*-build.json')]
    require(len(records) == 3 and {r['package'] for r in records} == SELECTORS, 'need exactly one build record per declared source')
    files = {}
    context = None
    submodules = None
    epochs = []
    for record in records:
        require(record['schema_version'] == 1 and record['release_number'] == number and record['conf'] == lock['sdk']['conf'], 'build record release/config mismatch')
        require(record['sources']['lock'] == lock, 'build jobs used different source locks')
        epoch = record['source_date_epoch']
        require(type(epoch) is int and epoch > 0, 'invalid build source epoch')
        epochs.append(epoch)
        if record['package'] == 'cyberdeck-service':
            submodules = record['sources']['submodules']
        if record['package'] == 'sharp-drm':
            entry = record['kernel_context']
            require(entry['file'] == 'kernel-context.json', 'unexpected context asset name')
            source = record['_directory'] / entry['file']
            require(source.is_file() and not source.is_symlink() and sha256(source) == entry['sha256'], 'kernel context asset bytes mismatch')
            context = validate_context(load_json(source))
        for entry in record['artifacts']:
            name = basename(entry['file'])
            require(name not in files, 'conflicting artifact basename across build jobs')
            source = record['_directory'] / name
            require(source.is_file() and not source.is_symlink(), 'missing or non-regular artifact')
            require(0 < entry['size'] < 2**31 and source.stat().st_size == entry['size'] and sha256(source) == entry['sha256'], 'artifact bytes mismatch')
            require(entry['kind'] in {'deb', 'source', 'buildinfo', 'changes'}, 'undeclared artifact kind')
            if entry['kind'] == 'deb':
                require(inspect_deb(source) == {key: entry[key] for key in ('package', 'version', 'architecture', 'depends')}, 'recorded binary tuple differs from package')
            files[name] = entry | {'_path': source}
    require(context is not None and context['conf'] == lock['sdk']['conf'], 'missing or wrong-board context')
    require(submodules is not None, 'missing service submodule provenance')
    with tempfile.TemporaryDirectory(prefix='cyberdeck-verify-source-') as temporary:
        actual = fetch(lock['service']['repository'], lock['service']['revision'], Path(temporary) / 'service', recursive=True)
        require(sorted(actual, key=lambda e: e['path']) == sorted(submodules, key=lambda e: e['path']), 'service gitlink provenance differs from canonical revision')
    binaries = validate_cohort(list(files.values()), number, context['abi_sha256'])
    for entry in binaries:
        payload(entry['_path'], context)
    source_checks(files, {name: entry['_path'] for name, entry in files.items()})
    output.mkdir(parents=True, exist_ok=False)
    for name, entry in files.items():
        shutil.copyfile(entry['_path'], output / name)
    (output / 'kernel-context.json').write_bytes(canonical(context) + b'\n')
    result = {'schema_version': 1, 'release_number': number, 'source_date_epoch': max(epochs),
              'sources': {'lock': lock, 'submodules': submodules}, 'kernel_context': context,
              'artifacts': [{key: value for key, value in entry.items() if not key.startswith('_')} for entry in files.values()]}
    (output / 'verified-input.json').write_bytes(canonical(result) + b'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--release-number', required=True)
    args = parser.parse_args()
    try:
        number = release_number(args.release_number, development=True)
        result = verify(args.artifacts, args.output, number)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'verify-artifacts: {error}\n')
    print(f'verified four binaries and complete sources for release {result["release_number"]}')


if __name__ == '__main__':
    main()
