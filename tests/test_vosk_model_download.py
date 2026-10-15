import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from zipfile import ZipFile, ZipInfo

import download_vosk_model as installer
import vosk_model


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.archive = self.directory / 'model.zip'
        self.url = 'https://example.invalid/model.zip'

    def test_http_403_is_actionable_and_not_retried(self):
        error = HTTPError(self.url, 403, 'Forbidden', {}, None)
        with patch.object(installer, 'open_url', side_effect=error) as fetch:
            with self.assertRaisesRegex(installer.ModelDownloadError, 'network'):
                installer.download_archive(self.url, self.archive)
        fetch.assert_called_once()

    def test_connect_proxy_403_is_actionable(self):
        with patch.object(installer, 'open_url', side_effect=URLError('Tunnel connection failed: 403 Forbidden')):
            with self.assertRaisesRegex(installer.ModelDownloadError, 'proxy returned 403'):
                installer.download_archive(self.url, self.archive)

    def test_temporary_failure_retries_then_verifies_bytes(self):
        data = b'model fixture'
        with patch.object(installer, 'MODEL_SHA256', hashlib.sha256(data).hexdigest()), \
                patch.object(installer, 'open_url', side_effect=[URLError('temporary'), io.BytesIO(data)]) as fetch, \
                patch.object(installer.time, 'sleep'):
            installer.download_archive(self.url, self.archive)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.archive.read_bytes(), data)

    def test_wrong_checksum_is_rejected(self):
        with patch.object(installer, 'open_url', return_value=io.BytesIO(b'truncated')):
            with self.assertRaisesRegex(installer.ModelDownloadError, 'SHA-256 mismatch'):
                installer.download_archive(self.url, self.archive)

    def test_http_download_and_downgrade_redirect_are_rejected(self):
        with self.assertRaisesRegex(installer.ModelDownloadError, 'HTTPS'):
            installer.download_archive('http://example.invalid/model.zip', self.archive)
        with self.assertRaisesRegex(installer.ModelDownloadError, 'HTTPS'):
            installer.HTTPSOnlyRedirect().redirect_request(None, None, 302, '', {}, 'http://example.invalid/a')

    def test_path_traversal_and_symlink_archives_are_rejected(self):
        for name, mode in [('../outside', 0o100644), (installer.MODEL_NAME + '/am/final.mdl', 0o120777)]:
            with self.subTest(name=name):
                with ZipFile(self.archive, 'w') as archive:
                    entry = ZipInfo(name)
                    entry.external_attr = mode << 16
                    archive.writestr(entry, b'bad')
                with patch.object(installer, 'MODEL_FILES', {'am/final.mdl': 'unused'}):
                    with self.assertRaises(installer.ModelDownloadError):
                        installer.extract_archive(self.archive, self.directory / 'staging')
        self.assertFalse((self.directory.parent / 'outside').exists())

    def test_install_cache_corruption_and_failed_repair(self):
        data = b'valid model file'
        manifest = {'am/final.mdl': hashlib.sha256(data).hexdigest()}
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, 'w') as archive:
            archive.writestr(installer.MODEL_NAME + '/am/final.mdl', data)
        payload = archive_bytes.getvalue()
        destination = self.directory / 'model'
        with patch.object(installer, 'MODEL_FILES', manifest), \
                patch.object(vosk_model, 'MODEL_FILES', manifest), \
                patch.object(installer, 'MODEL_SHA256', hashlib.sha256(payload).hexdigest()), \
                patch.object(installer, 'open_url', side_effect=lambda *args: io.BytesIO(payload)) as fetch:
            installer.install_model(destination, self.url)
            installer.install_model(destination, self.url)
            self.assertEqual(fetch.call_count, 1, 'Verified cache must work offline')
            file = destination / 'am/final.mdl'
            file.write_bytes(b'corrupted')
            self.assertIn('checksum mismatch', vosk_model.model_problem(destination))
            with patch.object(installer, 'open_url', side_effect=URLError('403 Forbidden')):
                with self.assertRaises(installer.ModelDownloadError):
                    installer.install_model(destination, self.url)
            self.assertEqual(file.read_bytes(), b'corrupted', 'Failed repair must not remove existing files')
            installer.install_model(destination, self.url)
            self.assertEqual(file.read_bytes(), data)
            self.assertFalse(list(self.directory.glob('.vosk-install-*')))

    def test_default_location_does_not_depend_on_working_directory(self):
        with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.cwd', return_value=self.directory):
            self.assertEqual(vosk_model.model_path(), vosk_model.PROJECT_DIR / 'models' / vosk_model.MODEL_NAME)
        with patch.dict(os.environ, {'VOSK_MODEL_PATH': str(self.directory / 'custom')}):
            self.assertEqual(vosk_model.model_path(), self.directory / 'custom')


if __name__ == '__main__':
    unittest.main()
