"""Shared byte, cohort and signature checks for immutable package releases."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

REPOSITORY = 'ardangelo/cyberdeck-packages'
PACKAGE_ARCHES = {'cyberdeck-service': 'riscv64', 'k230-status-ready': 'all',
                  'k230-sharp-drm': 'riscv64', 'k230-kernel-abi': 'riscv64'}
HEX64 = re.compile(r'[0-9a-f]{64}')
HEX40 = re.compile(r'[0-9a-f]{40}')
CONTEXT_FIELDS = {'schema_version', 'conf', 'kernel_source_revision', 'kernel_release',
                  'kernel_config_sha256', 'module_symvers_sha256', 'kernel_inputs_sha256',
                  'compiler_identity', 'compiler_sha256', 'abi_sha256'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'duplicate JSON field: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def release_number(value, *, development=False):
    require(isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value), 'invalid release number')
    number = int(value)
    require(number > 0 or development, 'publication requires a positive release number')
    return number


def basename(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+~-]*', value), 'unsafe asset basename')
    require(value not in {'.', '..'}, 'unsafe asset basename')
    return value


def asset_url(tag, filename, repository=REPOSITORY):
    return f'https://github.com/{repository}/releases/download/{tag}/{quote(basename(filename), safe="")}'


def check_asset_url(value, tag, filename, repository=REPOSITORY):
    url = urlsplit(value)
    require(url.scheme == 'https' and url.netloc == 'github.com' and not url.query and not url.fragment,
            'asset URL must be immutable GitHub HTTPS')
    require(unquote(url.path) == f'/{repository}/releases/download/{tag}/{basename(filename)}',
            'asset URL repository, tag or basename mismatch')


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        require(urlsplit(newurl).scheme == 'https', 'download redirected outside HTTPS')
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None and urlsplit(request.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


def download(url, destination, expected_sha256, expected_size=None, *, headers=None):
    from urllib.request import Request
    require(urlsplit(url).scheme == 'https', 'downloads require HTTPS')
    require(isinstance(expected_sha256, str) and HEX64.fullmatch(expected_sha256), 'invalid download hash')
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(), 'refusing to replace downloaded asset')
    temporary = destination.with_name(destination.name + '.partial')
    digest = hashlib.sha256()
    size = 0
    created = False
    try:
        with build_opener(HTTPSRedirects()).open(Request(url, headers=headers or {}), timeout=120) as source:
            with temporary.open('xb') as target:
                created = True
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(chunk)
                    require(expected_size is None or size <= expected_size, 'download exceeds declared size')
                    digest.update(chunk)
                    target.write(chunk)
        require(digest.hexdigest() == expected_sha256, 'download SHA-256 mismatch')
        require(expected_size is None or size == expected_size, 'download size mismatch')
        destination.hardlink_to(temporary)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return destination


def repository_config(path):
    config = load_json(path)
    require(isinstance(config, dict) and set(config) in (
        {'schema_version', 'repository', 'signing_fingerprint'},
        {'schema_version', 'repository', 'signing_fingerprint', 'historical_keys'}),
        'unexpected repository configuration fields')
    require(type(config['schema_version']) is int and config['schema_version'] == 1, 'unsupported repository configuration')
    require(config['repository'] == REPOSITORY, 'unexpected publication repository')
    require(isinstance(config['signing_fingerprint'], str) and
            re.fullmatch(r'[0-9A-F]{40}', config['signing_fingerprint']), 'invalid current primary fingerprint')
    historical = config.get('historical_keys', [])
    require(isinstance(historical, list), 'invalid historical key policy')
    seen = {config['signing_fingerprint']}
    for key in historical:
        require(isinstance(key, dict) and set(key) == {'fingerprint', 'max_release_number'}, 'invalid historical key entry')
        fingerprint = key['fingerprint']
        require(isinstance(fingerprint, str) and re.fullmatch(r'[0-9A-F]{40}', fingerprint)
                and fingerprint not in seen, 'invalid or duplicated historical fingerprint')
        require(type(key['max_release_number']) is int and key['max_release_number'] > 0, 'historical trust needs an explicit release ceiling')
        seen.add(fingerprint)
    return config


def verify_manifest_signature(data, signature, public_key, config):
    manifest = validate_manifest(load_json(data), repository=config['repository'])
    candidates = [config['signing_fingerprint']]
    candidates.extend(key['fingerprint'] for key in config.get('historical_keys', [])
                      if manifest['release_number'] <= key['max_release_number'])
    for fingerprint in candidates:
        try:
            verify_signature(data, signature, public_key, fingerprint)
            return manifest
        except ValueError:
            pass
    raise ValueError('manifest has no valid signature under the configured release-bounded trust policy')


def paragraphs(data):
    result = []
    for block in data.decode('utf-8').strip().split('\n\n'):
        require(block.strip(), 'empty Debian control paragraph')
        fields = {}
        current = None
        for line in block.splitlines():
            if line[:1] in (' ', '\t'):
                require(current is not None, 'orphan Debian control continuation')
                fields[current] += ' ' + line.strip()
            else:
                require(':' in line, 'invalid Debian control field')
                name, value = line.split(':', 1)
                require(name not in fields, f'duplicate Debian field: {name}')
                fields[name] = value.strip()
                current = name
        result.append(fields)
    return result


def inspect_deb(path):
    fields = paragraphs(subprocess.check_output(['dpkg-deb', '--field', str(path)]))
    require(len(fields) == 1, 'binary control must have exactly one paragraph')
    fields = fields[0]
    return {'package': fields['Package'], 'version': fields['Version'],
            'architecture': fields['Architecture'], 'depends': fields.get('Depends', '')}


def expected_versions(number, abi):
    require(type(number) is int and number >= 0, 'invalid integer release number')
    require(isinstance(abi, str) and HEX64.fullmatch(abi), 'invalid ABI hash')
    module = f'1.7+cyberdeck.{number}+abi.{abi}-1'
    return {'cyberdeck-service': f'0.1+cyberdeck.{number}-1',
            'k230-status-ready': f'1.0+cyberdeck.{number}-1',
            'k230-sharp-drm': module, 'k230-kernel-abi': module}


def validate_cohort(artifacts, number, abi):
    binaries = [entry for entry in artifacts if entry['kind'] == 'deb']
    versions = expected_versions(number, abi)
    require(len(binaries) == 4, 'release must contain exactly four binaries')
    require({entry['package'] for entry in binaries} == set(PACKAGE_ARCHES), 'undeclared or missing binary')
    for entry in binaries:
        package = entry['package']
        require(entry['version'] == versions[package], f'version outside release cohort: {package}')
        require(entry['architecture'] == PACKAGE_ARCHES[package], f'wrong architecture: {package}')
        dependencies = entry['depends']
        if package == 'cyberdeck-service':
            for required in ('kbd', 'openjdk-21-jre-headless'):
                require(re.search(r'(?:^|,\s*)' + re.escape(required) + r'(?:\s|,|$)', dependencies),
                        f'missing service dependency: {required}')
            for required in ('libc6', 'libstdc++6'):
                require(re.search(re.escape(required) + r' \(>= [^)]+\)', dependencies),
                        f'missing generated ABI dependency floor: {required}')
        elif package == 'k230-sharp-drm':
            require(f'k230-kernel-abi (= {versions[package]})' in dependencies,
                    'module must depend on exact marker version')
            require(re.search(r'(?:^|,\s*)kmod(?:\s|,|$)', dependencies), 'module lacks kmod dependency')
    return binaries


def validate_context(context):
    require(isinstance(context, dict) and set(context) == CONTEXT_FIELDS, 'unexpected kernel context fields')
    require(type(context['schema_version']) is int and context['schema_version'] == 1, 'kernel context schema mismatch')
    require(context['conf'] == 'k230_canmv_01studio_defconfig', 'unsupported kernel configuration')
    require(HEX40.fullmatch(context['kernel_source_revision']), 'invalid kernel source revision')
    require(re.fullmatch(r'[A-Za-z0-9._+-]+', context['kernel_release']), 'invalid kernel release')
    require(isinstance(context['compiler_identity'], str) and context['compiler_identity'].strip()
            and '\n' not in context['compiler_identity'], 'invalid compiler identity')
    for field in ('kernel_config_sha256', 'module_symvers_sha256', 'kernel_inputs_sha256', 'compiler_sha256', 'abi_sha256'):
        require(isinstance(context[field], str) and HEX64.fullmatch(context[field]), f'invalid context hash: {field}')
    identity = {key: value for key, value in context.items() if key != 'abi_sha256'}
    require(hashlib.sha256(canonical(identity)).hexdigest() == context['abi_sha256'], 'kernel ABI identity hash mismatch')
    return context


def validate_manifest(manifest, *, repository=REPOSITORY):
    fields = {'schema_version', 'release_tag', 'release_number', 'repository', 'source_commit', 'conf',
              'suite', 'snapshot_suite', 'snapshot_index', 'abi_sha256', 'source_date_epoch',
              'kernel_context', 'sources', 'artifacts'}
    require(isinstance(manifest, dict) and set(manifest) == fields, 'unexpected release manifest fields')
    require(type(manifest['schema_version']) is int and manifest['schema_version'] == 1, 'manifest schema mismatch')
    number = manifest['release_number']
    require(type(number) is int and number > 0, 'manifest release number must be positive')
    tag = f'packages-v{number}'
    require(manifest['release_tag'] == tag and manifest['repository'] == repository, 'manifest tag/repository mismatch')
    require(isinstance(manifest['source_commit'], str) and HEX40.fullmatch(manifest['source_commit']), 'invalid publication commit')
    abi = manifest['abi_sha256']
    require(isinstance(abi, str) and HEX64.fullmatch(abi), 'invalid manifest ABI')
    require(manifest['conf'] == 'k230_canmv_01studio_defconfig', 'unsupported manifest configuration')
    suite = f'trixie-k230-{abi}'
    require(manifest['suite'] == suite and manifest['snapshot_suite'] == f'{suite}-release-{number}', 'suite/ABI mismatch')
    require(type(manifest['source_date_epoch']) is int and manifest['source_date_epoch'] > 0, 'invalid source epoch')
    context = manifest['kernel_context']
    require(set(context) == {'file', 'sha256'} and context['file'] == 'kernel-context.json'
            and HEX64.fullmatch(context['sha256']), 'invalid context asset record')
    index = manifest['snapshot_index']
    require(set(index) == {'file', 'sha256', 'size', 'url'} and index['file'] == 'snapshot-Packages'
            and HEX64.fullmatch(index['sha256']) and type(index['size']) is int and 0 < index['size'] < 2**31,
            'invalid golden snapshot index')
    check_asset_url(index['url'], tag, index['file'], repository)
    require(isinstance(manifest['sources'], dict) and set(manifest['sources']) == {'lock', 'submodules'}, 'invalid source provenance')
    lock = manifest['sources']['lock']
    require(set(lock) == {'schema_version', 'service', 'sdk', 'sharp', 'toolchain', 'debian_snapshot', 'containers', 'tools'}
            and lock['schema_version'] == 1, 'incomplete source lock provenance')
    for name in ('service', 'sdk', 'sharp'):
        source = lock[name]
        require(isinstance(source, dict) and HEX40.fullmatch(source.get('revision', ''))
                and re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', source.get('repository', '')),
                f'invalid source provenance: {name}')
    require(lock['service']['repository'] == 'https://github.com/ardangelo/cyberdeck-service.git', 'noncanonical service source')
    require(lock['sdk'].get('conf') == manifest['conf'] and lock['debian_snapshot'] == '20250809T000000Z', 'source configuration/snapshot mismatch')
    for name in ('service', 'kernel'):
        require(re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}', lock['containers'].get(name, '')), 'unpinned container provenance')
    require(set(lock['tools']) >= {'temurin17', 'android_commandline', 'android_platform36', 'gradle'}, 'missing locked tool')
    for tool in [lock['toolchain'], *lock['tools'].values()]:
        require(urlsplit(tool.get('url', '')).scheme == 'https' and HEX64.fullmatch(tool.get('sha256', '')), 'unpinned tooling provenance')
    require(lock['toolchain'].get('prefix') == 'riscv64-unknown-linux-gnu', 'wrong toolchain prefix')
    require(isinstance(manifest['sources']['submodules'], list), 'invalid submodule provenance')
    paths = set()
    for module in manifest['sources']['submodules']:
        require(set(module) == {'path', 'repository', 'revision'}, 'invalid submodule fields')
        path = module['path']
        require(isinstance(path, str) and path not in paths and not path.startswith('/')
                and all(part not in {'', '.', '..'} for part in path.split('/')), 'unsafe/duplicate submodule path')
        require(HEX40.fullmatch(module['revision']) and re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', module['repository']), 'invalid submodule identity')
        paths.add(path)
    require({'mobileapp', 'src/nanopb'} <= paths, 'missing canonical gitlink provenance')
    artifacts = manifest['artifacts']
    require(isinstance(artifacts, list), 'invalid artifact list')
    names = set()
    for entry in artifacts:
        common = {'file', 'sha256', 'size', 'kind', 'url'}
        expected = common | ({'package', 'version', 'architecture', 'depends'} if entry.get('kind') == 'deb' else set())
        require(set(entry) == expected and entry['kind'] in {'deb', 'source', 'buildinfo', 'changes'}, 'invalid artifact fields/kind')
        filename = basename(entry['file'])
        require(filename not in names and filename not in {'release-manifest.json', 'release-manifest.json.asc', 'snapshot-Packages', 'kernel-context.json'}, 'duplicate/reserved artifact')
        names.add(filename)
        require(HEX64.fullmatch(entry['sha256']) and type(entry['size']) is int and 0 < entry['size'] < 2**31, 'invalid artifact digest/size')
        check_asset_url(entry['url'], tag, filename, repository)
    for source in ('cyberdeck-service', 'k230-status-ready', 'sharp-drm'):
        require(sum(e['kind'] == 'source' and e['file'].startswith(source + '_') and e['file'].endswith('.dsc') for e in artifacts) == 1,
                f'missing or duplicate source package: {source}')
    validate_cohort(artifacts, number, abi)
    return manifest


def validate_index(data, artifacts, suite, tag):
    binaries = [entry for entry in artifacts if entry['kind'] == 'deb']
    expected = {entry['package']: entry for entry in binaries}
    require(len(expected) == 4 and len(binaries) == 4, 'index needs exactly four distinct binaries')
    seen = set()
    for fields in paragraphs(data):
        package = fields.get('Package')
        require(package in expected and package not in seen, 'unexpected or duplicate indexed binary')
        seen.add(package)
        entry = expected[package]
        require(fields.get('Version') == entry['version'] and fields.get('Architecture') == entry['architecture'], 'indexed tuple mismatch')
        require(fields.get('Depends', '') == entry['depends'], 'indexed dependencies mismatch')
        require(fields.get('Filename') == f'pool/{suite}/{tag}/{entry["file"]}', 'unsafe or incorrect index Filename')
        require(fields.get('SHA256') == entry['sha256'] and fields.get('Size') == str(entry['size']), 'indexed bytes mismatch')
    require(seen == set(expected), 'missing indexed binary')


def verify_signature(data, signature, public_key, primary_fingerprint):
    require(re.fullmatch(r'[0-9A-F]{40}', primary_fingerprint), 'invalid primary fingerprint')
    with tempfile.TemporaryDirectory(prefix='cyberdeck-public-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        keyring = home / 'archive.gpg'
        subprocess.run(['gpg', '--batch', '--homedir', str(home), '--dearmor', '--output', str(keyring), str(public_key)], check=True, capture_output=True)
        result = subprocess.run(['gpgv', '--homedir', str(home), '--keyring', str(keyring), '--status-fd', '1', str(signature), str(data)], text=True, capture_output=True)
        require(result.returncode == 0, 'release signature verification failed')
        signatures = [line.split() for line in result.stdout.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
        require(len(signatures) == 1 and signatures[0][-1] == primary_fingerprint, 'release signature uses an unexpected primary key')
        require(not any(code in result.stdout for code in ('REVKEYSIG', 'EXPSIG', 'BADSIG', 'ERRSIG')), 'unusable release signature')


@contextmanager
def signing_home(public_key, primary_fingerprint):
    private = base64.b64decode(os.environ['APT_GPG_PRIVATE_KEY'], validate=True)
    passphrase = os.environ['APT_GPG_PASSPHRASE'].encode() + b'\n'
    signing = os.environ['APT_GPG_SIGNING_FINGERPRINT']
    require(re.fullmatch(r'[0-9A-F]{40}', signing), 'invalid signing subkey fingerprint')
    with tempfile.TemporaryDirectory(prefix='cyberdeck-secret-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        try:
            subprocess.run(['gpg', '--batch', '--homedir', str(home), '--import'], input=private, check=True, capture_output=True)
            listed = subprocess.check_output(['gpg', '--batch', '--homedir', str(home), '--with-colons', '--list-secret-keys'], text=True)
            primary = None
            active = None
            usable = False
            for line in listed.splitlines():
                fields = line.split(':')
                if fields[0] in {'sec', 'ssb'}:
                    active = fields
                elif fields[0] == 'fpr' and active:
                    if active[0] == 'sec':
                        primary = fields[9]
                    elif fields[9] == signing:
                        require(primary == primary_fingerprint, 'signing key primary fingerprint mismatch')
                        require(active[1] not in {'r', 'e', 'd'} and 's' in active[11].lower(), 'signing subkey is revoked, expired or not signing-capable')
                        require(not active[6] or int(active[6]) > int(time.time()), 'signing subkey expired')
                        usable = True
            require(usable, 'configured secret signing subkey is unavailable')

            def sign(data, output, *, clear=False):
                command = ['gpg', '--batch', '--homedir', str(home), '--pinentry-mode', 'loopback',
                           '--passphrase-fd', '0', '--local-user', signing + '!', '--digest-algo', 'SHA256',
                           '--armor', '--output', str(output), '--clearsign' if clear else '--detach-sign', str(data)]
                subprocess.run(command, input=passphrase, check=True, capture_output=True)
                if clear:
                    with tempfile.TemporaryDirectory(prefix='cyberdeck-clear-') as public_home:
                        subprocess.run(['gpg', '--batch', '--homedir', public_home, '--import', str(public_key)], check=True, capture_output=True)
                        subprocess.run(['gpg', '--batch', '--homedir', public_home, '--verify', str(output)], check=True, capture_output=True)
                else:
                    verify_signature(data, output, public_key, primary_fingerprint)
            yield sign
        finally:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], capture_output=True)


def source_checks(records, paths):
    """Recheck both Debian source and binary build chains against actual bytes."""
    parsed = {}
    for name in records:
        if not name.endswith(('.dsc', '.buildinfo', '.changes')):
            continue
        controls = paragraphs(paths[name].read_bytes())
        require(len(controls) == 1, 'source/build metadata must have one paragraph')
        fields = controls[0]
        rows = fields.get('Checksums-Sha256', '').split()
        require(len(rows) >= 3 and len(rows) % 3 == 0, 'source/build SHA256 linkage missing')
        linked = set()
        for offset in range(0, len(rows), 3):
            digest, size, filename = rows[offset:offset + 3]
            basename(filename)
            require(filename in records and filename not in linked and filename != name,
                    'source/build control references missing or repeated artifact')
            require(digest == records[filename]['sha256'] and size == str(records[filename]['size']),
                    'source/build artifact linkage differs from transported bytes')
            linked.add(filename)
        parsed[name] = (fields, linked)
    owned = set()
    binary_owner = {'cyberdeck-service': {'cyberdeck-service'},
                    'k230-status-ready': {'k230-status-ready'},
                    'sharp-drm': {'k230-sharp-drm', 'k230-kernel-abi'}}
    for source, packages in binary_owner.items():
        binaries = {name for name, entry in records.items()
                    if entry['kind'] == 'deb' and entry['package'] in packages}
        owned.update(binaries)
        dsc_name = next((name for name in parsed if name.startswith(source + '_') and name.endswith('.dsc')), None)
        require(dsc_name is not None, f'missing {source} source descriptor')
        version = dsc_name[len(source) + 1:-4]
        require(binaries and {records[name]['version'] for name in binaries} == {version},
                'source/binary version mismatch')
        dsc, source_archives = parsed[dsc_name]
        require(dsc.get('Format') == '3.0 (quilt)' and dsc.get('Source') == source
                and dsc.get('Version') == version, 'source descriptor identity/format mismatch')
        upstream = version.rsplit('-', 1)[0]
        if source == 'cyberdeck-service':
            expected_corresponding = f'{source}_{upstream}.corresponding-sources.tar.gz'
        require(source_archives == {f'{source}_{upstream}.orig.tar.gz',
                                    f'{source}_{version}.debian.tar.xz'}
                and all(records[name]['kind'] == 'source' for name in source_archives),
                'quilt source archives incomplete or misidentified')
        owned.update(source_archives | {dsc_name})
        for extension in ('.buildinfo', '.changes'):
            members = {name for name in parsed if name.startswith(f'{source}_{version}_')
                       and name.endswith(extension)}
            require(len(members) == 2, f'need source and binary {source} {extension}')
            source_members = {name for name in members if parsed[name][0].get('Architecture') == 'source'}
            require(len(source_members) == 1, f'need exactly one source {extension}')
            source_name = source_members.pop()
            binary_name = (members - {source_name}).pop()
            source_fields, source_links = parsed[source_name]
            binary_fields, binary_links = parsed[binary_name]
            for fields in (source_fields, binary_fields):
                require(fields.get('Source') == source and fields.get('Version') == version,
                        'Debian source/build version mismatch')
            require(binary_fields.get('Architecture') != 'source', 'binary control has source architecture')
            if extension == '.buildinfo':
                require(source_links == {dsc_name} and binary_links == binaries,
                        'buildinfo source/binary artifact linkage mismatch')
                source_buildinfo, binary_buildinfo = source_name, binary_name
            else:
                require(source_links == source_archives | {dsc_name, source_buildinfo}
                        and binary_links == binaries | {binary_buildinfo},
                        'changes source/binary artifact linkage mismatch')
            owned.update(members)
    require(expected_corresponding in records and records[expected_corresponding]['kind'] == 'source',
            'service corresponding source archive missing/misversioned')
    owned.add(expected_corresponding)
    require(owned == set(records), 'extra or unlinked Debian source/build artifact')
