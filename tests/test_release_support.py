import base64
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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
from release_support import download, sha256, signing_home, verify_signature


@unittest.skipUnless(shutil.which('gpg') and shutil.which('gpgv'), 'GnuPG required')
class SignatureBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='cyberdeck-signature-test-')
        cls.root = Path(cls.temporary.name)
        cls.keys = []
        for name in ('archive', 'untrusted'):
            home = cls.root / name
            home.mkdir(mode=0o700)
            base = ['gpg', '--batch', '--homedir', str(home), '--pinentry-mode', 'loopback', '--passphrase', '']
            subprocess.run(base + ['--quick-generate-key', f'{name} fixture <{name}@example.invalid>', 'ed25519', 'cert', '1d'], check=True, capture_output=True)
            listed = subprocess.check_output(base + ['--with-colons', '--list-secret-keys'], text=True)
            primary = next(row.split(':')[9] for row in listed.splitlines() if row.startswith('fpr:'))
            subprocess.run(base + ['--quick-add-key', primary, 'ed25519', 'sign', '1d'], check=True, capture_output=True)
            listed = subprocess.check_output(base + ['--with-colons', '--list-secret-keys'], text=True)
            signing = [row.split(':')[9] for row in listed.splitlines() if row.startswith('fpr:')][-1]
            public = cls.root / f'{name}.asc'
            public.write_bytes(subprocess.check_output(base + ['--armor', '--export', primary]))
            private = subprocess.check_output(base + ['--armor', '--export-secret-subkeys', primary])
            cls.keys.append((home, primary, signing, public, private))

    @classmethod
    def tearDownClass(cls):
        for home, *_ in cls.keys:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], capture_output=True)
        cls.temporary.cleanup()

    def test_signature_rejects_changed_content_and_wrong_trust_key(self):
        home, primary, signing, public, _ = self.keys[0]
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            data = Path(temporary) / 'manifest.json'
            signature = Path(temporary) / 'manifest.asc'
            data.write_bytes(b'{"cohort":1}\n')
            subprocess.run(['gpg', '--batch', '--homedir', str(home), '--local-user', signing + '!',
                            '--armor', '--output', str(signature), '--detach-sign', str(data)], check=True, capture_output=True)
            verify_signature(data, signature, public, primary)
            with self.assertRaises(ValueError):
                verify_signature(data, signature, self.keys[1][3], self.keys[1][1])
            data.write_bytes(b'{"cohort":2}\n')
            with self.assertRaises(ValueError):
                verify_signature(data, signature, public, primary)

    def test_secret_subkey_signer_cleans_private_home_on_interrupted_publication(self):
        _, primary, signing, public, private = self.keys[0]
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            root = Path(temporary)
            runner = root / 'runner'
            runner.mkdir()
            data = root / 'Release'
            signature = root / 'Release.gpg'
            clear = root / 'InRelease'
            data.write_bytes(b'Origin: Cyberdeck\nSuite: fixture\n')
            environment = {'RUNNER_TEMP': str(runner), 'APT_GPG_PRIVATE_KEY': base64.b64encode(private).decode(),
                           'APT_GPG_PASSPHRASE': '', 'APT_GPG_SIGNING_FINGERPRINT': signing}
            with patch.dict(os.environ, environment):
                with self.assertRaisesRegex(RuntimeError, 'interrupted fixture'):
                    with signing_home(public, primary) as sign:
                        sign(data, signature)
                        sign(data, clear, clear=True)
                        verify_signature(data, signature, public, primary)
                        raise RuntimeError('interrupted fixture')
                self.assertEqual(list(runner.iterdir()), [])
                with patch.dict(os.environ, {'APT_GPG_SIGNING_FINGERPRINT': self.keys[1][2]}):
                    with self.assertRaises(ValueError):
                        with signing_home(public, primary):
                            self.fail('unrelated signing subkey accepted')
                self.assertEqual(list(runner.iterdir()), [])
            with tempfile.TemporaryDirectory(dir=root) as public_home:
                ring = Path(public_home) / 'trusted.gpg'
                subprocess.run(['gpg', '--batch', '--homedir', public_home, '--dearmor', '--output', str(ring), str(public)], check=True, capture_output=True)
                plaintext = subprocess.check_output(['gpgv', '--homedir', public_home, '--keyring', str(ring), '--output', '-', str(clear)], stderr=subprocess.DEVNULL)
                self.assertEqual(plaintext, data.read_bytes())


class QuietFiles(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for authenticated HTTPS fixture')
class DownloadBoundaries(unittest.TestCase):
    def test_corruption_rejected_and_existing_outputs_not_replaced_or_removed(self):
        with tempfile.TemporaryDirectory(prefix='cyberdeck-download-test-') as temporary:
            root = Path(temporary)
            web = root / 'web'
            web.mkdir()
            content = b'expected immutable package bytes'
            (web / 'package.deb').write_bytes(content)
            certificate, key = root / 'certificate.pem', root / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', str(key), '-out', str(certificate)], check=True, capture_output=True)
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietFiles, directory=str(web)))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {'SSL_CERT_FILE': str(certificate)}):
                    url = f'https://127.0.0.1:{server.server_port}/package.deb'
                    destination = root / 'download.deb'
                    digest = hashlib.sha256(content).hexdigest()
                    download(url, destination, digest, len(content))
                    self.assertEqual(sha256(destination), digest)
                    (web / 'package.deb').write_bytes(b'corrupted immutable package bytes')
                    rejected = root / 'rejected.deb'
                    with self.assertRaises(ValueError):
                        download(url, rejected, digest)
                    self.assertFalse(rejected.exists())
                    self.assertFalse(rejected.with_name(rejected.name + '.partial').exists())
                    with self.assertRaises(ValueError):
                        download(url, destination, digest)
                    self.assertEqual(destination.read_bytes(), content)
                    partial_path = root / 'in-progress.deb.partial'
                    partial_path.write_bytes(b'owned by another download')
                    with self.assertRaises(FileExistsError):
                        download(url, root / 'in-progress.deb', digest)
                    self.assertEqual(partial_path.read_bytes(), b'owned by another download')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
