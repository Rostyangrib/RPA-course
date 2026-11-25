from collections.abc import Callable
from contextlib import closing
from email.errors import MessageError
from email.headerregistry import Address
from email.message import EmailMessage
from os import getenv
from pathlib import Path
import smtplib
import sqlite3
import ssl
import sys

from .imap_client import MAIL_TIMEOUT, get_mail_password, mail_login


JOURNAL_PATH = Path(__file__).with_name('.runtime') / 'technical.sqlite3'
EventRecorder = Callable[[str, str, str | None, str | None], None]
ATTENTION = {'error', 'needs_review', 'send_unconfirmed'}


def ignore_event(*args):
    pass


class CycleJournal:
    def __init__(self, path: Path):
        self.path = path
        self.database = None
        self.alerts = []

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.database = sqlite3.connect(self.path, timeout=2)
            with self.database as database:
                database.execute('''
                    CREATE TABLE IF NOT EXISTS events (
                        occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        request_id TEXT,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_code TEXT
                    )
                ''')
                database.execute("DELETE FROM events WHERE occurred_at < datetime('now', '-30 days')")
        except (OSError, sqlite3.Error):
            if self.database is not None:
                self.database.close()
                self.database = None
            self.record('journal', 'error', error_code='journal_unavailable')
        return self

    def __exit__(self, *exc):
        if self.database is not None:
            self.database.close()

    def record(self, stage: str, status: str, request_id: str | None = None, error_code: str | None = None):
        if stage != 'notification' and status in ATTENTION:
            self.alerts.append((request_id, stage, status, error_code))
        try:
            if self.database is not None:
                with self.database as database:
                    database.execute('INSERT INTO events (request_id, stage, status, error_code) VALUES (?, ?, ?, ?)',
                                     (request_id, stage, status, error_code))
                return
        except sqlite3.Error:
            alert = (None, 'journal', 'error', 'journal_write_failed')
            if alert not in self.alerts:
                self.alerts.append(alert)
            print('Не удалось записать событие в технический журнал.', file=sys.stderr)
        print(f'Журнал недоступен. {stage} | {status} | {request_id or "–"} | {error_code or "–"}', file=sys.stderr)


def notify_employee(journal: CycleJournal, mailbox: str | None = None):
    if not journal.alerts:
        return
    recipient = getenv('RPA_NOTIFY_TO', '').strip()
    sender = getenv('RPA_NOTIFY_FROM', '').strip() or mailbox
    if not recipient or not sender:
        journal.record('notification', 'skipped', error_code='notification_not_configured')
        print('Для уведомлений настройте RPA_NOTIFY_TO и адрес отправителя.', file=sys.stderr)
        return

    try:
        message = EmailMessage()
        message['From'] = Address(addr_spec=sender)
        message['To'] = Address(addr_spec=recipient)
        message['Subject'] = 'ОфисСнаб – требуется внимание'
        lines = ['Нужна проверка результатов цикла робота.', '']
        lines.extend(f'{request_id or "Цикл"} | {stage} | {status} | {code or "–"}'
                     for request_id, stage, status, code in dict.fromkeys(journal.alerts))
        message.set_content('\n'.join(lines))
        password = get_mail_password(sender)
        with closing(smtplib.SMTP_SSL('smtp.yandex.ru', 465, timeout=MAIL_TIMEOUT,
                                      context=ssl.create_default_context())) as smtp:
            smtp.login(mail_login(sender), password)
            smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
    except (OSError, ValueError, MessageError) as error:
        journal.record('notification', 'error', error_code=type(error).__name__)
        print('Уведомление сотруднику не отправлено. Проверьте технический журнал.', file=sys.stderr)
        return
    journal.record('notification', 'sent')
