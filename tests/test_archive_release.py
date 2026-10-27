"""Exercise real .deb indexing and signatures without contacting GitHub."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('archive_release', ROOT / 'scripts/archive-release.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)
from release_support import asset_url, canonical, expected_versions, sha256, validate_index

HEAD = 'a' * 40
CONF = 'k230_canmv_01studio_defconfig'


class MemoryGitHub:
    def __init__(self):
        self.records = {}
        self.tags = {}
        self.contents = {}
        self.uploaded = []
        self.next_id = 1

    def releases(self):
        return list(self.records.values())

    def release(self, tag):
        record = self.records.get(tag)
        return record if record is not None and not record['draft'] else None

    def assets(self, release):
        return {name: {'id': i, 'name': name, 'size': len(payload),
                'browser_download_url': asset_url(release['tag_name'], name)}
                for i, (name, payload) in enumerate(self.contents.get(release['tag_name'], {}).items(), 1)}

    def json(self, method, path, data=None, *, missing_ok=False):
        if path.startswith('/git/ref/tags/'):
            commit = self.tags.get(path.rsplit('/', 1)[1])
            return {'object': {'type': 'commit', 'sha': commit}} if commit else None
        if path == '/git/ref/heads/main':
            return {'object': {'type': 'commit', 'sha': HEAD}}
        if method == 'POST' and path == '/releases':
            tag = data['tag_name']
            assert tag not in self.records
            record = {'id': self.next_id, 'tag_name': tag,
                      'target_commitish': data['target_commitish'], 'draft': True,
                      'prerelease': False, 'immutable': False}
            self.next_id += 1
            self.records[tag] = record
            self.contents[tag] = {}
            return record
        if method == 'PATCH':
            record = next(r for r in self.records.values() if path == f'/releases/{r["id"]}')
            self.tags.setdefault(record['tag_name'], record['target_commitish'])
            record.update({'draft': False, 'immutable': True})
            return record
        raise AssertionError((method, path))

    def upload(self, release, path):
        name = path.name
        assert release['draft'] and name not in self.contents[release['tag_name']]
        payload = path.read_bytes()
        self.contents[release['tag_name']][name] = payload
        self.uploaded.append(name)
        return {'name': name, 'size': len(payload)}

    def asset(self, release, entry, destination, digest, size):
        payload = self.contents[release['tag_name']][entry['name']]
        assert len(payload) == size and hashlib.sha256(payload).hexdigest() == digest
        Path(destination).write_bytes(payload)
        return destination

    def metadata(self, release, entry, destination):
        Path(destination).write_bytes(self.contents[release['tag_name']][entry['name']])
        return destination


def make_deb(directory, name, version, arch, dependencies):
    root = directory / ('build-' + name)
    control = root / 'DEBIAN'
    control.mkdir(parents=True, exist_ok=True)
    (control / 'control').write_text(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
                                   f'Maintainer: Test <test@example.org>\nDescription: Fixture package\n'
                                   + (f'Depends: {dependencies}\n' if dependencies else ''))
    (root / 'usr/share/doc' / name).mkdir(parents=True, exist_ok=True)
    (root / 'usr/share/doc' / name / 'copyright').write_text('Test fixture\n')
    dest = directory / f'{name}_{version}_{arch}.deb'
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(root), str(dest)], check=True, capture_output=True)
    return dest


class ArchiveBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if any(shutil.which(tool) is None for tool in ('gpg', 'gpgv', 'dpkg-deb', 'apt-ftparchive')):
            raise unittest.SkipTest('GPG and apt-ftparchive required (run tests on packaging pod)')
        cls.keys = tempfile.TemporaryDirectory(prefix='archive-test-keys-')
        home = Path(cls.keys.name)
        home.chmod(0o700)
        cls.passphrase = 'fixture-passphrase'
        base = ['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', cls.passphrase,
                '--homedir', str(home)]
        subprocess.run(base + ['--quick-gen-key', 'Archive Fixture <fixture@example.org>', 'ed25519', 'cert', '1d'],
                       check=True, capture_output=True)
        keylist = subprocess.check_output(base + ['--with-colons', '--list-secret-keys'], text=True)
        cls.primary = [row.split(':')[9] for row in keylist.splitlines() if row.startswith('fpr:')][0]
        subprocess.run(base + ['--quick-add-key', cls.primary, 'ed25519', 'sign', '1d'],
                       check=True, capture_output=True)
        listed = subprocess.check_output(base + ['--with-colons', '--list-secret-keys'], text=True)
        cls.subkey = [row.split(':')[9] for row in listed.splitlines() if row.startswith('fpr:')][-1]
        cls.public = home / 'public.asc'
        cls.public.write_bytes(subprocess.check_output(base + ['--armor', '--export', cls.primary]))
        cls.private = base64.b64encode(subprocess.check_output(base + ['--armor', '--export-secret-subkeys', cls.primary])).decode()
        cls.config = {'schema_version': 1, 'repository': 'ardangelo/cyberdeck-packages',
                      'signing_fingerprint': cls.primary}

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'keys'):
            subprocess.run(['gpgconf', '--homedir', cls.keys.name, '--kill', 'gpg-agent'], capture_output=True)
            cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = self.root / 'verified'
        self.transport.mkdir()
        self.api = MemoryGitHub()
        self.env = patch.dict(os.environ, {'APT_GPG_PRIVATE_KEY': self.private,
                'APT_GPG_PASSPHRASE': self.passphrase, 'APT_GPG_SIGNING_FINGERPRINT': self.subkey})
        self.env.start()
        self.addCleanup(self.env.stop)
        identity = {'schema_version': 1, 'conf': CONF, 'kernel_source_revision': 'b' * 40,
                    'kernel_release': '6.6.0-fixture', 'kernel_config_sha256': '1' * 64,
                    'module_symvers_sha256': '2' * 64, 'kernel_inputs_sha256': '3' * 64,
                    'compiler_identity': 'test compiler', 'compiler_sha256': '4' * 64}
        self.context = identity | {'abi_sha256': hashlib.sha256(canonical(identity)).hexdigest()}
        (self.transport / 'kernel-context.json').write_bytes(canonical(self.context) + b'\n')
        self.write_transport(1)

    def write_transport(self, number, *, source_number=None):
        for path in self.transport.iterdir():
            if path.name != 'kernel-context.json':
                path.unlink()
        versions = expected_versions(number, self.context['abi_sha256'])
        source_versions = expected_versions(number if source_number is None else source_number,
                                            self.context['abi_sha256'])
        entries = []
        for name, arch in [('cyberdeck-service', 'riscv64'), ('k230-status-ready', 'all'),
                           ('k230-sharp-drm', 'riscv64'), ('k230-kernel-abi', 'riscv64')]:
            dependencies = ('libc6 (>= 2.38), libstdc++6 (>= 14), kbd, openjdk-21-jre-headless'
                            if name == 'cyberdeck-service' else
                            f'k230-kernel-abi (= {versions[name]}), kmod' if name == 'k230-sharp-drm' else '')
            deb = make_deb(self.root, name, versions[name], arch, dependencies)
            shutil.copyfile(deb, self.transport / deb.name)
            entries.append(self.entry(self.transport / deb.name, 'deb',
                           package=name, version=versions[name], architecture=arch, depends=dependencies))
        for source in sorted(archive.SOURCES):
            version = source_versions['cyberdeck-service' if source == 'cyberdeck-service' else
                               'k230-status-ready' if source == 'k230-status-ready' else 'k230-sharp-drm']
            orig = self.transport / f'{source}_{version.rsplit("-", 1)[0]}.orig.tar.gz'
            debian = self.transport / f'{source}_{version}.debian.tar.xz'
            orig.write_bytes((source + ' original source').encode())
            debian.write_bytes((source + ' Debian metadata').encode())
            entries.extend((self.entry(orig, 'source'), self.entry(debian, 'source')))
            dsc = self.transport / f'{source}_{version}.dsc'
            dsc.write_text(f'Format: 3.0 (quilt)\nSource: {source}\nVersion: {version}\nChecksums-Sha256:\n'
                           + ''.join(f' {sha256(p)} {p.stat().st_size} {p.name}\n' for p in (orig, debian)))
            entries.append(self.entry(dsc, 'source'))
            binary_names = {'cyberdeck-service': {'cyberdeck-service'},
                            'k230-status-ready': {'k230-status-ready'},
                            'sharp-drm': {'k230-sharp-drm', 'k230-kernel-abi'}}[source]
            binaries = [self.transport / e['file'] for e in entries
                        if e['kind'] == 'deb' and e['package'] in binary_names]
            source_info = self.transport / f'{source}_{version}_source.buildinfo'
            binary_info = self.transport / f'{source}_{version}_amd64.buildinfo'
            for path, arch, linked in ((source_info, 'source', [dsc]),
                                       (binary_info, 'riscv64', binaries)):
                path.write_text(f'Source: {source}\nVersion: {version}\nArchitecture: {arch}\nChecksums-Sha256:\n'
                                + ''.join(f' {sha256(item)} {item.stat().st_size} {item.name}\n' for item in linked))
                entries.append(self.entry(path, 'buildinfo'))
            for path, arch, linked in ((self.transport / f'{source}_{version}_source.changes',
                                         'source', [dsc, orig, debian, source_info]),
                                        (self.transport / f'{source}_{version}_amd64.changes',
                                         'riscv64', binaries + [binary_info])):
                path.write_text(f'Source: {source}\nVersion: {version}\nArchitecture: {arch}\nChecksums-Sha256:\n'
                                + ''.join(f' {sha256(item)} {item.stat().st_size} {item.name}\n' for item in linked))
                entries.append(self.entry(path, 'changes'))
        corresponding = self.transport / f'cyberdeck-service_{source_versions["cyberdeck-service"].rsplit("-", 1)[0]}.corresponding-sources.tar.gz'
        corresponding.write_bytes(b'fixture complete corresponding source')
        entries.append(self.entry(corresponding, 'source'))
        record = {'schema_version': 1, 'release_number': number, 'source_date_epoch': 1730000000,
                  'sources': {'lock': json.loads((ROOT / 'source-lock.json').read_text()),
                              'submodules': [{'path': 'mobileapp', 'repository': 'https://github.com/ardangelo/libpebble3.git', 'revision': 'c' * 40},
                                             {'path': 'src/nanopb', 'repository': 'https://github.com/nanopb/nanopb.git', 'revision': 'd' * 40}]},
                  'kernel_context': self.context, 'artifacts': entries}
        (self.transport / 'verified-input.json').write_bytes(canonical(record) + b'\n')

    @staticmethod
    def entry(path, kind, **fields):
        return {'file': path.name, 'sha256': sha256(path), 'size': path.stat().st_size, 'kind': kind} | fields

    def perform(self, number=1):
        return archive.archive(self.api, self.transport, number, self.config, self.public, head=HEAD)

    def test_signed_golden_index_and_idempotent_immutable_release(self):
        self.assertEqual(self.perform(), 'packages-v1')
        stored = self.api.contents['packages-v1']
        index = stored['snapshot-Packages']
        manifest = json.loads(stored['release-manifest.json'])
        validate_index(index, manifest['artifacts'], manifest['suite'], manifest['release_tag'])
        self.assertEqual(hashlib.sha256(index).hexdigest(), manifest['snapshot_index']['sha256'])
        self.assertEqual(len([a for a in manifest['artifacts'] if a['kind'] == 'deb']), 4)
        before = copy.deepcopy(stored)
        uploads = len(self.api.uploaded)
        self.assertEqual(self.perform(), 'packages-v1')
        self.assertEqual(self.api.contents['packages-v1'], before)
        self.assertEqual(len(self.api.uploaded), uploads)

    def test_reject_changed_bytes_without_overwriting_published(self):
        self.perform()
        original = copy.deepcopy(self.api.contents['packages-v1'])
        path = next(self.transport.glob('*.corresponding-sources.tar.gz'))
        path.write_bytes(b'changed corresponding source')
        record_path = self.transport / 'verified-input.json'
        record = json.loads(record_path.read_text())
        modified = next(a for a in record['artifacts'] if a['file'] == path.name)
        modified['sha256'] = sha256(path)
        modified['size'] = path.stat().st_size
        record_path.write_bytes(canonical(record) + b'\n')
        with self.assertRaisesRegex(ValueError, 'conflict|mismatch'):
            self.perform()
        self.assertEqual(self.api.contents['packages-v1'], original)

    def test_partial_draft_resumes_existing_signature_and_never_overwrites_conflicts(self):
        self.perform()
        original = copy.deepcopy(self.api.contents['packages-v1'])
        self.api.records['packages-v1'].update({'draft': True, 'immutable': False})
        signature = original['release-manifest.json.asc']
        del self.api.contents['packages-v1']['snapshot-Packages']
        uploads = len(self.api.uploaded)
        self.assertEqual(self.perform(), 'packages-v1')
        self.assertEqual(self.api.contents['packages-v1']['release-manifest.json.asc'], signature)
        self.assertEqual(self.api.uploaded[uploads:], ['snapshot-Packages'])
        self.api.records['packages-v1'].update({'draft': True, 'immutable': False})
        self.api.contents['packages-v1']['snapshot-Packages'] = b'conflict'
        before = copy.deepcopy(self.api.contents['packages-v1'])
        with self.assertRaises((ValueError, AssertionError)):
            self.perform()
        self.assertEqual(self.api.contents['packages-v1'], before)

    def test_unpublished_draft_without_tag_resumes_after_interruption(self):
        upload = self.api.upload

        def interrupted_upload(release, path):
            upload(release, path)
            raise RuntimeError('interrupted asset upload')

        with patch.object(self.api, 'upload', side_effect=interrupted_upload):
            with self.assertRaisesRegex(RuntimeError, 'interrupted asset upload'):
                self.perform()
        self.assertTrue(self.api.records['packages-v1']['draft'])
        self.assertNotIn('packages-v1', self.api.tags)
        retained = copy.deepcopy(self.api.contents['packages-v1'])
        self.assertEqual(self.perform(), 'packages-v1')
        for name, payload in retained.items():
            self.assertEqual(self.api.contents['packages-v1'][name], payload)
            self.assertEqual(self.api.uploaded.count(name), 1)
        self.assertFalse(self.api.records['packages-v1']['draft'])
        self.assertTrue(self.api.records['packages-v1']['immutable'])
        self.assertEqual(self.api.tags['packages-v1'], HEAD)

    def test_tag_conflict_during_upload_prevents_publication(self):
        upload = self.api.upload

        def conflicting_upload(release, path):
            result = upload(release, path)
            self.api.tags[release['tag_name']] = 'e' * 40
            return result

        with patch.object(self.api, 'upload', side_effect=conflicting_upload):
            with self.assertRaisesRegex(ValueError, 'Git tag commit'):
                self.perform()
        self.assertTrue(self.api.records['packages-v1']['draft'])
        self.assertFalse(self.api.records['packages-v1']['immutable'])
        self.assertEqual(self.api.tags['packages-v1'], 'e' * 40)

    def test_published_release_requires_matching_tag(self):
        self.perform()
        before = copy.deepcopy(self.api.contents['packages-v1'])
        del self.api.tags['packages-v1']
        with self.assertRaisesRegex(ValueError, 'published release lacks matching Git tag'):
            self.perform()
        self.assertEqual(self.api.contents['packages-v1'], before)

    def test_new_numbers_are_monotonic_and_idempotency_survives_later_release(self):
        self.perform()
        self.api.records['packages-v3'] = {'id': 3, 'tag_name': 'packages-v3', 'target_commitish': HEAD,
                                          'draft': False, 'prerelease': False, 'immutable': True}
        self.assertEqual(self.perform(), 'packages-v1')
        self.write_transport(2)
        with self.assertRaisesRegex(ValueError, 'exceed'):
            self.perform(2)
        self.api.records.pop('packages-v3')
        self.write_transport(1)
        self.api.tags['packages-v1'] = 'e' * 40
        with self.assertRaisesRegex(ValueError, 'Git tag commit'):
            self.perform()

    def test_resumed_draft_rejects_extraneous_or_invalid_signature(self):
        self.perform()
        self.api.records['packages-v1'].update({'draft': True, 'immutable': False})
        stored = self.api.contents['packages-v1']
        stored['unverified.deb'] = b'extra'
        before = len(self.api.uploaded)
        with self.assertRaisesRegex(ValueError, 'unexpected assets'):
            self.perform()
        self.assertEqual(len(self.api.uploaded), before)
        del stored['unverified.deb']
        signature = stored['release-manifest.json.asc']
        stored['release-manifest.json.asc'] = b'x' * len(signature)
        with self.assertRaises(ValueError):
            self.perform()
        self.assertEqual(len(self.api.uploaded), before)

    def test_missing_source_linkage_fails_before_draft(self):
        dsc = next(self.transport.glob('*.dsc'))
        dsc.write_text(dsc.read_text().replace('Checksums-Sha256:', 'Broken:'))
        record_path = self.transport / 'verified-input.json'
        record = json.loads(record_path.read_text())
        next(a for a in record['artifacts'] if a['file'] == dsc.name)['sha256'] = sha256(dsc)
        next(a for a in record['artifacts'] if a['file'] == dsc.name)['size'] = dsc.stat().st_size
        record_path.write_bytes(canonical(record) + b'\n')
        with self.assertRaises(ValueError):
            self.perform()
        self.assertEqual(self.api.records, {})

    def test_different_version_source_chain_fails_before_draft(self):
        self.write_transport(1, source_number=2)
        with self.assertRaisesRegex(ValueError, 'source/binary version mismatch'):
            self.perform()
        self.assertEqual(self.api.records, {})

    def test_undeclared_fifth_binary_fails_before_draft(self):
        binary = make_deb(self.root, 'unapproved-package', '1.0-1', 'riscv64', '')
        copy = self.transport / binary.name
        shutil.copyfile(binary, copy)
        record_path = self.transport / 'verified-input.json'
        record = json.loads(record_path.read_text())
        record['artifacts'].append(self.entry(copy, 'deb', package='unapproved-package',
                                              version='1.0-1', architecture='riscv64', depends=''))
        record_path.write_bytes(canonical(record) + b'\n')
        with self.assertRaisesRegex(ValueError, 'exactly four binaries'):
            self.perform()
        self.assertFalse(self.api.records)


class ArchiveSourcePolicy(unittest.TestCase):
    def test_release_number_must_be_positive_unpadded_decimal(self):
        for value in ('0', '-1', '01', '1;echo x', '1.0', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive.release_number(value)

    def test_checkout_must_be_protected_current_main(self):
        with patch.object(archive.subprocess, 'check_output', return_value=HEAD + '\n'):
            with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/feature', 'GITHUB_SHA': HEAD}):
                with self.assertRaisesRegex(ValueError, 'main workflow checkout'):
                    archive.git_source(MemoryGitHub())
            with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': 'f' * 40}):
                with self.assertRaisesRegex(ValueError, 'main workflow checkout'):
                    archive.git_source(MemoryGitHub())
            with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': HEAD}):
                remote = MemoryGitHub()
                with patch.object(remote, 'json', return_value={'object': {'type': 'commit', 'sha': 'b' * 40}}):
                    with self.assertRaisesRegex(ValueError, 'protected main advanced'):
                        archive.git_source(remote)



if __name__ == '__main__':
    unittest.main()
