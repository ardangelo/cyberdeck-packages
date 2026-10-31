#!/usr/bin/env python3
"""Reconstruct signed, ABI-separated APT suites solely from immutable GitHub Releases."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import html
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote

from github_api import GitHub
from release_support import (REPOSITORY, check_asset_url, inspect_deb, load_json, paragraphs,
                             repository_config, require, sha256, signing_home, source_checks, validate_context,
                             validate_index, verify_manifest_signature, verify_signature)

LIMIT = 900 * 1024 * 1024
TAG = re.compile(r'packages-v([1-9][0-9]*)\Z')
MANIFEST = 'release-manifest.json'
SIGNATURE = MANIFEST + '.asc'


def tag_commit(api, tag):
    ref = api.json('GET', '/git/ref/tags/' + quote(tag, safe=''))
    require(isinstance(ref, dict) and ref.get('ref') == 'refs/tags/' + tag,
            f'missing or incorrect tag ref: {tag}')
    obj = ref.get('object')
    for _ in range(8):
        require(isinstance(obj, dict) and obj.get('type') in {'tag', 'commit'}
                and re.fullmatch(r'[0-9a-f]{40}', obj.get('sha', '')), 'invalid tag object')
        if obj['type'] == 'commit':
            return obj['sha']
        obj = api.json('GET', '/git/tags/' + obj['sha']).get('object')
    raise ValueError('tag annotation nesting exceeds limit')


def eligible(api):
    releases = {}
    for release in api.releases():
        if release.get('draft') is True or not str(release.get('tag_name', '')).startswith('packages-v'):
            continue
        tag = release['tag_name']
        match = TAG.fullmatch(tag)
        require(match is not None and release.get('prerelease') is False,
                f'invalid published package release: {tag}')
        number = int(match[1])
        require(number not in releases, f'duplicate release number: {number}')
        require(release.get('immutable') is True, f'package release is not immutable: {tag}')
        releases[number] = release
    return sorted(releases.items())


def asset_record(entry, filename, size, tag):
    require(isinstance(entry, dict) and entry.get('name') == filename
            and type(entry.get('size')) is int and entry['size'] == size
            and type(entry.get('id')) is int and entry['id'] > 0
            and entry.get('state') == 'uploaded', f'missing or conflicting release asset: {tag}/{filename}')
    check_asset_url(entry.get('browser_download_url'), tag, filename)


def fetch_release(api, release, work, key, config):
    tag = release['tag_name']
    assets = api.assets(release)
    require(MANIFEST in assets and SIGNATURE in assets, f'missing signed manifest: {tag}')
    manifest_path, signature_path = work / MANIFEST, work / SIGNATURE
    api.metadata(release, assets[MANIFEST], manifest_path)
    api.metadata(release, assets[SIGNATURE], signature_path)
    manifest = verify_manifest_signature(manifest_path, signature_path, key, config)
    require(manifest['release_tag'] == tag and manifest['source_commit'] == tag_commit(api, tag),
            f'tag/commit differs from signed manifest: {tag}')
    expected = {MANIFEST: manifest_path.stat().st_size, SIGNATURE: signature_path.stat().st_size,
                'kernel-context.json': None,
                manifest['snapshot_index']['file']: manifest['snapshot_index']['size']}
    for artifact in manifest['artifacts']:
        expected[artifact['file']] = artifact['size']
    require(set(assets) == set(expected), f'missing or undeclared release assets: {tag}')
    for name, size in expected.items():
        if size is None:
            size = assets[name].get('size')
            require(type(size) is int and 0 < size < 2**31, f'invalid context asset size: {tag}')
            expected[name] = size
        require(0 < size < 2**31, f'oversized release asset: {tag}/{name}')
        asset_record(assets[name], name, size, tag)
    context = api.asset(release, assets['kernel-context.json'], work / 'kernel-context.json',
                        manifest['kernel_context']['sha256'], expected['kernel-context.json'])
    value = validate_context(load_json(context))
    require(value['abi_sha256'] == manifest['abi_sha256'] and value['conf'] == manifest['conf'],
            f'kernel context/provenance mismatch: {tag}')
    index_entry = manifest['snapshot_index']
    golden = api.asset(release, assets['snapshot-Packages'], work / 'snapshot-Packages',
                       index_entry['sha256'], index_entry['size']).read_bytes()
    validate_index(golden, manifest['artifacts'], manifest['suite'], tag)
    require(golden.endswith(b'\n'), f'index must end in newline: {tag}')
    paths = {}
    for artifact in manifest['artifacts']:
        entry = assets[artifact['file']]
        # Check source/build descriptors, but keep large source archives off Pages.
        if artifact['kind'] == 'deb' or artifact['file'].endswith(('.dsc', '.changes', '.buildinfo')):
            binary = api.asset(release, entry, work / artifact['file'], artifact['sha256'], artifact['size'])
            paths[artifact['file']] = binary
        if artifact['kind'] == 'deb':
            require(inspect_deb(binary) == {key: artifact[key] for key in ('package', 'version', 'architecture', 'depends')},
                    f'binary metadata differs from signed cohort: {tag}/{artifact["file"]}')
    source_checks({entry['file']: entry for entry in manifest['artifacts']}, paths)
    return manifest, golden


def withdrawals(path, releases, *, history=True):
    path = Path(path)
    entries = load_json(path)
    require(isinstance(entries, list), 'withdrawals must be a JSON array')
    seen = set()
    available = {release['tag_name'] for _, release in releases}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'tag', 'reason'}
                and entry['tag'] in available and entry['tag'] not in seen
                and isinstance(entry['reason'], str) and entry['reason'].strip()
                and len(entry['reason']) <= 2048, 'invalid, unknown or duplicate withdrawal')
        seen.add(entry['tag'])
    if history:
        # Compare each committed version, including the checkout's HEAD. No site state is read.
        root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'],
                                            cwd=path.parent, text=True).strip()).resolve()
        relative = path.resolve().relative_to(root)
        require(subprocess.check_output(['git', 'rev-parse', '--is-shallow-repository'],
                                        cwd=root, text=True).strip() == 'false',
                'withdrawal enforcement requires complete Git history')
        log = subprocess.run(['git', 'log', '--format=%H', '--', str(relative)],
                             cwd=root, text=True, capture_output=True, check=True)
        earlier = []
        for commit in reversed(log.stdout.splitlines()):
            old = subprocess.run(['git', 'show', f'{commit}:{relative.as_posix()}'],
                                 cwd=root, text=True, capture_output=True, check=True)
            import json
            previous = json.loads(old.stdout)
            require(isinstance(previous, list) and previous[:len(earlier)] == earlier,
                    'withdrawal history is not append-only')
            earlier = previous
        require(entries[:len(earlier)] == earlier, 'withdrawals must extend committed history')
    return {entry['tag']: entry['reason'] for entry in entries}


def gzip_index(data):
    return gzip.compress(data, compresslevel=9, mtime=0)


def release_metadata(suite, index, now):
    date = lambda value: value.strftime('%a, %d %b %Y %H:%M:%S +0000')
    lines = ['Origin: Cyberdeck', 'Label: Cyberdeck', f'Suite: {suite}', f'Codename: {suite}',
             'Architectures: riscv64', 'Components: main', f'Date: {date(now)}',
             f'Valid-Until: {date(now + timedelta(days=14))}']
    for algorithm in ('SHA256', 'SHA512'):
        lines.append(f'{algorithm}:')
        for name in sorted(index):
            blob = index[name]
            digest = hashlib.new(algorithm.lower(), blob).hexdigest()
            lines.append(f' {digest} {len(blob)} {name}')
    return ('\n'.join(lines) + '\n').encode()


def verify_clear_signature(clear, key, fingerprint):
    with tempfile.TemporaryDirectory(prefix='cyberdeck-clear-') as temporary:
        ring = Path(temporary) / 'keys.gpg'
        subprocess.run(['gpg', '--batch', '--dearmor', '--output', str(ring), str(key)], check=True, capture_output=True)
        result = subprocess.run(['gpgv', '--keyring', str(ring), '--status-fd', '1', str(clear)],
                                text=True, capture_output=True)
        signatures = [line.split() for line in result.stdout.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
        require(result.returncode == 0 and len(signatures) == 1 and signatures[0][-1] == fingerprint,
                'InRelease signature not authorized by current key')


def write_suite(site, suite, data, now, signer, key, fingerprint):
    directory = site / 'dists' / suite
    binary = directory / 'main/binary-riscv64'
    binary.mkdir(parents=True)
    indices = {'main/binary-riscv64/Packages': data,
               'main/binary-riscv64/Packages.gz': gzip_index(data)}
    for filename, blob in indices.items():
        (directory / filename).write_bytes(blob)
    metadata = directory / 'Release'
    metadata.write_bytes(release_metadata(suite, indices, now))
    if signer is not None:
        signer(metadata, directory / 'Release.gpg')
        signer(metadata, directory / 'InRelease', clear=True)
        verify_signature(metadata, directory / 'Release.gpg', key, fingerprint)
        verify_clear_signature(directory / 'InRelease', key, fingerprint)
    return indices


def catalog(site, cohorts, withdrawn, config):
    fingerprints = [config['signing_fingerprint']] + [entry['fingerprint'] for entry in config.get('historical_keys', [])]
    body = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Cyberdeck packages</title>',
            '<h1>Cyberdeck APT archive</h1><p>Current and historical primary fingerprints:</p><ul>']
    body += [f'<li><code>{html.escape(fingerprint)}</code></li>' for fingerprint in fingerprints]
    body.append('</ul><p>APT site: https://ardangelo.github.io/cyberdeck-packages/</p><ul>')
    for manifest, _ in cohorts:
        tag = manifest['release_tag']
        suites = [manifest['suite']]
        if tag not in withdrawn:
            suites.append(manifest['snapshot_suite'])
        body.append(f'<li>{html.escape(tag)} (ABI <code>{html.escape(manifest["abi_sha256"])}</code>): '
                    + ', '.join(html.escape(suite) for suite in suites))
        if tag in withdrawn:
            body.append(' — withdrawn: ' + html.escape(withdrawn[tag]))
        body.append('<ul>')
        for artifact in manifest['artifacts']:
            if artifact['kind'] in {'source', 'deb'}:
                body.append(f'<li>{html.escape(artifact["kind"])}: '
                            f'<a href="{html.escape(artifact["url"], quote=True)}">'
                            f'{html.escape(artifact["file"])}</a></li>')
        body.append('</ul></li>')
    body.append('</ul></html>\n')
    (site / 'index.html').write_text(''.join(body))
    (site / '.nojekyll').write_bytes(b'')


def assemble(api, output, key, config, withdrawn_path, *, dry_run=False, now=None, signer=None, history=True):
    output = Path(output)
    require(not output.is_symlink() and (not output.exists() or (output.is_dir() and not any(output.iterdir()))),
            'output must be absent or an empty non-symlink directory')
    require(Path(key).is_file(), 'real committed public signing key is required')
    require(dry_run or signer is not None, 'signed publication requires a configured signing key')
    now = now or datetime.now(timezone.utc)
    require(now.tzinfo is not None and now.utcoffset() == timedelta(0), 'publication clock must be UTC')
    releases = eligible(api)
    require(releases, 'no immutable package releases available; refusing an empty publication')
    withdrawn = withdrawals(withdrawn_path, releases, history=history)
    with tempfile.TemporaryDirectory(prefix='cyberdeck-publish-', dir=output.parent) as temporary:
        work = Path(temporary)
        site = work / 'site'
        site.mkdir()
        cohorts = []
        live = {}
        for number, release in releases:
            archive = work / f'archive-{number}'
            archive.mkdir()
            manifest, index = fetch_release(api, release, archive, key, config)
            require(manifest['release_number'] == number, 'release number differs from tag')
            cohorts.append((manifest, index))
            pool = site / 'pool' / manifest['suite'] / manifest['release_tag']
            pool.mkdir(parents=True)
            for artifact in manifest['artifacts']:
                if artifact['kind'] == 'deb':
                    shutil.copyfile(archive / artifact['file'], pool / artifact['file'])
            for fields in paragraphs(index):
                filename = fields['Filename']
                binary = site / filename
                require(binary.is_file() and binary.stat().st_size == int(fields['Size'])
                        and sha256(binary) == fields['SHA256'], f'index/pool mismatch: {filename}')
            if manifest['release_tag'] not in withdrawn:
                if signer is not None or dry_run:
                    write_suite(site, manifest['snapshot_suite'], index, now, signer, key,
                                config['signing_fingerprint'])
                live.setdefault(manifest['suite'], []).append(index)
        for suite, indices in live.items():
            combined = b'\n\n'.join(index.strip(b'\n') for index in indices) + b'\n\n'
            write_suite(site, suite, combined, now, signer, key, config['signing_fingerprint'])
            for fields in paragraphs(combined):
                binary = site / fields['Filename']
                require(binary.is_file() and binary.stat().st_size == int(fields['Size'])
                        and sha256(binary) == fields['SHA256'], 'live index references absent/corrupt pool bytes')
        catalog(site, cohorts, withdrawn, config)
        if dry_run:
            (site / 'DRY-RUN-NOT-DEPLOYABLE').write_text('Unsigned APT metadata; never deploy this site.\n')
        size = sum(path.stat().st_size for path in site.rglob('*') if path.is_file())
        require(size <= LIMIT, f'assembled site exceeds 900 MiB: {size} bytes')
        if output.exists():
            output.rmdir()  # Already checked empty; never replace an existing deployment.
        site.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--repository-config', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    require(args.repository == REPOSITORY, 'wrong publication repository')
    require(args.repository_config.is_file(), 'real repository.json trust configuration is required')
    require(args.public_key.is_file(), 'real committed archive public key is required')
    config = repository_config(args.repository_config)
    if args.dry_run:
        assemble(GitHub(args.repository), args.output, args.public_key, config,
                 Path(__file__).resolve().parents[1] / 'withdrawn-releases.json', dry_run=True)
    else:
        for name in ('APT_GPG_PRIVATE_KEY', 'APT_GPG_PASSPHRASE', 'APT_GPG_SIGNING_FINGERPRINT'):
            require(os.environ.get(name), f'protected signing prerequisite unavailable: {name}')
        with signing_home(args.public_key, config['signing_fingerprint']) as sign:
            assemble(GitHub(args.repository), args.output, args.public_key, config,
                     Path(__file__).resolve().parents[1] / 'withdrawn-releases.json', signer=sign)


if __name__ == '__main__':
    main()
