from unittest import TestCase
from unittest.mock import patch

from rpa_course.config import DEFAULT_CONFIG, load_config
from rpa_course.credentials import read_credential
from rpa_course.imap_client import get_mail_password


class DockerConfigTest(TestCase):
    def test_container_reads_mailbox_and_password_from_environment(self):
        with patch.dict('os.environ', {'RPA_MAIL_ADDRESS': 'robot@yandex.ru', 'RPA_MAIL_PASSWORD': 'test'}):
            config = load_config(DEFAULT_CONFIG)
            self.assertEqual(config.mail_address, 'robot@yandex.ru')
            self.assertEqual(get_mail_password(config.mail_address), 'test')

    def test_windows_still_uses_credential_manager_when_variable_is_unset(self):
        with patch.dict('os.environ', {}, clear=True), patch('rpa_course.credentials.sys.platform', 'win32'), \
                patch('rpa_course.credentials.read_windows_credential', return_value='test-key') as credential:
            self.assertEqual(read_credential('GIGACHAT_CREDENTIALS', 'GIGACHAT_CREDENTIALS'), 'test-key')
        credential.assert_called_once_with('GIGACHAT_CREDENTIALS')
