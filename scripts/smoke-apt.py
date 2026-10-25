#!/usr/bin/env python3
"""Install a signed live or fixed Cyberdeck cohort in a disposable RISC-V rootfs."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
from urllib.parse import urlsplit

from release_support import (PACKAGE_ARCHES, canonical, download, inspect_deb, load_json,
                             paragraphs, repository_config, require, sha256, validate_cohort,
                             validate_context, validate_index, verify_manifest_signature)

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = ('-o', 'Dir::Etc::sourcelist=/etc/apt/cyberdeck-smoke-empty.list', '-o', 'Dir::Etc::sourceparts=/etc/apt/cyberdeck-smoke.sources.d',
           '-o', 'Acquire::Check-Valid-Until=true', '-o', 'Acquire::AllowInsecureRepositories=false',
           '-o', 'APT::Get::AllowUnauthenticated=false', '-o', 'APT::Update::Error-Mode=any')


def live_index(data, manifest):
    """Validate every retained cohort, then require this manifest's exact index rows."""
    cohorts = {}
    abi, suite = manifest['abi_sha256'], manifest['suite']
    for row in paragraphs(data):
        require(row.get('Package') in PACKAGE_ARCHES, 'live index contains an unauthorized binary')
        match = re.fullmatch(r'pool/' + re.escape(suite) + r'/packages-v([1-9][0-9]*)/([A-Za-z0-9][A-Za-z0-9_.+~-]*)', row.get('Filename', ''))
        require(match is not None, 'live index crosses ABI or release pool boundaries')
        number = int(match[1])
        digest = row.get('SHA256', '')
        require(re.fullmatch('[0-9a-f]{64}', digest) and row.get('Size', '').isdigit(), 'live index lacks strong package bytes')
        entry = {'kind': 'deb', 'file': match[2], 'package': row['Package'], 'version': row['Version'],
                 'architecture': row['Architecture'], 'depends': row.get('Depends', ''),
                 'sha256': digest, 'size': int(row['Size'])}
        cohorts.setdefault(number, []).append(entry)
    require(manifest['release_number'] in cohorts, 'expected cohort is absent from live suite')
    for number, entries in cohorts.items():
        validate_cohort(entries, number, abi)
    expected = {entry['package']: entry for entry in manifest['artifacts'] if entry['kind'] == 'deb'}
    for entry in cohorts[manifest['release_number']]:
        require(all(entry[field] == expected[entry['package']][field] for field in entry), 'live cohort differs from immutable manifest')


def smoke(uri, suite, key, manifest_path, signature, config_path, context_path=None, golden_path=None):
    config = repository_config(config_path)
    manifest = verify_manifest_signature(manifest_path, signature, key, config)
    require(suite in {manifest['suite'], manifest['snapshot_suite']}, 'suite is outside expected signed manifest')
    url = urlsplit(uri)
    require(url.scheme == 'https' or (url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1', 'localhost'}), 'APT smoke requires HTTPS or a loopback-only fixture')
    require(url.hostname and not url.username and not url.password and not url.query and not url.fragment, 'invalid APT URI')
    require(not any(character.isspace() for character in uri), 'APT URI contains whitespace')
    helpers = runpy.run_path(str(ROOT / 'scripts/smoke-packages.py'))
    target = helpers['target']
    with tempfile.TemporaryDirectory(prefix='cyberdeck-apt-smoke-') as temporary, ExitStack() as resources:
        work = Path(temporary)
        if context_path is None:
            context_path = download(f'https://github.com/{manifest["repository"]}/releases/download/{manifest["release_tag"]}/kernel-context.json',
                                    work / 'kernel-context.json', manifest['kernel_context']['sha256'])
        require(sha256(context_path) == manifest['kernel_context']['sha256'], 'context asset checksum mismatch')
        context = validate_context(load_json(context_path))
        require(context['abi_sha256'] == manifest['abi_sha256'] and context['conf'] == manifest['conf'], 'manifest/context identity mismatch')
        if golden_path is None:
            entry = manifest['snapshot_index']
            golden_path = download(entry['url'], work / entry['file'], entry['sha256'], entry['size'])
        require(sha256(golden_path) == manifest['snapshot_index']['sha256'] and Path(golden_path).stat().st_size == manifest['snapshot_index']['size'], 'golden index asset mismatch')
        golden = Path(golden_path).read_bytes()
        validate_index(golden, manifest['artifacts'], manifest['suite'], manifest['release_tag'])
        root = work / 'rootfs'
        resources.enter_context(helpers['prepared_rootfs'](root, manifest['sources']['lock']['debian_snapshot']))
        policy = root / 'usr/sbin/policy-rc.d'
        policy.write_text('#!/bin/sh\nexit 101\n')
        policy.chmod(0o755)
        rings = root / 'etc/apt/keyrings'
        rings.mkdir(parents=True, exist_ok=True)
        ring = rings / 'cyberdeck-smoke.gpg'
        with tempfile.TemporaryDirectory(prefix='cyberdeck-smoke-public-') as public_home:
            subprocess.run(['gpg', '--batch', '--homedir', public_home, '--dearmor', '--output', str(ring), str(key)], check=True, capture_output=True)
        ring.chmod(0o644)
        sources = root / 'etc/apt/cyberdeck-smoke.sources.d'
        sources.mkdir()
        (root / 'etc/apt/cyberdeck-smoke-empty.list').write_bytes(b'')
        snapshot = manifest['sources']['lock']['debian_snapshot']
        (sources / 'cohort.sources').write_text(
            f'Types: deb\nURIs: {uri}\nSuites: {suite}\nComponents: main\nArchitectures: riscv64\nSigned-By: /etc/apt/keyrings/cyberdeck-smoke.gpg\n\n'
            f'Types: deb\nURIs: https://snapshot.debian.org/archive/debian/{snapshot}/\nSuites: trixie\nComponents: main\nArchitectures: riscv64\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n')
        target(root, '/usr/bin/apt-get', *OPTIONS, 'update')
        lists = list((root / 'var/lib/apt/lists').glob(f'*_dists_{suite}_main_binary-riscv64_Packages*'))
        require(len(lists) == 1 and lists[0].is_file(), 'authenticated suite index is missing or ambiguous')
        index = target(root, '/usr/lib/apt/apt-helper', 'cat-file', '/var/lib/apt/lists/' + lists[0].name).stdout.encode()
        if suite == manifest['snapshot_suite']:
            require(index == golden, 'authenticated fixed snapshot differs from immutable golden index')
        else:
            live_index(index, manifest)
        binaries = [entry for entry in manifest['artifacts'] if entry['kind'] == 'deb']
        for entry in binaries:
            policy_output = target(root, '/usr/bin/apt-cache', *OPTIONS, 'policy', entry['package']).stdout
            candidates = re.findall(r'^\s*Candidate:\s*(\S+)\s*$', policy_output, re.MULTILINE)
            require(candidates == [entry['version']], f'wrong effective APT candidate: {entry["package"]}')
        selectors = [f'{entry["package"]}={entry["version"]}' for entry in binaries]
        simulation = target(root, '/usr/bin/apt-get', *OPTIONS, '--simulate', 'install', *selectors).stdout
        target(root, '/usr/bin/apt-get', *OPTIONS, '--yes', '--download-only', '--reinstall', 'install', *selectors)
        cached = {}
        for path in (root / 'var/cache/apt/archives').glob('*.deb'):
            metadata = inspect_deb(path)
            if metadata['package'] in PACKAGE_ARCHES:
                require(metadata['package'] not in cached, 'multiple cached core package versions')
                cached[metadata['package']] = (path, metadata)
        require(set(cached) == set(PACKAGE_ARCHES), 'missing downloaded core package')
        for entry in binaries:
            path, metadata = cached[entry['package']]
            require(metadata == {field: entry[field] for field in ('package', 'version', 'architecture', 'depends')}, 'cached binary metadata mismatch')
            require(path.stat().st_size == entry['size'] and sha256(path) == entry['sha256'], 'cached binary checksum mismatch')
        image = root / 'usr/share/k230'
        image.mkdir(parents=True, exist_ok=True)
        (image / 'kernel-abi').write_text(context['abi_sha256'] + '\n')
        (image / 'kernel-context.json').write_bytes(canonical(context) + b'\n')
        target(root, '/usr/bin/apt-get', *OPTIONS, '--yes', '--no-download', '--reinstall', 'install', *selectors)
        require(target(root, '/usr/bin/dpkg', '--audit').stdout.strip() == '', 'target dpkg audit is not empty')
        versions = {}
        for entry in binaries:
            actual = target(root, '/usr/bin/dpkg-query', '-W', '-f=${Status}|${Version}|${Architecture}', entry['package']).stdout.strip()
            require(actual == f'install ok installed|{entry["version"]}|{entry["architecture"]}', 'installed package tuple mismatch')
            versions[entry['package']] = entry['version']
        marker = load_json(root / 'usr/share/k230-kernel-abi/kernel-context.json')
        require(marker == context, 'installed marker differs from expected context')
        return {'schema_version': 1, 'uri': uri, 'suite': suite, 'release_tag': manifest['release_tag'],
                'abi_sha256': context['abi_sha256'], 'authenticated_packages_sha256': hashlib.sha256(index).hexdigest(),
                'installed_versions': versions, 'cached_sha256': {entry['package']: entry['sha256'] for entry in binaries},
                'simulation': simulation, 'dpkg_audit': 'empty', 'runtime': helpers['RUNTIME']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uri', required=True)
    parser.add_argument('--suite', required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--expected-manifest', type=Path, required=True)
    parser.add_argument('--signature', type=Path)
    parser.add_argument('--repository-config', type=Path, default=ROOT / 'repository.json')
    parser.add_argument('--context', type=Path)
    parser.add_argument('--golden-index', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        signature = args.signature or args.expected_manifest.with_name(args.expected_manifest.name + '.asc')
        if args.output:
            args.output.mkdir(parents=True, exist_ok=False)
        result = smoke(args.uri, args.suite, args.key, args.expected_manifest, signature,
                       args.repository_config, args.context, args.golden_index)
        encoded = canonical(result) + b'\n'
        if args.output:
            (args.output / 'apt-smoke.json').write_bytes(encoded)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'smoke-apt: {error}\n')


if __name__ == '__main__':
    main()
