"""Fetch immutable Git sources and export only tracked, clean build inputs."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


EXCLUDED_PARTS = {
    '.git', '.gradle', '.idea', '.vscode', '.claude', '.omp', '__pycache__',
    '.cache', 'build', 'out', 'node_modules', 'qemu-linux', 'artifacts',
}
EXCLUDED_SUFFIXES = ('.o', '.a', '.so', '.ko', '.class', '.deb', '.dsc', '.changes', '.buildinfo')
EXCLUDED_NAMES = {
    'local.properties', 'cyberdeck_daemon', 'fake_uinput', 'jni_test_daemon',
    'k230_protocol.pb.c', 'k230_protocol.pb.h', 'AGENTS.md', 'CLAUDE.md',
}


def git(root: Path, *args: str) -> str:
    command = ['git', '-C', str(root), *args]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f'Git source command failed in {root}: {result.stderr.strip()}')
    return result.stdout.strip()


def validate_revision(repository: str, revision: str) -> None:
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', repository):
        raise ValueError(f'source must use an anonymous GitHub HTTPS repository: {repository!r}')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('source revision must be a full lowercase Git commit')


def submodules(root: Path) -> list[tuple[Path, str]]:
    entries = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-rz', 'HEAD'])
    result = []
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, revision = metadata.decode().split()
        if mode != '160000':
            continue
        path = PurePosixPath(os.fsdecode(raw_path))
        if path.is_absolute() or '..' in path.parts:
            raise ValueError(f'unsafe gitlink path: {path}')
        result.append((Path(path), revision))
    return sorted(result)


def verify_checkout(root: Path, expected: str) -> list[dict[str, str]]:
    actual = git(root, 'rev-parse', 'HEAD')
    if actual != expected:
        raise ValueError(f'{root}: expected {expected}, found {actual}')
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError(f'dirty source checkout: {root}')
    provenance = []
    for relative, revision in submodules(root):
        child = root / relative
        repository = git(child, 'remote', 'get-url', 'origin')
        validate_revision(repository, revision)
        nested = verify_checkout(child, revision)
        provenance.append({'path': relative.as_posix(), 'repository': repository, 'revision': revision})
        for entry in nested:
            provenance.append({**entry, 'path': (relative / entry['path']).as_posix()})
    return provenance


def fetch(repository: str, revision: str, destination: Path, *, recursive: bool = False) -> list[dict[str, str]]:
    validate_revision(repository, revision)
    destination.mkdir(parents=True, exist_ok=False)
    git(destination, 'init', '--quiet')
    git(destination, 'remote', 'add', 'origin', repository)
    git(destination, 'fetch', '--quiet', '--depth=1', 'origin', revision)
    git(destination, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
    if recursive:
        git(destination, '-c', 'protocol.file.allow=never', '-c', 'protocol.ssh.allow=never',
            'submodule', 'update', '--init', '--recursive', '--depth=1')
    return verify_checkout(destination, revision)


def included(name: str, *, debian: bool) -> bool:
    path = PurePosixPath(name)
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if not debian and path.parts[0] == 'debian':
        return False
    if path.name in EXCLUDED_NAMES or path.name.endswith(EXCLUDED_SUFFIXES):
        return False
    # The checked-in Gradle launcher is source tooling, not a distribution JAR.
    if path.suffix == '.jar' and path.name != 'gradle-wrapper.jar':
        return False
    # Debian products are never inputs, even when accidentally tracked.
    if path.parts[0] == 'debian' and (any(part.startswith('.') for part in path.parts[1:]) or path.name in {
        'files', 'debhelper-build-stamp',
    } or 'cyberdeck-service' in path.parts[1:] or path.name.endswith(('.substvars', '.debhelper'))):
        return False
    return True


def export(root: Path, destination: Path, *, debian: bool = True) -> None:
    """Resolve gitlinks from separately archived trees; never copy worktree output."""
    destination.mkdir(parents=True, exist_ok=True)
    command = ['git', '-C', str(root), 'archive', '--format=tar', 'HEAD']
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
            for member in archive:
                if included(member.name, debian=debian):
                    archive.extract(member, destination, filter='data')
        if process.wait() != 0:
            raise RuntimeError(f'git archive failed: {root}')
    for relative, _revision in submodules(root):
        if included(relative.as_posix(), debian=debian):
            export(root / relative, destination / relative, debian=debian)


def archive(source: Path, output: Path, prefix: str, epoch: int) -> None:
    """Create deterministic upstream source tarballs with no host uid/path/date."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+~-]*', prefix):
        raise ValueError('unsafe source archive prefix')
    import gzip
    with output.open('xb') as target, gzip.GzipFile(fileobj=target, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as result:
            for path in [source, *sorted(source.rglob('*'))]:
                relative = path.relative_to(source)
                name = str(PurePosixPath(prefix) / relative.as_posix())
                info = result.gettarinfo(str(path), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = epoch
                if info.isdir():
                    info.mode = 0o755
                elif info.issym():
                    info.mode = 0o777
                elif info.isfile():
                    info.mode = 0o755 if info.mode & 0o111 else 0o644
                if info.isfile():
                    with path.open('rb') as stream:
                        result.addfile(info, stream)
                else:
                    result.addfile(info)
