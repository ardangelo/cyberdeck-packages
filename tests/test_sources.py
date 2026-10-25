import io
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts/sources.py'
SPEC = importlib.util.spec_from_file_location('sources', MODULE_PATH)
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


class SourceArchiveTests(unittest.TestCase):
    def test_archive_ignores_host_permissions_but_preserves_executable_and_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, mode in [('private', 0o700), ('shared', 0o755)]:
                tree = root / label
                tree.mkdir(mode=mode)
                (tree / 'main.c').write_text('int main(void) { return 0; }\n')
                (tree / 'main.c').chmod(0o600 if label == 'private' else 0o644)
                (tree / 'configure').write_text('#!/bin/sh\nexit 0\n')
                (tree / 'configure').chmod(0o700 if label == 'private' else 0o755)
                sources.archive(tree, root / f'{label}.tar.gz', 'source-1.0', 1754697600)
            self.assertEqual((root / 'private.tar.gz').read_bytes(), (root / 'shared.tar.gz').read_bytes())
            with tarfile.open(root / 'private.tar.gz') as archive:
                self.assertEqual(archive.getmember('source-1.0/configure').mode, 0o755)
                self.assertEqual(archive.getmember('source-1.0/main.c').mode, 0o644)
                self.assertEqual(archive.getmember('source-1.0').mode, 0o755)
            (root / 'private/main.c').write_text('int main(void) { return 1; }\n')
            sources.archive(root / 'private', root / 'changed.tar.gz', 'source-1.0', 1754697600)
            self.assertNotEqual((root / 'private.tar.gz').read_bytes(), (root / 'changed.tar.gz').read_bytes())

    def test_source_extraction_rejects_escape_and_special_entries_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            destination = base / 'source'
            destination.mkdir()
            outside = base / 'outside'
            outside.mkdir()
            sentinel = outside / 'sentinel'
            sentinel.write_bytes(b'untouched')
            (destination / 'jump').symlink_to(outside, target_is_directory=True)
            attacks = [
                ('../outside/sentinel', tarfile.REGTYPE, ''),
                (str(sentinel), tarfile.REGTYPE, ''),
                ('jump/sentinel', tarfile.REGTYPE, ''),
                ('escape', tarfile.SYMTYPE, '../outside'),
                ('absolute-link', tarfile.SYMTYPE, str(outside)),
                ('hardlink', tarfile.LNKTYPE, 'jump/sentinel'),
                ('device', tarfile.CHRTYPE, ''),
                ('pipe', tarfile.FIFOTYPE, ''),
            ]
            for name, kind, link in attacks:
                with self.subTest(name=name):
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = link
                    entry.size = 7 if kind == tarfile.REGTYPE else 0
                    data = io.BytesIO()
                    with tarfile.open(fileobj=data, mode='w') as archive:
                        archive.addfile(entry, io.BytesIO(b'changed') if entry.isfile() else None)
                    data.seek(0)
                    with tarfile.open(fileobj=data) as archive:
                        with self.assertRaises(ValueError):
                            sources.extract_member(archive, archive.next(), destination)
                    self.assertEqual(sentinel.read_bytes(), b'untouched')
            self.assertEqual({path.name for path in destination.iterdir()}, {'jump'})


if __name__ == '__main__':
    unittest.main()
