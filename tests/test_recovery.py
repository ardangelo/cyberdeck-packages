"""Behavioral recovery policy checks using actual temporary Git trees and ZIP bytes."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recovery


WORKFLOW = '''name: Package release
on:
  workflow_dispatch:
    inputs:
      release_number:
        type: string
env:
  BUILD_FLAG: base
jobs:
  build:
    strategy:
      matrix:
        package: [status-ready, cyberdeck-service, sharp-drm]
    steps:
      - run: python3 scripts/build-packages.py --package "${{ matrix.package }}"
  verify:
    steps:
      - run: python3 scripts/verify-artifacts.py
  archive:
    steps:
      - run: python3 scripts/archive-release.py
'''


def command(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], stderr=subprocess.PIPE).decode().strip()


@contextmanager
def history():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        command(root, 'init', '-q', '-b', 'main')
        command(root, 'config', 'user.email', 'test@example.invalid')
        command(root, 'config', 'user.name', 'Recovery Test')
        files = {
            '.github/workflows/release.yml': WORKFLOW,
            'source-lock.json': '{"schema_version":1}\n',
            'containers/service.Dockerfile': 'FROM scratch\n',
            'packages/status-ready/debian/control': 'Source: status-ready\n',
            **{f'scripts/{name}': '# helper\n' for name in recovery.BUILD_SCRIPTS},
        }
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        command(root, 'add', '.')
        command(root, 'commit', '-qm', 'original release')
        original = command(root, 'rev-parse', 'HEAD')
        yield root, original


def commit(root, name, contents=None, *, executable=False):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents if contents is not None else path.read_text() + '\nchanged\n')
    if executable:
        path.chmod(0o755)
    command(root, 'add', name)
    command(root, 'commit', '-qm', 'adjust ' + name)
    return command(root, 'rev-parse', 'HEAD')


def metadata(original):
    run_id, repo_id = 123, 456
    run = {'id': run_id, 'repository': {'full_name': recovery.REPOSITORY, 'id': repo_id},
           'head_repository': {'full_name': recovery.REPOSITORY, 'id': repo_id},
           'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': original,
           'path': recovery.WORKFLOW, 'workflow_id': 42, 'status': 'completed',
           'conclusion': 'failure', 'run_attempt': 1}
    artifact = {'id': 789, 'name': 'verified-packages', 'expired': False,
                'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                'size_in_bytes': 108, 'digest': 'sha256:' + 'a' * 64,
                'workflow_run': {'id': run_id, 'repository_id': repo_id,
                                 'head_repository_id': repo_id, 'head_branch': 'main',
                                 'head_sha': original}}
    jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'}
            for name in ('Build status-ready', 'Build cyberdeck-service', 'Build sharp-drm',
                         'Verify packages and smoke')]
    return run, jobs, artifact


class API:
    repository = recovery.REPOSITORY
    token = 'not-a-secret'
    root = 'https://api.github.com/repos/' + recovery.REPOSITORY

    def __init__(self, original):
        self.run, self.jobs, self.artifact = metadata(original)

    def json(self, method, path):
        if path == '/actions/runs/123':
            return self.run
        if path == '/actions/workflows/release.yml':
            return {'id': 42, 'path': recovery.WORKFLOW}
        if path == '/actions/runs/123/jobs?filter=all&per_page=100&page=1':
            return {'jobs': self.jobs}
        if path == '/actions/runs/123/artifacts?per_page=100&page=1':
            return {'artifacts': [self.artifact]}
        raise AssertionError(path)

    def headers(self, accept='application/vnd.github+json'):
        return {'Accept': accept, 'Authorization': 'Bearer ' + self.token}


class RecoveryHistoryTests(unittest.TestCase):
    def test_failed_archive_release_after_publication_only_change(self):
        with history() as (root, original):
            updated = WORKFLOW.replace('scripts/archive-release.py', 'scripts/archive-release.py --new-flag')
            head = commit(root, '.github/workflows/release.yml', updated)
            head = commit(root, 'scripts/archive-release.py', '# new publication logic\n')
            head = commit(root, 'scripts/verify-artifacts.py', '# new verification logic\n')
            head = commit(root, 'README.md', 'New instructions\n')
            source = recovery.source_build(API(original), 123, root, head)
            self.assertEqual(source['head_sha'], original)
            self.assertNotEqual(source['head_sha'], head)

    def test_archive_only_rerun_does_not_require_rebuilding_successful_original_jobs(self):
        with history() as (root, original):
            head = commit(root, 'scripts/archive-release.py', '# repaired archive logic\n')
            api = API(original)
            api.run['run_attempt'] = 2
            api.jobs.append({'name': 'Archive immutable release', 'status': 'completed', 'conclusion': 'failure'})
            source = recovery.source_build(api, 123, root, head)
            self.assertEqual(source['head_sha'], original)
            self.assertNotEqual(source['head_sha'], head)

    def test_changed_build_inputs_and_uncommitted_inputs_rejected(self):
        for name in ('source-lock.json', 'containers/service.Dockerfile',
                     'packages/status-ready/debian/control', 'scripts/build-packages.py',
                     'scripts/sources.py', 'scripts/kernel-context.py', 'scripts/release_support.py',
                     'scripts/new-build-helper.py', 'scripts/new-build-helper.sh', 'scripts/helpers/nested.py'):
            with self.subTest(name=name), history() as (root, original):
                head = commit(root, name, '# changed build input\n')
                with self.assertRaisesRegex(ValueError, 'build input changed.*' + name):
                    recovery.source_build(API(original), 123, root, head)
        with history() as (root, original):
            head = commit(root, 'scripts/build-packages.py', '# helper\n', executable=True)
            with self.assertRaisesRegex(ValueError, 'build input changed.*build-packages'):
                recovery.source_build(API(original), 123, root, head)
        for name in ('source-lock.json', 'packages/status-ready/debian/control',
                     'scripts/new-build-helper.py'):
            with self.subTest(uncommitted=name), history() as (root, original):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('uncommitted')
                with self.assertRaisesRegex(ValueError, 'uncommitted build input'):
                    recovery.source_build(API(original), 123, root, original)

    def test_semantic_workflow_build_changes_rejected_not_formatting(self):
        with history() as (root, original):
            formatting = WORKFLOW.replace('name: Package release', 'name: Package release\n# comment')
            head = commit(root, recovery.WORKFLOW, formatting)
            recovery.source_build(API(original), 123, root, head)
            (root / recovery.WORKFLOW).write_text(formatting.replace('BUILD_FLAG: base', 'BUILD_FLAG: changed'))
            with self.assertRaisesRegex(ValueError, 'uncommitted release workflow build'):
                recovery.source_build(API(original), 123, root, head)
        for changed in (WORKFLOW.replace('BUILD_FLAG: base', 'BUILD_FLAG: changed'),
                        WORKFLOW.replace('matrix.package', 'inputs.package')):
            with history() as (root, original):
                head = commit(root, recovery.WORKFLOW, changed)
                with self.assertRaisesRegex(ValueError, 'release workflow build job or top-level'):
                    recovery.source_build(API(original), 123, root, head)

    def test_original_commit_must_be_ancestor(self):
        with history() as (root, original):
            command(root, 'checkout', '--orphan', 'other')
            command(root, 'rm', '-qr', '--cached', '.')
            head = commit(root, 'unrelated.txt', 'unrelated\n')
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                recovery.source_build(API(original), 123, root, head)

    def test_wrong_source_provenance_rejected(self):
        alterations = [
            ('run repo', lambda api: api.run['repository'].update(full_name='attacker/fork')),
            ('head repo', lambda api: api.run['head_repository'].update(id=999)),
            ('event', lambda api: api.run.update(event='pull_request')),
            ('branch', lambda api: api.run.update(head_branch='other')),
            ('workflow', lambda api: api.run.update(path='.github/workflows/ci.yml')),
            ('workflow id', lambda api: api.run.update(workflow_id=999)),
            ('status', lambda api: api.run.update(status='in_progress')),
            ('run id', lambda api: api.run.update(id=111)),
            ('build job', lambda api: api.jobs[1].update(conclusion='failure')),
            ('smoke job', lambda api: api.jobs[3].update(conclusion='skipped')),
            ('artifact ID', lambda api: api.artifact.update(id=0)),
            ('artifact binding', lambda api: api.artifact['workflow_run'].update(head_sha='f'*40)),
            ('artifact repo', lambda api: api.artifact['workflow_run'].update(repository_id=999)),
            ('artifact expiry', lambda api: api.artifact.update(expired=True)),
            ('past expiry', lambda api: api.artifact.update(expires_at='2020-01-01T00:00:00Z')),
            ('digest', lambda api: api.artifact.update(digest=None)),
            ('size', lambda api: api.artifact.update(size_in_bytes=0)),
        ]
        with history() as (root, original):
            for label, change in alterations:
                with self.subTest(label=label):
                    api = API(original)
                    change(api)
                    with self.assertRaises(ValueError):
                        recovery.source_build(api, 123, root, original)
            api = API(original)
            api.artifact['expired'] = True
            extra = deepcopy(api.artifact)
            class Duplicates(API):
                def json(self, method, path):
                    if '/artifacts?' in path:
                        return {'artifacts': [self.artifact, extra]}
                    return super().json(method, path)
            dup = Duplicates(original)
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                recovery.source_build(dup, 123, root, original)


def archive_bytes(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        for entry in members:
            if len(entry) == 2:
                name, data = entry
                bundle.writestr(name, data)
            else:
                name, data, mode = entry
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                bundle.writestr(info, data)
    return stream.getvalue()


class ByteStream:
    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def read(self, amount):
        return self.stream.read(amount)


class DownloadAPI(API):
    def __init__(self, data):
        self.data = data
        self.opener = self

    def open(self, request: Request, timeout):
        return ByteStream(self.data)


class RecoveryBytesTests(unittest.TestCase):
    def source(self, data):
        return {'artifact_id': 789, 'artifact_size': len(data),
                'artifact_sha256': hashlib.sha256(data).hexdigest()}

    def test_verified_download_and_flat_extract(self):
        data = archive_bytes([('verified-input.json', '{}'), ('package.deb', b'package')])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            api = DownloadAPI(data)
            archive = recovery.download_artifact(api, self.source(data), root / 'source.zip')
            self.assertEqual((recovery.extract_verified(archive, self.source(data), root / 'verified') /
                              'package.deb').read_bytes(), b'package')
            with self.assertRaisesRegex(ValueError, 'replace'):
                recovery.download_artifact(api, self.source(data), archive)
            with self.assertRaisesRegex(ValueError, 'replace'):
                recovery.extract_verified(archive, self.source(data), root / 'verified')

    def test_rejects_download_corruption_and_does_not_publish_partial(self):
        data = archive_bytes([('package.deb', b'package')])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for received, source in ((data[:-1], self.source(data)),
                                     (data + b'x', self.source(data)),
                                     (data, self.source(data + b'x'))):
                api = DownloadAPI(received)
                target = root / 'source.zip'
                with self.assertRaises(ValueError):
                    recovery.download_artifact(api, source, target)
                self.assertFalse(target.exists())
                self.assertFalse((root / 'source.zip.partial').exists())
            archive = root / 'source.zip'
            archive.write_bytes(data[:-1])
            with self.assertRaisesRegex(ValueError, 'size mismatch'):
                recovery.extract_verified(archive, self.source(data), root / 'verified')
            self.assertFalse((root / 'verified').exists())

    def test_rejects_unsafe_zip_entries_before_extracting(self):
        cases = [archive_bytes([('../escape', 'x')]),
                 archive_bytes([('nested/file.deb', 'x')]),
                 archive_bytes([('\\evil', 'x')]),
                 archive_bytes([('ok.deb', 'x'), ('ok.deb', 'duplicate')]),
                 archive_bytes([('dir/', 'x')]),
                 archive_bytes([('link', 'outside', 0o120777)]),
                 archive_bytes([('fifo', 'x', 0o010644)]),
                 archive_bytes([('empty', b'')])]
        for data in cases:
            with self.subTest(data=data[:12]), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = root / 'source.zip'
                archive.write_bytes(data)
                with self.assertRaises(ValueError):
                    recovery.extract_verified(archive, self.source(data), root / 'verified')
                self.assertFalse((root / 'verified').exists())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = archive_bytes([('ok.deb', 'x')])
            archive = root / 'source.zip'
            archive.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                recovery.extract_verified(archive, self.source(data + b'x') |
                                          {'artifact_size': len(data)}, root / 'verified')

    def test_rejects_excessive_members_without_creating_destination(self):
        data = archive_bytes([(f'asset-{index}', b'x') for index in range(257)])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.zip'
            archive.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'member count'):
                recovery.extract_verified(archive, self.source(data), root / 'verified')
            self.assertFalse((root / 'verified').exists())

    def test_corrupted_zip_member_cleans_extracted_directory(self):
        data = bytearray(archive_bytes([('good.deb', b'GOOD'), ('bad.deb', b'BAD-MEMBER')]))
        index = data.index(b'BAD-MEMBER')
        data[index] ^= 1
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.zip'
            archive.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'ZIP is corrupt'):
                recovery.extract_verified(archive, self.source(data), root / 'verified')
            self.assertFalse((root / 'verified').exists())


if __name__ == '__main__':
    unittest.main()
