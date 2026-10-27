"""Constrain manual archive recovery to the original trusted release build and bytes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import shutil
import stat
import subprocess
from urllib.request import Request
import zipfile

import yaml

from release_support import REPOSITORY, basename, require, sha256

MAX_ZIP = 2**31 - 1
MAX_MEMBERS = 256
MAX_EXPANDED = 8 * 2**30
# All scripts not exclusively used for publication, recovery, or post-build verification
# are build inputs. A newly introduced helper is included by default.
NON_BUILD_SCRIPTS = {
    'archive-release.py', 'github_api.py', 'publish-repository.py',
    'recover-release.py', 'recovery.py', 'smoke-apt.py', 'smoke-packages.py',
    'verify-artifacts.py',
}
BUILD_SCRIPTS = {'build-packages.py', 'sources.py', 'kernel-context.py', 'release_support.py'}
WORKFLOW = '.github/workflows/release.yml'


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


def build_input(path: str) -> bool:
    return (path == 'source-lock.json' or path.startswith(('containers/', 'packages/'))
            or (path.startswith('scripts/') and path[len('scripts/'):] not in NON_BUILD_SCRIPTS))


def tree_inputs(root: Path, commit: str) -> dict[str, tuple[str, str]]:
    listing = git(root, 'ls-tree', '-r', '-z', '--full-tree', commit,
                  'source-lock.json', 'containers', 'packages', 'scripts')
    entries = {}
    for item in listing.split(b'\0'):
        if not item:
            continue
        metadata, raw_path = item.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        path = raw_path.decode('utf-8')
        if build_input(path):
            require(kind == 'blob' and mode in {'100644', '100755'}, f'non-regular build input: {path}')
            entries[path] = (mode, oid)
    require({'source-lock.json', *(f'scripts/{name}' for name in BUILD_SCRIPTS)} <= entries.keys(),
            'required build helper or source lock missing')
    require(any(p.startswith('containers/') for p in entries)
            and any(p.startswith('packages/') for p in entries), 'container or package build inputs missing')
    return entries


def workflow_build(root: Path, commit: str | None):
    if commit is None:
        contents = (root / WORKFLOW).read_bytes()
    else:
        contents = git(root, 'show', f'{commit}:{WORKFLOW}')
    # PyYAML's legacy YAML 1.1 resolver otherwise interprets the Actions `on` key as True.
    class ActionsLoader(yaml.SafeLoader):
        pass
    ActionsLoader.yaml_implicit_resolvers = {
        key: [(tag, expression) for tag, expression in entries
              if tag != 'tag:yaml.org,2002:bool']
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    ActionsLoader.add_implicit_resolver('tag:yaml.org,2002:bool',
                                        re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'),
                                        list('tTfF'))
    try:
        workflow = yaml.load(contents, Loader=ActionsLoader)
    except yaml.YAMLError as error:
        raise ValueError('invalid release workflow YAML') from error
    require(isinstance(workflow, dict) and isinstance(workflow.get('jobs'), dict)
            and isinstance(workflow['jobs'].get('build'), dict), 'release workflow has no build job')
    return {key: workflow.get(key) for key in ('env', 'defaults')} | {'build': workflow['jobs']['build']}


def compare_build(root: Path, original: str, head: str) -> None:
    require(root.resolve() == Path(git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve(),
            'recovery root must be the full checkout')
    require(git(root, 'rev-parse', 'HEAD').decode().strip() == head, 'trusted checkout SHA changed')
    # GitHub checkout is expected to be credentialless/full-history; no fetch of an
    # attacker-supplied SHA and no comparison to a synthetic remote tree.
    try:
        kind = git(root, 'cat-file', '-t', original).strip()
    except subprocess.CalledProcessError as error:
        raise ValueError('original build commit missing from checkout history') from error
    require(kind == b'commit', 'original build SHA is not a commit')
    ancestry = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', original, head],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(ancestry.returncode == 0, 'original build commit is not an ancestor of main')
    previous, current = tree_inputs(root, original), tree_inputs(root, head)
    for path in sorted(previous.keys() | current.keys()):
        require(previous.get(path) == current.get(path), f'build input changed since source run: {path}')
    require(workflow_build(root, original) == workflow_build(root, head),
            'release workflow build job or top-level env/defaults changed')
    for raw in git(root, 'status', '--porcelain=v1', '--no-renames', '-z', '--untracked-files=all', '--',
                   'source-lock.json', 'containers', 'packages', 'scripts', WORKFLOW).split(b'\0'):
        if not raw:
            continue
        path = raw[3:].decode('utf-8')
        require(not build_input(path), f'uncommitted build input: {path}')
    require(workflow_build(root, None) == workflow_build(root, head),
            'uncommitted release workflow build job or top-level env/defaults')


def source_build(api, run_id: int, root: Path, head: str) -> dict:
    require(type(run_id) is int and run_id > 0, 'invalid source run ID')
    require(isinstance(head, str) and re.fullmatch(r'[0-9a-f]{40}', head), 'invalid trusted checkout SHA')
    require(api.repository == REPOSITORY, 'source run must belong to publication repository')
    run = api.json('GET', f'/actions/runs/{run_id}')
    require(isinstance(run, dict) and run.get('id') == run_id, 'source run ID mismatch')
    repo = run.get('repository')
    require(isinstance(repo, dict) and repo.get('full_name') == REPOSITORY
            and type(repo.get('id')) is int and repo['id'] > 0,
            'source run belongs to wrong repository')
    original = run.get('head_sha')
    require(isinstance(original, str) and re.fullmatch(r'[0-9a-f]{40}', original), 'invalid source run SHA')
    require(run.get('event') == 'workflow_dispatch' and run.get('head_branch') == 'main'
            and run.get('path') == WORKFLOW and run.get('status') == 'completed'
            and run.get('conclusion') in {'success', 'failure'},
            'source must be a completed main release dispatch from release.yml')
    require(isinstance(run.get('head_repository'), dict)
            and run['head_repository'].get('id') == repo['id']
            and run['head_repository'].get('full_name') == REPOSITORY,
            'source run head repository mismatch')
    workflow = api.json('GET', '/actions/workflows/release.yml')
    require(isinstance(workflow, dict) and workflow.get('id') == run.get('workflow_id')
            and workflow.get('path') == WORKFLOW, 'source run used a different workflow')
    compare_build(Path(root), original, head)
    attempt = run.get('run_attempt')
    require(type(attempt) is int and attempt > 0, 'invalid source run attempt')
    jobs = []
    page = 1
    while True:
        response = api.json('GET', f'/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}')
        require(isinstance(response, dict) and isinstance(response.get('jobs'), list), 'invalid source job listing')
        jobs.extend(response['jobs'])
        require(len(jobs) <= 1000, 'source run has too many jobs')
        if len(response['jobs']) < 100:
            break
        page += 1
    for name in ('Build status-ready', 'Build cyberdeck-service', 'Build sharp-drm',
                 'Verify packages and smoke'):
        matches = [job for job in jobs if job.get('name') == name]
        require(any(job.get('status') == 'completed' and job.get('conclusion') == 'success'
                    for job in matches), f'source job not successful: {name}')
    artifacts = []
    page = 1
    while True:
        response = api.json('GET', f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        require(isinstance(response, dict) and isinstance(response.get('artifacts'), list), 'invalid source artifacts')
        artifacts.extend(response['artifacts'])
        require(len(artifacts) <= 1000, 'source run has too many artifacts')
        if len(response['artifacts']) < 100:
            break
        page += 1
    matches = [entry for entry in artifacts if entry.get('name') == 'verified-packages']
    require(len(matches) == 1, 'source run must have exactly one verified-packages artifact')
    entry = matches[0]
    identity = entry.get('workflow_run')
    require(isinstance(identity, dict) and identity.get('id') == run_id
            and identity.get('repository_id') == repo['id']
            and identity.get('head_repository_id') == repo['id']
            and identity.get('head_branch') == 'main' and identity.get('head_sha') == original,
            'verified artifact belongs to a different source run')
    require(entry.get('expired') is False, 'verified artifact has expired')
    expires = entry.get('expires_at')
    require(isinstance(expires, str), 'verified artifact has no expiry')
    try:
        expiration = datetime.fromisoformat(expires.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('invalid verified artifact expiry') from error
    require(expiration.tzinfo is not None and expiration > datetime.now(timezone.utc),
            'verified artifact has expired')
    digest = entry.get('digest')
    require(isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest),
            'verified artifact missing GitHub SHA-256 digest')
    size = entry.get('size_in_bytes')
    require(type(size) is int and 0 < size <= MAX_ZIP and type(entry.get('id')) is int
            and entry['id'] > 0, 'invalid verified artifact identity/ZIP size')
    return {'run_id': run_id, 'head_sha': original, 'artifact_id': entry['id'],
            'artifact_sha256': digest[7:], 'artifact_size': size}


def verified_zip(archive: Path, source: dict) -> None:
    require(type(source['artifact_size']) is int and 0 < source['artifact_size'] <= MAX_ZIP
            and isinstance(source['artifact_sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', source['artifact_sha256']),
            'invalid verified ZIP metadata')
    require(archive.is_file() and not archive.is_symlink()
            and archive.stat().st_size == source['artifact_size'], 'verified ZIP size mismatch')
    require(sha256(archive) == source['artifact_sha256'], 'verified ZIP SHA-256 mismatch')


def download_artifact(api, source: dict, destination: Path) -> Path:
    destination = Path(destination)
    require(type(source['artifact_id']) is int and source['artifact_id'] > 0, 'invalid artifact ID')
    require(api.repository == REPOSITORY and api.token, 'authenticated publication API required')
    require(not destination.exists() and not destination.is_symlink(), 'refusing to replace verified ZIP')
    size, expected = source['artifact_size'], source['artifact_sha256']
    require(type(size) is int and 0 < size <= MAX_ZIP and isinstance(expected, str)
            and re.fullmatch(r'[0-9a-f]{64}', expected), 'invalid artifact ZIP metadata')
    temporary = destination.with_name(destination.name + '.partial')
    created = False
    try:
        url = api.root + f'/actions/artifacts/{source["artifact_id"]}/zip'
        with api.opener.open(Request(url, headers=api.headers('application/octet-stream')), timeout=120) as response:
            with temporary.open('xb') as output:
                created = True
                digest = hashlib.sha256()
                received = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    received += len(chunk)
                    require(received <= size, 'artifact ZIP exceeds GitHub declared size')
                    digest.update(chunk)
                    output.write(chunk)
        require(received == size and digest.hexdigest() == expected, 'artifact ZIP size/SHA-256 mismatch')
        destination.hardlink_to(temporary)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return destination


def extract_verified(archive: Path, source: dict, destination: Path) -> Path:
    archive, destination = Path(archive), Path(destination)
    verified_zip(archive, source)
    require(not destination.exists() and not destination.is_symlink(), 'refusing to replace extracted cohort')
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            require(0 < len(members) <= MAX_MEMBERS, 'invalid verified ZIP member count')
            names = set()
            total = 0
            for member in members:
                name = basename(member.filename)
                mode = member.external_attr >> 16
                require(name not in names and not member.is_dir() and not (member.external_attr & 0x10)
                        and stat.S_IFMT(mode) in (0, stat.S_IFREG), f'unsafe or repeated ZIP member: {name}')
                require(0 < member.file_size < 2**31 and member.compress_size <= MAX_ZIP,
                        f'verified ZIP member exceeds size bound: {name}')
                total += member.file_size
                require(total <= MAX_EXPANDED, 'verified ZIP expansion exceeds limit')
                names.add(name)
            destination.mkdir()
            try:
                for member in members:
                    with bundle.open(member) as stream, (destination / member.filename).open('xb') as target:
                        copied = 0
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            copied += len(chunk)
                            require(copied <= member.file_size, 'verified ZIP member exceeded declared size')
                            target.write(chunk)
                        require(copied == member.file_size, 'verified ZIP member size mismatch')
            except Exception:
                shutil.rmtree(destination)
                raise
    except zipfile.BadZipFile as error:
        raise ValueError('verified artifact ZIP is corrupt') from error
    return destination
