#!/usr/bin/env python3
"""Retrieve and validate one original verified release artifact on trusted main."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import zipfile

from github_api import GitHub
from release_support import REPOSITORY, release_number, require
from recovery import download_artifact, extract_verified, source_build

ROOT = Path(__file__).resolve().parents[1]


def archive_module():
    spec = importlib.util.spec_from_file_location('archive_release', ROOT / 'scripts' / 'archive-release.py')
    require(spec is not None and spec.loader is not None, 'archive verifier unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, help='Original Package release workflow run ID')
    parser.add_argument('--release-number', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        number = release_number(args.release_number)
        require(re.fullmatch(r'[1-9][0-9]*', args.run_id) is not None, 'invalid original run ID')
        run_id = int(args.run_id)
        require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY,
                'recovery requires the protected publication repository')
        api = GitHub(REPOSITORY)
        require(api.token, 'recovery requires authenticated GitHub API access')
        archive = archive_module()
        head = archive.git_source(api)
        require(not args.output.exists() and not args.output.is_symlink(),
                'recovery output already exists; use a fresh directory')
        source = source_build(api, run_id, ROOT, head)
        args.output.mkdir(parents=True, exist_ok=False)
        zip_path = download_artifact(api, source, args.output / 'source-artifact.zip')
        verified = extract_verified(zip_path, source, args.output / 'verified')
        archive.verified_assets(verified, number)
    except (ValueError, KeyError, OSError, RuntimeError, ImportError, zipfile.BadZipFile,
            zipfile.LargeZipFile, subprocess.CalledProcessError) as error:
        parser.exit(1, f'recover-release: {error}\n')
    print(f'recovered original verified transport from run {run_id} for packages-v{number}')

if __name__ == '__main__':
    main()
