"""Exercise draft API-ID downloads over real authenticated HTTPS."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from github_api import GitHub


class AssetServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer isolated-fixture-token':
            self.send_error(403)
            return
        if self.headers.get('Accept') != 'application/octet-stream':
            self.send_error(406)
            return
        payload = self.server.payloads.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for authenticated HTTPS fixture')
class ReleaseAssetDownloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='cyberdeck-release-api-')
        cls.root = Path(cls.temporary.name)
        cls.certificate, key = cls.root / 'certificate.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cls.certificate)], check=True, capture_output=True)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), AssetServer)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.certificate, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name)
        environment = patch.dict(os.environ, {'SSL_CERT_FILE': str(self.certificate)})
        environment.start()
        self.addCleanup(environment.stop)
        self.api = GitHub(token='isolated-fixture-token')
        self.api.root = f'https://127.0.0.1:{self.server.server_port}/repos/ardangelo/cyberdeck-packages'
        self.release = {'tag_name': 'packages-v1', 'draft': True}
        self.package = b'opaque corresponding-source archive\x00\xff\n'
        self.manifest = b'{"release_tag":"packages-v1"}\n'
        self.server.payloads = {
            '/repos/ardangelo/cyberdeck-packages/releases/assets/7': self.package,
            '/repos/ardangelo/cyberdeck-packages/releases/assets/8': self.manifest,
        }

    def entry(self, number, name, payload):
        return {'id': number, 'name': name, 'size': len(payload),
                'browser_download_url': 'https://github.com/ardangelo/cyberdeck-packages/'
                                        'releases/download/untagged-draft-placeholder/' + name}

    def test_draft_asset_and_manifest_use_authenticated_id_despite_placeholder_browser_url(self):
        package = self.entry(7, 'package_1+cyberdeck.1.orig.tar.gz', self.package)
        downloaded = self.destination / package['name']
        self.api.asset(self.release, package, downloaded, hashlib.sha256(self.package).hexdigest(), len(self.package))
        self.assertEqual(downloaded.read_bytes(), self.package)
        manifest = self.entry(8, 'release-manifest.json', self.manifest)
        destination = self.destination / manifest['name']
        self.api.metadata(self.release, manifest, destination)
        self.assertEqual(json.loads(destination.read_bytes()), {'release_tag': 'packages-v1'})

    def test_published_assets_still_reject_wrong_repository_tag_and_basename(self):
        self.release['draft'] = False
        for path in ('someone/else/releases/download/packages-v1/package.orig.tar.gz',
                     'ardangelo/cyberdeck-packages/releases/download/packages-v2/package.orig.tar.gz',
                     'ardangelo/cyberdeck-packages/releases/download/packages-v1/wrong.orig.tar.gz'):
            with self.subTest(path=path):
                entry = self.entry(7, 'package.orig.tar.gz', self.package)
                entry['browser_download_url'] = 'https://github.com/' + path
                destination = self.destination / entry['name']
                with self.assertRaises(ValueError):
                    self.api.asset(self.release, entry, destination,
                                   hashlib.sha256(self.package).hexdigest(), len(self.package))
                self.assertFalse(destination.exists())
        entry = self.entry(8, 'release-manifest.json', self.manifest)
        with self.assertRaises(ValueError):
            self.api.metadata(self.release, entry, self.destination / entry['name'])
        self.assertFalse((self.destination / entry['name']).exists())

    def test_draft_metadata_rejects_nonpositive_or_noninteger_asset_identity(self):
        for identity in (0, -1, True, '../7'):
            with self.subTest(identity=identity):
                manifest = self.entry(identity, 'release-manifest.json', self.manifest)
                destination = self.destination / manifest['name']
                with self.assertRaises(ValueError):
                    self.api.metadata(self.release, manifest, destination)
                self.assertFalse(destination.exists())

    def test_corrupt_draft_bytes_and_metadata_size_fail_without_replacing_files(self):
        package = self.entry(7, 'package.orig.tar.gz', self.package)
        self.server.payloads['/repos/ardangelo/cyberdeck-packages/releases/assets/7'] = b'x' * len(self.package)
        destination = self.destination / package['name']
        with self.assertRaises(ValueError):
            self.api.asset(self.release, package, destination,
                           hashlib.sha256(self.package).hexdigest(), len(self.package))
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name(destination.name + '.partial').exists())
        manifest = self.entry(8, 'release-manifest.json', self.manifest)
        manifest['size'] += 1
        with self.assertRaises(ValueError):
            self.api.metadata(self.release, manifest, self.destination / manifest['name'])
        self.assertFalse((self.destination / manifest['name']).exists())
        destination.write_bytes(b'existing verified bytes')
        with self.assertRaises(ValueError):
            self.api.asset(self.release, package, destination,
                           hashlib.sha256(self.package).hexdigest(), len(self.package))
        self.assertEqual(destination.read_bytes(), b'existing verified bytes')


if __name__ == '__main__':
    unittest.main()
