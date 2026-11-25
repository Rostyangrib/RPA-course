"""Проверки архивирования, отправки писем и восстановления после обрывов SMTP."""

from contextlib import closing
from datetime import datetime
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
from smtplib import SMTPAuthenticationError, SMTPServerDisconnected
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rpa_course.delivery import IRKUTSK, deliver_pending_responses
from rpa_course.mail_message import IncomingMessage
from rpa_course.price import load_price
from rpa_course.storage import (
    get_or_create_inbox_cursor,
    initialize_database,
    lock_cycle,
    recover_interrupted_sends,
    save_intake_result,
    save_order_analysis,
    save_price_snapshot,
)


MAILBOX = "robot@yandex.ru"
CLIENT = "customer@example.com"
NOW = datetime(2026, 11, 23, 10, 30, tzinfo=IRKUTSK)
QUOTE = {
    "lines": [{"sku": "P001", "name": "Бумага А4", "quantity": 5, "unit": "упак.",
               "price": "350.00", "amount": "1750.00"}],
    "total": "1750.00",
}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        """Создаёт тестовую базу и подменяет SMTP, пароль и время формирования письма."""
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "state.sqlite3"
        initialize_database(self.path)
        price = load_price(Path(__file__).resolve().parents[1] / "data/price.xlsx")
        self.price_sha256 = price.sha256
        save_price_snapshot(self.path, price)
        self.smtp = self.enterContext(patch("rpa_course.delivery.smtplib.SMTP_SSL"))
        self.send = self.smtp.return_value.send_message
        self.enterContext(patch("rpa_course.delivery.get_mail_password", return_value="test"))
        self.enterContext(patch("rpa_course.delivery.datetime")).now.return_value = NOW

    def enqueue(self, uid, kind="quote", result=QUOTE):
        """Добавляет в тестовую базу заявку и заранее подготовленный результат анализа."""
        with closing(sqlite3.connect(self.path)) as db:
            get_or_create_inbox_cursor(db, MAILBOX, 11, 0)
            save_intake_result(db, MAILBOX, 11, uid, IncomingMessage(
                "registered", "Расчёт заказа", CLIENT, internet_message_id=f"<order-{uid}@example.com>",
                references="<earlier@example.com>",
            ))
            request_id = f"{MAILBOX}:11:{uid}"
            save_order_analysis(db, request_id, self.price_sha256, kind, json.dumps(result))

    def statuses(self):
        """Читает сохраняемые статусы обращений для сравнения с ожидаемыми."""
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""
                SELECT r.status, p.error_code FROM requests r LEFT JOIN responses p ON p.request_id = r.message_id
                ORDER BY r.rowid
            """).fetchall()

    def test_archives_quotes_before_sending_once_in_original_thread(self):
        """Проверяет наличие архива до отправки, исходную цепочку и отсутствие повторов."""
        self.enqueue(1)
        self.enqueue(2)

        def check_archive(message, *, from_addr, to_addrs):
            """Проверяет архив и статус непосредственно в момент имитированной отправки."""
            self.assertEqual((from_addr, to_addrs), (MAILBOX, [CLIENT]))
            self.assertIn(("sending", None), self.statuses())
            archives = list((self.root / "responses").glob("*.eml"))
            self.assertTrue(any(file.read_bytes() == message.as_bytes() for file in archives))

        self.send.side_effect = check_archive
        with lock_cycle(self.path):
            self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {"sent": 2})
            self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {})
        self.assertEqual(self.send.call_count, 2)
        self.smtp.return_value.login.assert_called_with("robot", "test")
        self.assertEqual(self.smtp.call_args.args, ("smtp.yandex.ru", 465))
        self.assertTrue(self.smtp.call_args.kwargs["context"].check_hostname)

        folder = self.root / "responses"
        self.assertEqual(sorted(file.name for file in folder.iterdir()),
                         ["КП-20261123-001.eml", "КП-20261123-002.eml"])
        message = BytesParser(policy=policy.default).parsebytes((folder / "КП-20261123-001.eml").read_bytes())
        self.assertEqual(message["In-Reply-To"], "<order-1@example.com>")
        self.assertEqual(message["References"], "<earlier@example.com> <order-1@example.com>")
        self.assertEqual(str(message["To"]), CLIENT)
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])
        self.assertIn("1 750,00 руб.", message.get_body(("plain",)).get_content())
        self.assertIn("28.11.2026", message.get_body(("html",)).get_content())
        self.assertFalse(list(message.iter_attachments()))

    def test_uncertain_clarification_is_safe_html_and_is_not_retried(self):
        """Проверяет экранирование текста уточнения и отсутствие повтора при неизвестном исходе."""
        self.enqueue(1, "clarification", {"issues": [{"fragment": "<неизвестный & товар>", "code": "not_found"}]})
        self.send.side_effect = SMTPServerDisconnected()
        self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {"send_unconfirmed": 1})
        self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {})
        self.assertEqual(self.statuses(), [("send_unconfirmed", "smtp_result_unknown")])
        self.send.assert_called_once()
        message = self.send.call_args.args[0]
        self.assertIn("&lt;неизвестный &amp; товар&gt;", message.get_body(("html",)).get_content())
        self.assertIn("полный перечень товаров", message.get_body(("plain",)).get_content())

    def test_recovers_crash_between_send_and_confirmation(self):
        """Проверяет восстановление статуса после остановки между передачей письма и подтверждением."""
        self.enqueue(1)
        self.send.side_effect = KeyboardInterrupt
        with lock_cycle(self.path):
            with self.assertRaisesRegex(ValueError, "Другой экземпляр"), lock_cycle(self.path):
                self.fail("Параллельный цикл не должен стартовать")
            with self.assertRaises(KeyboardInterrupt):
                deliver_pending_responses(self.path, MAILBOX)
        self.assertEqual(self.statuses(), [("sending", None)])
        with lock_cycle(self.path):
            self.assertEqual(recover_interrupted_sends(self.path), 1)
            self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {})
        self.assertEqual(self.statuses(), [("send_unconfirmed", "smtp_interrupted")])
        self.send.assert_called_once()

    def test_archive_failure_prevents_transmission(self):
        """Проверяет перевод на ручную проверку при ошибке сохранения письма."""
        self.enqueue(1)
        with patch("rpa_course.delivery.save_archive", side_effect=PermissionError):
            self.assertEqual(deliver_pending_responses(self.path, MAILBOX), {"needs_review": 1})
        self.assertEqual(self.statuses(), [("needs_review", "archive_failed")])
        self.smtp.assert_not_called()

    def test_authentication_rejection_stops_the_cycle(self):
        """Проверяет завершение цикла при отказе SMTP в авторизации."""
        self.enqueue(1)
        self.enqueue(2)
        self.smtp.return_value.login.side_effect = SMTPAuthenticationError(535, b"test")
        with self.assertRaisesRegex(ValueError, "авторизацию SMTP"):
            deliver_pending_responses(self.path, MAILBOX)
        self.assertEqual(self.statuses(), [("error", "smtp_auth_rejected"), ("registered", None)])
        self.send.assert_not_called()
