from argparse import Namespace
from contextlib import closing, redirect_stderr
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rpa_course.__main__ import run_cycle
from rpa_course.journal import CycleJournal, notify_employee


MAIL = {'RPA_NOTIFY_FROM': 'robot@yandex.ru', 'RPA_NOTIFY_TO': 'employee@example.com'}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / 'technical.sqlite3'
        self.enterContext(redirect_stderr(StringIO()))
        self.password = self.enterContext(patch('rpa_course.journal.get_mail_password', return_value='test-password'))
        self.smtp = self.enterContext(patch('rpa_course.journal.smtplib.SMTP_SSL'))

    def events(self):
        with closing(sqlite3.connect(self.path)) as database:
            return database.execute('SELECT stage, status, error_code FROM events ORDER BY rowid').fetchall()

    def test_retention_and_minimal_schema(self):
        with CycleJournal(self.path) as journal:
            journal.record('price', 'loaded')
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("INSERT INTO events (occurred_at, stage, status) VALUES (datetime('now', '-31 days'), 'old', 'ok')")
            fields = [row[1] for row in database.execute('PRAGMA table_info(events)')]
        with CycleJournal(self.path):
            pass
        self.assertEqual(fields, ['occurred_at', 'request_id', 'stage', 'status', 'error_code'])
        self.assertEqual(self.events(), [('price', 'loaded', None)])

    def test_startup_error_is_logged_even_when_smtp_fails(self):
        args = Namespace(config=self.path.with_suffix('.toml'), check_gigachat=False)
        args.config.write_text('[invalid', encoding='utf-8')
        self.smtp.side_effect = OSError
        with patch.dict('os.environ', MAIL), CycleJournal(self.path) as journal:
            self.assertEqual(run_cycle(args, journal), 1)
        self.smtp.assert_called_once()
        self.assertIn(('startup', 'error', 'TOMLDecodeError'), self.events())
        self.assertIn(('notification', 'error', 'OSError'), self.events())

    def test_sends_one_summary_only_for_alerts(self):
        with patch.dict('os.environ', MAIL), CycleJournal(self.path) as journal:
            journal.record('analysis', 'clarification', 'a')
            notify_employee(journal)
            self.smtp.assert_not_called()
            journal.record('analysis', 'needs_review', 'b', 'invalid_model_response')
            journal.record('delivery', 'send_unconfirmed', 'c', 'smtp_interrupted')
            notify_employee(journal)
        send = self.smtp.return_value.send_message
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertEqual(str(message['To']), 'employee@example.com')
        self.assertIn('invalid_model_response', message.get_content())
        self.assertIn('smtp_interrupted', message.get_content())
        self.assertNotIn('test-password', message.get_content())
        self.assertEqual(self.events()[-1], ('notification', 'sent', None))
