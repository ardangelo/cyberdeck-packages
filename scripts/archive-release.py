#!/usr/bin/env python3
"""Sign and archive a verified package cohort as one immutable GitHub Release."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from github_api import GitHub
from release_support import (asset_url, basename, canonical, inspect_deb,
                             load_json, paragraphs, release_number, repository_config,
                             require, sha256, signing_home, source_checks, validate_cohort, validate_context,
                             validate_index, validate_manifest, verify_manifest_signature)

MAX_ASSET = 2**31
RELEASE_TAG = re.compile(r'packages-v([1-9][0-9]*)\Z')
SOURCES = {'cyberdeck-service', 'k230-status-ready', 'sharp-drm'}


def verified_assets(directory, number):
    require(directory.is_dir(), 'verified transport directory missing')
    record = load_json(directory / 'verified-input.json')
    require(isinstance(record, dict) and set(record) == {'schema_version', 'release_number',
            'source_date_epoch', 'sources', 'kernel_context', 'artifacts'}, 'invalid verified transport fields')
    require(record['sources']['lock'] == load_json(Path(__file__).resolve().parents[1] / 'source-lock.json'),
            'transport source lock differs from protected checkout')
    require(type(record['schema_version']) is int and record['schema_version'] == 1
            and type(record['release_number']) is int and record['release_number'] == number,
            'verified transport release mismatch')
    require(type(record['source_date_epoch']) is int and record['source_date_epoch'] > 0,
            'invalid source date epoch')
    context = validate_context(record['kernel_context'])
    require(isinstance(record['artifacts'], list), 'invalid verified artifact list')
    entries = {}
    paths = {}
    for entry in record['artifacts']:
        require(isinstance(entry, dict) and set(entry) == ({'file', 'sha256', 'size', 'kind'} |
                ({'package', 'version', 'architecture', 'depends'} if entry.get('kind') == 'deb' else set())),
                'invalid verified artifact fields')
        name = basename(entry['file'])
        require(name not in entries and name not in {'kernel-context.json', 'snapshot-Packages',
                'release-manifest.json', 'release-manifest.json.asc', 'verified-input.json'},
                'repeated or reserved artifact basename')
        require(entry['kind'] in {'deb', 'source', 'buildinfo', 'changes'}, 'unexpected artifact kind')
        entries[name] = entry
        paths[name] = directory / name
    require(set(path.name for path in directory.iterdir()) == set(paths) | {'verified-input.json', 'kernel-context.json'},
            'transport contains missing or unverified files')
    paths['kernel-context.json'] = directory / 'kernel-context.json'
    for name, path in paths.items():
        require(path.is_file() and not path.is_symlink(), f'missing/non-regular asset: {name}')
        require(0 < path.stat().st_size < MAX_ASSET, f'asset exceeds GitHub size bound: {name}')
        if name != 'kernel-context.json':
            entry = entries[name]
            require(type(entry['size']) is int and entry['size'] == path.stat().st_size
                    and entry['sha256'] == sha256(path), f'asset differs from verified record: {name}')
            if entry['kind'] == 'deb':
                require(inspect_deb(path) == {key: entry[key] for key in ('package', 'version', 'architecture', 'depends')},
                        f'Debian metadata differs from verified record: {name}')
    require(load_json(paths['kernel-context.json']) == context, 'context asset differs from verified context')
    validate_cohort(list(entries.values()), number, context['abi_sha256'])
    source_checks(entries, paths)
    return record, entries, paths


def golden_index(paths, artifacts, suite, tag, stage):
    pool = stage / 'pool' / suite / tag
    pool.mkdir(parents=True)
    for entry in sorted((e for e in artifacts if e['kind'] == 'deb'), key=lambda e: e['file']):
        os.link(paths[entry['file']], pool / entry['file'])
    relative = f'pool/{suite}/{tag}'
    generated = subprocess.check_output(['apt-ftparchive', 'packages', relative], cwd=stage)
    blocks = [block + b'\n\n' for block in generated.strip().split(b'\n\n')]
    controls = [(paragraphs(block)[0], block) for block in blocks]
    require(len(controls) == 4, 'apt-ftparchive indexed unexpected number of binaries')
    require(len({fields['Package'] for fields, _ in controls}) == 4, 'duplicate indexed package')
    data = b''.join(block for _, block in sorted(controls, key=lambda row: row[0]['Package']))
    validate_index(data, artifacts, suite, tag)
    index = stage / 'snapshot-Packages'
    index.write_bytes(data)
    return index


def git_source(api):
    head = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], text=True).strip()
    require(re.fullmatch(r'[0-9a-f]{40}', head), 'invalid checkout commit')
    require(os.environ.get('GITHUB_REF') == 'refs/heads/main'
            and os.environ.get('GITHUB_SHA') == head, 'archive requires exact main workflow checkout')
    branch = api.json('GET', '/git/ref/heads/main')
    require(branch['object']['type'] == 'commit' and branch['object']['sha'] == head,
            'protected main advanced or checkout does not match remote main')
    return head


def tag_commit(api, tag):
    reference = api.json('GET', '/git/ref/tags/' + tag, missing_ok=True)
    if reference is None:
        return None
    obj = reference['object']
    for _ in range(4):
        if obj['type'] == 'commit':
            return obj['sha']
        require(obj['type'] == 'tag', 'tag does not identify a commit')
        obj = api.json('GET', '/git/tags/' + obj['sha'])['object']
    raise ValueError('nested tag chain exceeds bound')


def release_identity(release, tag, head):
    require(release['tag_name'] == tag and release['target_commitish'] == head,
            'existing release targets a different publication source')
    require(release.get('prerelease') is False, 'prerelease cannot authorize archive')


def check_remote_asset(api, release, entry, expected, scratch):
    require(type(entry.get('size')) is int and entry['size'] == expected.stat().st_size,
            f'existing asset size conflict: {expected.name}')
    api.asset(release, entry, scratch / expected.name, sha256(expected), expected.stat().st_size)


def archive(api, artifacts, number, config, public_key, *, head):
    tag = f'packages-v{number}'
    record, entries, paths = verified_assets(artifacts, number)
    suite = f'trixie-k230-{record["kernel_context"]["abi_sha256"]}'
    with tempfile.TemporaryDirectory(prefix='cyberdeck-archive-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        stage = Path(temporary)
        sealed = stage / 'sealed'
        sealed.mkdir()
        for name, path in paths.items():
            copy = sealed / name
            shutil.copyfile(path, copy)
            if name == 'kernel-context.json':
                require(load_json(copy) == record['kernel_context'], 'kernel context changed while sealing input')
            else:
                require(sha256(copy) == entries[name]['sha256']
                        and copy.stat().st_size == entries[name]['size'],
                        f'asset changed while sealing archive input: {name}')
        paths = {name: sealed / name for name in paths}
        index = golden_index(paths, list(entries.values()), suite, tag, stage)
        index_record = {'file': index.name, 'sha256': sha256(index), 'size': index.stat().st_size,
                        'url': asset_url(tag, index.name)}
        manifest = {'schema_version': 1, 'release_tag': tag, 'release_number': number,
                    'repository': config['repository'], 'source_commit': head,
                    'conf': record['kernel_context']['conf'], 'suite': suite,
                    'snapshot_suite': f'{suite}-release-{number}', 'snapshot_index': index_record,
                    'abi_sha256': record['kernel_context']['abi_sha256'],
                    'source_date_epoch': record['source_date_epoch'],
                    'kernel_context': {'file': 'kernel-context.json', 'sha256': sha256(paths['kernel-context.json'])},
                    'sources': record['sources'],
                    'artifacts': [entry | {'url': asset_url(tag, entry['file'])}
                                  for entry in sorted(entries.values(), key=lambda item: item['file'])]}
        validate_manifest(manifest, repository=config['repository'])
        manifest_file = stage / 'release-manifest.json'
        manifest_file.write_bytes(canonical(manifest) + b'\n')
        signature = stage / 'release-manifest.json.asc'
        files = paths | {index.name: index, manifest_file.name: manifest_file}
        releases = api.releases()
        known = [r for r in releases if isinstance(r.get('tag_name'), str) and RELEASE_TAG.fullmatch(r['tag_name'])]
        require(len({r['tag_name'] for r in known}) == len(known), 'duplicate release tags')
        existing = next((release for release in known if release['tag_name'] == tag), None)
        if existing is None or existing['draft'] is True:
            for release in known:
                match = RELEASE_TAG.fullmatch(release['tag_name'])
                if release['id'] != (existing or {}).get('id'):
                    require(int(match[1]) < number, 'release number must exceed all existing package releases')
        # Drafts pin target_commitish; GitHub may create their tag only at publication.
        remote_tag = tag_commit(api, tag)
        require(remote_tag is None or remote_tag == head, 'Git tag commit differs from checked-out main')
        if existing is not None:
            release_identity(existing, tag, head)
            if existing['draft'] is False:
                require(remote_tag == head, 'published release lacks matching Git tag')
            remote = api.assets(existing)
            require(set(remote) <= set(files) | {signature.name}, 'draft/published release has unexpected assets')
            with tempfile.TemporaryDirectory(prefix='cyberdeck-check-', dir=os.environ.get('RUNNER_TEMP')) as check_dir:
                check = Path(check_dir)
                if existing['draft'] is False:
                    require(existing.get('immutable') is True, 'published release is not immutable')
                    require(set(remote) == set(files) | {signature.name}, 'published release has missing assets')
                    for name, path in files.items():
                        check_remote_asset(api, existing, remote[name], path, check)
                    api.metadata(existing, remote[signature.name], check / signature.name)
                    verify_manifest_signature(manifest_file, check / signature.name, public_key, config)
                    return tag
                for name, entry in remote.items():
                    if name == signature.name:
                        continue
                    check_remote_asset(api, existing, entry, files[name], check)
                if signature.name in remote:
                    require(manifest_file.name in remote, 'orphaned draft signature')
                    api.metadata(existing, remote[signature.name], check / signature.name)
                    verify_manifest_signature(manifest_file, check / signature.name, public_key, config)
                    shutil.copyfile(check / signature.name, signature)
        else:
            require(remote_tag is None, 'existing tag without a release requires operator repair')
        if existing is None or signature.name not in remote:
            with signing_home(public_key, config['signing_fingerprint']) as sign:
                sign(manifest_file, signature)
            verify_manifest_signature(manifest_file, signature, public_key, config)
        # A resumed draft keeps its previously verified signature; GPG signatures
        # need not have reproducible timestamps.
        files[signature.name] = signature
        for name, path in files.items():
            require(path.is_file() and 0 < path.stat().st_size < MAX_ASSET, f'invalid upload asset: {name}')
        if existing is None:
            existing = api.json('POST', '/releases', {'tag_name': tag, 'target_commitish': head,
                        'name': tag, 'draft': True, 'prerelease': False})
            release_identity(existing, tag, head)
            require(existing['draft'] is True, 'new release is not a draft')
            require(tag_commit(api, tag) in (None, head), 'new release tag differs from checked-out main')
            remote = api.assets(existing)
            require(not remote, 'new draft unexpectedly contains assets')
        for name, path in files.items():
            if name not in remote:
                uploaded = api.upload(existing, path)
                require(uploaded['name'] == name and uploaded['size'] == path.stat().st_size,
                        'GitHub uploaded asset identity/size mismatch')
        remote = api.assets(existing)
        require(set(remote) == set(files), 'draft assets incomplete or unexpected')
        with tempfile.TemporaryDirectory(prefix='cyberdeck-draft-proof-', dir=os.environ.get('RUNNER_TEMP')) as check_dir:
            for name, path in files.items():
                check_remote_asset(api, existing, remote[name], path, Path(check_dir))
        require(tag_commit(api, tag) in (None, head), 'Git tag commit differs before publication')
        updated = api.json('PATCH', f'/releases/{existing["id"]}', {'draft': False})
        require(updated['draft'] is False, 'GitHub did not publish release')
        published = api.release(tag)
        require(published is not None and published['id'] == existing['id'] and published['draft'] is False
                and published.get('immutable') is True, 'published release immutability not confirmed')
        require(tag_commit(api, tag) == head, 'published Git tag commit mismatch')
        public_assets = api.assets(published)
        require(set(public_assets) == set(files), 'public release assets differ from verified draft')
        with tempfile.TemporaryDirectory(prefix='cyberdeck-public-proof-', dir=os.environ.get('RUNNER_TEMP')) as check_dir:
            for name, path in files.items():
                check_remote_asset(api, published, public_assets[name], path, Path(check_dir))
        return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--release-number', required=True)
    parser.add_argument('--repository-config', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    args = parser.parse_args()
    try:
        number = release_number(args.release_number)
        config = repository_config(args.repository_config)
        require(os.environ.get('GITHUB_REPOSITORY') == config['repository'],
                'archive must run in the protected publication repository')
        require(args.public_key.is_file(), 'public signing key missing')
        api = GitHub(config['repository'])
        require(api.token, 'archive requires authenticated GitHub API access')
        head = git_source(api)
        tag = archive(api, args.artifacts, number, config, args.public_key, head=head)
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'archive-release: {error}\n')
    print(f'archived immutable {tag}')


if __name__ == '__main__':
    main()
