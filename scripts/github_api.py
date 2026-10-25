"""GitHub Release transport; publication policy stays in archive/publisher callers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

from release_support import (HTTPSRedirects, REPOSITORY, basename, check_asset_url,
                             download, require)


class GitHub:
    def __init__(self, repository=REPOSITORY, token=None):
        require(repository == REPOSITORY, 'unexpected GitHub publication repository')
        self.repository = repository
        self.token = token if token is not None else os.environ.get('GH_TOKEN', os.environ.get('GITHUB_TOKEN'))
        self.root = f'https://api.github.com/repos/{repository}'
        self.opener = build_opener(HTTPSRedirects())

    def headers(self, accept='application/vnd.github+json'):
        headers = {'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'cyberdeck-packages'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        return headers

    def json(self, method, path, data=None, *, missing_ok=False):
        require(path.startswith('/') and not path.startswith('//'), 'invalid GitHub API relative path')
        body = None if data is None else json.dumps(data).encode()
        headers = self.headers()
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener.open(Request(self.root + path, data=body, headers=headers, method=method), timeout=120) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f'GitHub {method} {path} failed: HTTP {error.code}') from error
        require(len(payload) <= 8 * 1024 * 1024, 'GitHub API response exceeds metadata limit')
        return json.loads(payload) if payload else None

    def releases(self):
        result = []
        page = 1
        while True:
            entries = self.json('GET', f'/releases?per_page=100&page={page}')
            require(isinstance(entries, list), 'invalid GitHub releases response')
            result.extend(entries)
            if len(entries) < 100:
                return result
            page += 1

    def release(self, tag):
        return self.json('GET', '/releases/tags/' + quote(tag, safe=''), missing_ok=True)

    def assets(self, release):
        result = []
        page = 1
        while True:
            entries = self.json('GET', f'/releases/{release["id"]}/assets?per_page=100&page={page}')
            require(isinstance(entries, list), 'invalid GitHub assets response')
            result.extend(entries)
            if len(entries) < 100:
                break
            page += 1
        require(len({entry['name'] for entry in result}) == len(result), 'duplicate release asset names')
        return {entry['name']: entry for entry in result}

    def upload(self, release, path):
        require(release['draft'] is True, 'cannot upload to a published release')
        path = Path(path)
        require(path.is_file() and 0 < path.stat().st_size < 2**31, 'invalid upload asset size')
        url = f'https://uploads.github.com/repos/{self.repository}/releases/{release["id"]}/assets?name={quote(basename(path.name), safe="")}'
        headers = self.headers()
        headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(path.stat().st_size)})
        with path.open('rb') as stream:
            with self.opener.open(Request(url, data=stream, headers=headers, method='POST'), timeout=360) as response:
                return json.loads(response.read(1024 * 1024))

    def asset(self, release, entry, destination, digest, size):
        require(type(entry['id']) is int and entry['id'] > 0 and entry['size'] == size, 'release asset identity/size mismatch')
        check_asset_url(entry['browser_download_url'], release['tag_name'], entry['name'], self.repository)
        if release['draft']:
            url = self.root + f'/releases/assets/{entry["id"]}'
            headers = self.headers('application/octet-stream')
        else:
            url = entry['browser_download_url']
            headers = None
        return download(url, destination, digest, size, headers=headers)

    def metadata(self, release, entry, destination, *, limit=4 * 1024 * 1024):
        """Fetch a still-untrusted manifest/signature; caller MUST verify the signature."""
        require(entry['name'] in {'release-manifest.json', 'release-manifest.json.asc'}, 'unverified fetch is limited to signed metadata')
        require(type(entry['size']) is int and 0 < entry['size'] <= limit, 'signed metadata exceeds size limit')
        check_asset_url(entry['browser_download_url'], release['tag_name'], entry['name'], self.repository)
        if release['draft']:
            url = self.root + f'/releases/assets/{entry["id"]}'
            headers = self.headers('application/octet-stream')
        else:
            url = entry['browser_download_url']
            headers = {'User-Agent': 'cyberdeck-packages'}
        with self.opener.open(Request(url, headers=headers), timeout=120) as response:
            payload = response.read(limit + 1)
        require(len(payload) == entry['size'] and len(payload) <= limit, 'signed metadata response size mismatch')
        with Path(destination).open('xb') as output:
            output.write(payload)
        return Path(destination)
