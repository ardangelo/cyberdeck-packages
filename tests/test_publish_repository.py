"""Signed, stateless publication tests with real Debian packages and ephemeral GPG keys."""
import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from release_support import REPOSITORY, asset_url, canonical, expected_versions, paragraphs, sha256

spec = importlib.util.spec_from_file_location('publisher', ROOT / 'scripts/publish-repository.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


class FixtureAPI:
    """Only the GitHub transport is replaced; signed release and package bytes are real."""
    def __init__(self):
        self.items = []
        self.blobs = {}
        self.commits = {}

    def add(self, release, files, commit):
        self.items.append(release)
        self.blobs[release['tag_name']] = files
        self.commits[release['tag_name']] = commit

    def releases(self):
        return list(self.items)

    def assets(self, release):
        tag = release['tag_name']
        return {name: {'name': name, 'size': len(blob), 'id': index + 1, 'state': 'uploaded',
                       'browser_download_url': asset_url(tag, name)}
                for index, (name, blob) in enumerate(self.blobs[tag].items())}

    def metadata(self, release, entry, destination):
        Path(destination).write_bytes(self.blobs[release['tag_name']][entry['name']])
        return Path(destination)

    def asset(self, release, entry, destination, digest, size):
        data = self.blobs[release['tag_name']][entry['name']]
        if hashlib.sha256(data).hexdigest() != digest or len(data) != size:
            raise ValueError('download digest/size mismatch')
        Path(destination).write_bytes(data)
        return Path(destination)

    def json(self, method, path):
        tag = path.rsplit('/', 1)[-1]
        if path.startswith('/git/ref/tags/'):
            return {'ref': 'refs/tags/' + tag, 'object': {'type': 'commit', 'sha': self.commits[tag]}}
        raise AssertionError(path)


@unittest.skipUnless(shutil.which('gpg') and shutil.which('dpkg-deb'), 'GPG and dpkg-deb required')
class SignedRepository(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'gpg'
        self.home.mkdir(mode=0o700)
        self.addCleanup(subprocess.run, ['gpgconf', '--homedir', str(self.home), '--kill', 'gpg-agent'],
                        capture_output=True)
        run('gpg', '--batch', '--homedir', str(self.home), '--passphrase', '', '--quick-generate-key',
            'Publisher Fixture <fixture@example.invalid>', 'ed25519', 'cert', '1d')
        output = run('gpg', '--batch', '--homedir', str(self.home), '--with-colons', '--list-secret-keys', text=True).stdout
        self.primary = next(line.split(':')[9] for line in output.splitlines() if line.startswith('fpr:'))
        run('gpg', '--batch', '--homedir', str(self.home), '--passphrase', '', '--quick-add-key',
            self.primary, 'ed25519', 'sign', '1d')
        output = run('gpg', '--batch', '--homedir', str(self.home), '--with-colons', '--list-secret-keys', text=True).stdout
        self.signing = [line.split(':')[9] for line in output.splitlines() if line.startswith('fpr:')][-1]
        self.key = self.root / 'archive.asc'
        self.key.write_bytes(run('gpg', '--batch', '--homedir', str(self.home), '--armor', '--export', self.primary).stdout)
        private = run('gpg', '--batch', '--homedir', str(self.home), '--armor', '--export-secret-subkeys', self.primary).stdout
        self.config = {'schema_version': 1, 'repository': REPOSITORY, 'signing_fingerprint': self.primary}
        self.oldenv = {name: os.environ.get(name) for name in
                       ('APT_GPG_PRIVATE_KEY', 'APT_GPG_PASSPHRASE', 'APT_GPG_SIGNING_FINGERPRINT')}
        os.environ.update(APT_GPG_PRIVATE_KEY=base64.b64encode(private).decode(),
                          APT_GPG_PASSPHRASE='', APT_GPG_SIGNING_FINGERPRINT=self.signing)
        self.addCleanup(self.restore_env)
        self.api = FixtureAPI()
        self.withdrawal = self.root / 'withdrawn.json'
        self.withdrawal.write_text('[]')
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)

    def restore_env(self):
        for name, value in self.oldenv.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def sign(self, blob):
        data = self.root / 'manifest'
        signature = self.root / 'signature'
        data.write_bytes(blob)
        if signature.exists():
            signature.unlink()
        run('gpg', '--batch', '--homedir', str(self.home), '--local-user', self.signing + '!',
            '--armor', '--detach-sign', '--output', str(signature), str(data))
        return signature.read_bytes()

    def package(self, name, version, arch, depends):
        staging = self.root / 'stage'
        if staging.exists():
            shutil.rmtree(staging)
        control = staging / 'DEBIAN'
        control.mkdir(parents=True)
        (control / 'control').write_text(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
                                         f'Maintainer: Fixture <fixture@example.invalid>\n'
                                         f'Depends: {depends}\nDescription: signed publication fixture\n')
        (staging / 'usr/share/fixtures').mkdir(parents=True)
        (staging / 'usr/share/fixtures' / name).write_text(version)
        package = self.root / f'{name}_{version}_{arch}.deb'
        run('dpkg-deb', '--build', '--root-owner-group', str(staging), str(package))
        return package.read_bytes()

    def cohort(self, number, variant):
        tag = f'packages-v{number}'
        identity = {'schema_version': 1, 'conf': 'k230_canmv_01studio_defconfig',
                    'kernel_source_revision': '7d4e1f444f461dbe3833bd99a4640e7b6c2cd529',
                    'kernel_release': '6.6.0-same-release', 'kernel_config_sha256': 'a' * 64,
                    'module_symvers_sha256': variant * 64, 'kernel_inputs_sha256': 'c' * 64,
                    'compiler_identity': 'riscv64-unknown-linux-gnu-gcc test', 'compiler_sha256': 'd' * 64}
        abi = hashlib.sha256(canonical(identity)).hexdigest()
        context = dict(identity, abi_sha256=abi)
        suite = 'trixie-k230-' + abi
        versions = expected_versions(number, abi)
        deps = {'cyberdeck-service': 'kbd, openjdk-21-jre-headless, libc6 (>= 2.38), libstdc++6 (>= 14)',
                'k230-status-ready': '', 'k230-sharp-drm': f'kmod, k230-kernel-abi (= {versions["k230-sharp-drm"]})',
                'k230-kernel-abi': ''}
        artifacts, files, stanzas = [], {'kernel-context.json': canonical(context)}, []
        for name, version in versions.items():
            arch = 'all' if name == 'k230-status-ready' else 'riscv64'
            filename = f'{name}_{version}_{arch}.deb'
            payload = self.package(name, version, arch, deps[name])
            files[filename] = payload
            artifact = {'file': filename, 'sha256': hashlib.sha256(payload).hexdigest(),
                        'size': len(payload), 'kind': 'deb', 'url': asset_url(tag, filename),
                        'package': name, 'version': version, 'architecture': arch, 'depends': deps[name]}
            artifacts.append(artifact)
            stanzas.append(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
                           f'Depends: {deps[name]}\nFilename: pool/{suite}/{tag}/{filename}\n'
                           f'Size: {len(payload)}\nSHA256: {artifact["sha256"]}\n')
        golden = ('\n'.join(stanzas)).encode()
        files['snapshot-Packages'] = golden
        def add_source(filename, payload, kind='source'):
            files[filename] = payload
            artifacts.append({'file': filename, 'sha256': hashlib.sha256(payload).hexdigest(),
                              'size': len(payload), 'kind': kind, 'url': asset_url(tag, filename)})
            return filename

        def source_tar(filename, payload, *, xz=False):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                info = tarfile.TarInfo(filename)
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
            return lzma.compress(buffer.getvalue()) if xz else gzip.compress(buffer.getvalue(), mtime=0)

        def checksums(names):
            return ''.join(f' {hashlib.sha256(files[name]).hexdigest()} {len(files[name])} {name}\n'
                           for name in names)

        owners = {'cyberdeck-service': {'cyberdeck-service'}, 'k230-status-ready': {'k230-status-ready'},
                  'sharp-drm': {'k230-sharp-drm', 'k230-kernel-abi'}}
        for source, packages in owners.items():
            version = versions[next(iter(packages))]
            upstream = version.rsplit('-', 1)[0]
            orig = add_source(f'{source}_{upstream}.orig.tar.gz',
                              source_tar(f'{source}-{upstream}/fixture.c', b'int fixture;\n'))
            debian = add_source(f'{source}_{version}.debian.tar.xz',
                                source_tar('debian/control', f'Source: {source}\n'.encode(), xz=True))
            dsc = add_source(f'{source}_{version}.dsc',
                             (f'Format: 3.0 (quilt)\nSource: {source}\nVersion: {version}\nChecksums-Sha256:\n'
                              + checksums([orig, debian])).encode())
            binaries = [entry['file'] for entry in artifacts
                        if entry['kind'] == 'deb' and entry['package'] in packages]
            source_info = add_source(f'{source}_{version}_source.buildinfo',
                                     (f'Source: {source}\nVersion: {version}\nArchitecture: source\nChecksums-Sha256:\n'
                                      + checksums([dsc])).encode(), 'buildinfo')
            binary_info = add_source(f'{source}_{version}_riscv64.buildinfo',
                                     (f'Source: {source}\nVersion: {version}\nArchitecture: riscv64\nChecksums-Sha256:\n'
                                      + checksums(binaries)).encode(), 'buildinfo')
            add_source(f'{source}_{version}_source.changes',
                       (f'Source: {source}\nVersion: {version}\nArchitecture: source\nChecksums-Sha256:\n'
                        + checksums([dsc, orig, debian, source_info])).encode(), 'changes')
            add_source(f'{source}_{version}_riscv64.changes',
                       (f'Source: {source}\nVersion: {version}\nArchitecture: riscv64\nChecksums-Sha256:\n'
                        + checksums(binaries + [binary_info])).encode(), 'changes')
        corresponding = f'cyberdeck-service_{versions["cyberdeck-service"].rsplit("-", 1)[0]}.corresponding-sources.tar.gz'
        add_source(corresponding, source_tar('corresponding-source/fixture.c', b'int fixture_dependency;\n'))
        lock = json.loads((ROOT / 'source-lock.json').read_text())
        commit = format(number, '040x')
        manifest = {'schema_version': 1, 'release_tag': tag, 'release_number': number,
                    'repository': REPOSITORY, 'source_commit': commit, 'conf': context['conf'],
                    'suite': suite, 'snapshot_suite': f'{suite}-release-{number}',
                    'snapshot_index': {'file': 'snapshot-Packages', 'sha256': hashlib.sha256(golden).hexdigest(),
                                       'size': len(golden), 'url': asset_url(tag, 'snapshot-Packages')},
                    'abi_sha256': abi, 'source_date_epoch': 1761000000,
                    'kernel_context': {'file': 'kernel-context.json', 'sha256': hashlib.sha256(files['kernel-context.json']).hexdigest()},
                    'sources': {'lock': lock, 'submodules': [
                        {'path': path, 'repository': repo, 'revision': revision} for path, repo, revision in (
                            ('mobileapp', 'https://github.com/ardangelo/libpebble3.git', 'b' * 40),
                            ('src/nanopb', 'https://github.com/nanopb/nanopb.git', '4e73df5a72e470a8195c3efdaf1d0e45e22c3af7'))]},
                    'artifacts': artifacts}
        files[publisher.MANIFEST] = canonical(manifest)
        files[publisher.SIGNATURE] = self.sign(files[publisher.MANIFEST])
        self.api.add({'tag_name': tag, 'draft': False, 'prerelease': False, 'immutable': True}, files, commit)
        return manifest, files

    def publish(self, destination, *, now=None, dry_run=False):
        if dry_run:
            signer = None
        else:
            with publisher.signing_home(self.key, self.primary) as sign:
                return publisher.assemble(self.api, destination, self.key, self.config,
                                          self.withdrawal, signer=sign, now=now or self.now, history=False)
        return publisher.assemble(self.api, destination, self.key, self.config,
                                  self.withdrawal, dry_run=True, now=now or self.now, history=False)

    def test_signed_snapshots_live_advancement_renewal_stateless_and_withdrawal(self):
        first, _ = self.cohort(1, 'b')
        site1 = self.root / 'site1'
        self.publish(site1)
        snapshot = site1 / 'dists' / first['snapshot_suite'] / 'main/binary-riscv64/Packages'
        old_bytes = snapshot.read_bytes()
        self.assertEqual(gzip.decompress((snapshot.parent / 'Packages.gz').read_bytes()), old_bytes)
        self.assertEqual((snapshot.parent / 'Packages.gz').read_bytes()[4:8], b'\x00' * 4)
        self.assertEqual(list(site1.rglob('*.dsc')), [])
        self.assertIn('.corresponding-sources.tar.gz', (site1 / 'index.html').read_text())
        release = paragraphs((snapshot.parents[2] / 'Release').read_bytes())[0]
        self.assertEqual(release['Suite'], first['snapshot_suite'])
        self.assertEqual(release['Origin'], 'Cyberdeck')
        self.assertIn('main/binary-riscv64/Packages', release['SHA256'])
        self.assertIn('main/binary-riscv64/Packages.gz', release['SHA512'])
        second, _ = self.cohort(2, 'b')
        different, _ = self.cohort(3, 'e')
        self.assertEqual(first['suite'], second['suite'])
        self.assertNotEqual(first['suite'], different['suite'])
        site2 = self.root / 'site2'
        self.publish(site2)
        self.assertEqual(old_bytes, (site2 / snapshot.relative_to(site1)).read_bytes())
        live = (site2 / 'dists' / first['suite'] / 'main/binary-riscv64/Packages').read_bytes()
        self.assertEqual([field['Version'] for field in paragraphs(live) if field['Package'] == 'cyberdeck-service'],
                         [expected_versions(number, first['abi_sha256'])['cyberdeck-service']
                          for number in (1, 2)])
        other = (site2 / 'dists' / different['suite'] / 'main/binary-riscv64/Packages').read_bytes()
        self.assertNotIn(second['abi_sha256'].encode(), other)
        self.assertEqual({field['Filename'].split('/')[1] for field in paragraphs(other)}, {different['suite']})
        site3 = self.root / 'site3'
        self.publish(site3, now=self.now + timedelta(days=2))
        self.assertEqual(old_bytes, (site3 / snapshot.relative_to(site1)).read_bytes())
        self.assertNotEqual((site2 / 'dists' / first['suite'] / 'Release').read_bytes(),
                            (site3 / 'dists' / first['suite'] / 'Release').read_bytes())
        self.withdrawal.write_text(json.dumps([{'tag': 'packages-v1', 'reason': '<retired>&'}]))
        site4 = self.root / 'site4'
        self.publish(site4)
        self.assertFalse((site4 / snapshot.relative_to(site1)).exists())
        self.assertTrue((site4 / 'pool' / first['suite'] / 'packages-v1').is_dir())
        self.assertEqual(4, len(paragraphs((site4 / 'dists' / first['suite'] / 'main/binary-riscv64/Packages').read_bytes())))
        self.assertIn('&lt;retired&gt;&amp;', (site4 / 'index.html').read_text())
        site5 = self.root / 'site5'
        self.publish(site5)
        self.assertEqual((site4 / 'dists' / first['suite'] / 'main/binary-riscv64/Packages').read_bytes(),
                         (site5 / 'dists' / first['suite'] / 'main/binary-riscv64/Packages').read_bytes())

    def test_corrupt_index_extra_asset_conflict_and_oversized_site_fail_closed(self):
        manifest, files = self.cohort(1, 'b')
        tag = manifest['release_tag']
        output = self.root / 'deployment'
        golden = files['snapshot-Packages']
        files['snapshot-Packages'] = golden + b'\nPackage: fifth\nVersion: 1\n\n'
        changed = dict(manifest, snapshot_index=dict(manifest['snapshot_index']))
        changed['snapshot_index']['sha256'] = hashlib.sha256(files['snapshot-Packages']).hexdigest()
        changed['snapshot_index']['size'] = len(files['snapshot-Packages'])
        files[publisher.MANIFEST] = canonical(changed)
        files[publisher.SIGNATURE] = self.sign(files[publisher.MANIFEST])
        with self.assertRaisesRegex(ValueError, 'unexpected or duplicate indexed binary'):
            self.publish(output, dry_run=True)
        self.assertFalse(output.exists())
        files['snapshot-Packages'] = golden
        files[publisher.MANIFEST] = canonical(manifest)
        files[publisher.SIGNATURE] = self.sign(files[publisher.MANIFEST])
        files['undeclared.deb'] = b'extra'
        with self.assertRaisesRegex(ValueError, 'undeclared release assets'):
            self.publish(output, dry_run=True)
        files.pop('undeclared.deb')
        previous = publisher.LIMIT
        try:
            publisher.LIMIT = 1
            with self.assertRaisesRegex(ValueError, 'exceeds 900 MiB'):
                self.publish(output, dry_run=True)
        finally:
            publisher.LIMIT = previous
        self.assertFalse(output.exists())
        output.mkdir()
        (output / 'existing-deployment').write_bytes(b'preserve this deployment')
        with self.assertRaisesRegex(ValueError, 'output must be absent'):
            self.publish(output, dry_run=True)
        self.assertEqual((output / 'existing-deployment').read_bytes(), b'preserve this deployment')

    def test_eligible_releases_and_package_bytes_fail_closed(self):
        manifest, files = self.cohort(1, 'b')
        filename = next(entry['file'] for entry in manifest['artifacts'] if entry['kind'] == 'deb')
        original = files[filename]
        files[filename] = bytes([original[0] ^ 1]) + original[1:]
        with self.assertRaisesRegex(ValueError, 'digest/size'):
            self.publish(self.root / 'corrupt-deb', dry_run=True)
        files[filename] = original
        release = self.api.items[0]
        release['immutable'] = False
        with self.assertRaisesRegex(ValueError, 'not immutable'):
            self.publish(self.root / 'mutable', dry_run=True)
        release['immutable'] = True
        self.api.items.append(dict(release))
        with self.assertRaisesRegex(ValueError, 'duplicate release number'):
            self.publish(self.root / 'duplicate', dry_run=True)
        self.api.items.pop()
        self.api.items.append(dict(release, tag_name='packages-v01'))
        with self.assertRaisesRegex(ValueError, 'invalid published package release'):
            self.publish(self.root / 'malformed', dry_run=True)
        self.api.items[-1]['draft'] = True
        self.api.items.append(dict(release, tag_name='unrelated'))
        self.publish(self.root / 'ignored', dry_run=True)
        self.assertTrue((self.root / 'ignored' / 'DRY-RUN-NOT-DEPLOYABLE').is_file())
        self.assertFalse((self.root / 'ignored' / 'dists' / manifest['suite'] / 'InRelease').exists())

    def test_signed_release_missing_source_or_build_artifacts_is_rejected(self):
        manifest, files = self.cohort(1, 'b')
        complete = dict(files)
        for number, suffix in enumerate(('.orig.tar.gz', '.buildinfo', '.changes')):
            with self.subTest(missing=suffix):
                files.clear()
                files.update(complete)
                missing = next(entry['file'] for entry in manifest['artifacts']
                               if entry['file'].startswith('cyberdeck-service_')
                               and entry['file'].endswith(suffix))
                modified = dict(manifest, artifacts=[entry for entry in manifest['artifacts']
                                                    if entry['file'] != missing])
                del files[missing]
                files[publisher.MANIFEST] = canonical(modified)
                files[publisher.SIGNATURE] = self.sign(files[publisher.MANIFEST])
                output = self.root / f'incomplete-source-{number}'
                with self.assertRaises(ValueError):
                    self.publish(output, dry_run=True)
                self.assertFalse(output.exists())

    def test_withdrawal_history_is_append_only(self):
        self.cohort(1, 'b')
        self.cohort(2, 'b')
        repo = self.root / 'history'
        repo.mkdir()
        run('git', '-C', str(repo), 'init', '-q')
        run('git', '-C', str(repo), 'config', 'user.email', 'fixture@example.invalid')
        run('git', '-C', str(repo), 'config', 'user.name', 'Fixture')
        register = repo / 'withdrawn-releases.json'
        first = {'tag': 'packages-v1', 'reason': 'ABI erratum'}
        second = {'tag': 'packages-v2', 'reason': 'different erratum'}
        register.write_text(json.dumps([first]))
        run('git', '-C', str(repo), 'add', 'withdrawn-releases.json')
        run('git', '-C', str(repo), 'commit', '-qm', 'withdraw first')
        register.write_text(json.dumps([first, second]))
        self.assertEqual(publisher.withdrawals(register, publisher.eligible(self.api)),
                         {'packages-v1': first['reason'], 'packages-v2': second['reason']})
        register.write_text(json.dumps([second]))
        with self.assertRaisesRegex(ValueError, 'append|extend committed history'):
            publisher.withdrawals(register, publisher.eligible(self.api))
        register.write_text('[]')
        run('git', '-C', str(repo), 'add', 'withdrawn-releases.json')
        run('git', '-C', str(repo), 'commit', '-qm', 'remove withdrawal')
        shallow = self.root / 'shallow'
        run('git', 'clone', '--depth=1', repo.as_uri(), str(shallow))
        with self.assertRaises(ValueError):
            publisher.withdrawals(shallow / 'withdrawn-releases.json', publisher.eligible(self.api))

    def test_rejects_bad_tag_ref_signature_and_withdrawal(self):
        manifest, files = self.cohort(1, 'b')
        self.api.commits['packages-v1'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'tag/commit'):
            self.publish(self.root / 'bad-commit', dry_run=True)
        self.api.commits['packages-v1'] = manifest['source_commit']
        files[publisher.SIGNATURE] = b'wrong signature'
        with self.assertRaises(ValueError):
            self.publish(self.root / 'bad-signature', dry_run=True)
        files[publisher.SIGNATURE] = self.sign(files[publisher.MANIFEST])
        run('gpg', '--batch', '--homedir', str(self.home), '--passphrase', '', '--quick-generate-key',
            'Wrong Fixture <wrong@example.invalid>', 'ed25519', 'cert', '1d')
        listed = run('gpg', '--batch', '--homedir', str(self.home), '--with-colons', '--list-keys', text=True).stdout
        wrong_primary = [line.split(':')[9] for line in listed.splitlines() if line.startswith('fpr:')][-1]
        wrong_key = self.root / 'wrong.asc'
        wrong_key.write_bytes(run('gpg', '--batch', '--homedir', str(self.home), '--armor', '--export', wrong_primary).stdout)
        with self.assertRaisesRegex(ValueError, 'no valid signature'):
            publisher.assemble(self.api, self.root / 'wrong-key', wrong_key, self.config,
                               self.withdrawal, dry_run=True, now=self.now, history=False)
        self.withdrawal.write_text(json.dumps([{'tag': 'packages-v99', 'reason': 'unknown'}]))
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.publish(self.root / 'bad-withdrawal', dry_run=True)


if __name__ == '__main__':
    unittest.main()
