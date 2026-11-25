"""Проверки IMAP-входа, отбора новых писем и восстановления приёма."""

from contextlib import closing, nullcontext
from email.message import EmailMessage
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rpa_course.imap_client import MailError, open_inbox
from rpa_course.intake import receive_inbox
from rpa_course.storage import initialize_database


ADDRESS = "robot@yandex.ru"


def email_bytes(subject: str, body: str = "Бумага А4 — 5 упаковок.") -> bytes:
    """Формирует тестовое MIME-письмо для имитации входящих."""
    message = EmailMessage()
    message["From"] = "Клиент <customer@example.com>"
    message["Subject"] = subject
    message.set_content(body)
    return message.as_bytes()


class FakeInbox:
    def __init__(self):
        """Сохраняет тестовые письма и настройки имитированного IMAP-ящика."""
        self.validity = 7
        self.last_uid = 40
        self.messages = {40: email_bytes("Расчёт заказа")}
        self.fetched = []
        self.fail_once = None

    def response(self, name):
        """Возвращает подставленные значения версии папки и следующего UID."""
        value = self.validity if name == "UIDVALIDITY" else self.last_uid + 1
        return name, [str(value).encode("ascii")]

    def uid(self, command, *args):
        """Имитирует поиск, чтение письма и временный сбой по заданному UID."""
        if command == "SEARCH":
            _, criterion, uid_range = args
            assert criterion == "UID"
            start, end = map(int, uid_range.split(":"))
            values = [str(uid) for uid in reversed(self.messages) if start <= uid <= end]
            return "OK", [" ".join(values).encode("ascii")]
        assert command == "FETCH" and args[1] == "(BODY.PEEK[])"
        uid = int(args[0])
        self.fetched.append(uid)
        if self.fail_once == uid:
            self.fail_once = None
            raise TimeoutError
        return "OK", [(b"body", self.messages[uid])]


class IntakeTests(unittest.TestCase):
    def setUp(self):
        """Создаёт тестовую базу и подменяет почтовый ящик, пароль и ожидание повтора."""
        directory = self.enterContext(TemporaryDirectory())
        self.database = Path(directory) / "state.sqlite3"
        initialize_database(self.database)
        self.inbox = FakeInbox()
        self.enterContext(patch("rpa_course.intake.get_mail_password", return_value="test"))
        self.open_inbox = self.enterContext(patch("rpa_course.intake.open_inbox", return_value=nullcontext(self.inbox)))
        self.sleep = self.enterContext(patch("rpa_course.intake.sleep"))

    def test_imap_login_uses_short_name_for_personal_yandex_address(self):
        """Проверяет логины личных ящиков Яндекса и ящиков с другими доменами."""
        with patch("rpa_course.imap_client.IMAP4_SSL") as imap:
            connection = imap.return_value
            connection.select.return_value = ("OK", [b"0"])
            for address, login in ((ADDRESS, "robot"), ("robot@company.example", "robot@company.example")):
                with self.subTest(address=address):
                    with open_inbox(address, "test"):
                        connection.login.assert_called_with(login, "test")

    def test_records_only_new_messages_and_resumes_after_timeout(self):
        """Проверяет начальную границу, приём новых заявок и восстановление после тайм-аута."""
        self.assertTrue(receive_inbox(self.database, ADDRESS).initialized)
        self.assertEqual(self.inbox.fetched, [])

        self.inbox.messages[41] = email_bytes("Re: РАСЧЁТ ЗАКАЗА", "Бумага\n\n> Старый заказ")
        self.inbox.messages[42] = email_bytes("Другой вопрос")
        self.inbox.messages[43] = email_bytes("Расчет заказа", "а" * 4001)
        self.inbox.last_uid = 43
        self.inbox.fail_once = 42
        report = receive_inbox(self.database, ADDRESS)
        self.assertEqual((report.registered, report.skipped, report.needs_review), (2, 1, 0))
        self.sleep.assert_called_once_with(30)
        self.assertEqual(self.inbox.fetched, [41, 42, 42, 43])
        self.assertEqual(receive_inbox(self.database, ADDRESS).registered, 0)
        self.assertEqual(self.inbox.fetched, [41, 42, 42, 43])

        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone(), (3,))
            self.assertEqual(db.execute("SELECT body, issue FROM inbox_messages ORDER BY request_id").fetchall(),
                             [("Бумага", None), (None, "body_too_long")])

    def test_stops_if_imap_identifiers_change(self):
        """Проверяет остановку при изменении версии папки, когда старые UID недостоверны."""
        receive_inbox(self.database, ADDRESS)
        self.inbox.validity += 1
        with self.assertRaisesRegex(MailError, "изменила идентификаторы"):
            receive_inbox(self.database, ADDRESS)
        self.assertEqual(self.inbox.fetched, [])

    def test_retries_network_error_but_not_authentication_rejection(self):
        """Проверяет повтор подключения при сетевом сбое и прекращение попыток при неверном пароле."""
        self.open_inbox.side_effect = TimeoutError
        with self.assertRaisesRegex(MailError, "после трёх попыток"):
            receive_inbox(self.database, ADDRESS)
        self.assertEqual(self.open_inbox.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

        self.open_inbox.reset_mock()
        self.sleep.reset_mock()
        self.open_inbox.side_effect = MailError("Яндекс Почта отклонила авторизацию IMAP.")
        with self.assertRaisesRegex(MailError, "авторизацию"):
            receive_inbox(self.database, ADDRESS)
        self.open_inbox.assert_called_once()
        self.sleep.assert_not_called()
