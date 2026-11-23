from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
import sqlite3
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rpa_course.storage import initialize_database
from rpa_course.web import PAGE_SIZE, archive_file, list_requests, request_detail, request_row
from rpa_course.web_run import RunControl


class WebPanelTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.sqlite3"
        initialize_database(self.path)

    def test_search_uses_irkutsk_date_and_keeps_global_counts(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.executemany("INSERT INTO requests VALUES (?, ?, ?)", [
                ("earlier", "sent", "2026-11-22 15:59:59"),
                ("review", "needs_review", "2026-11-22 16:00:00"),
            ])
            database.execute("INSERT INTO inbox_messages (request_id, subject, issue) VALUES (?, ?, ?)",
                             ("review", "Расчёт заказа", "invalid_reply_address"))

        result = list_requests(self.path, {"q": "РАСЧЕТ", "status": "attention", "from": "2026-11-23"})
        self.assertEqual([item["id"] for item in result["items"]], ["review"])
        self.assertEqual(result["counts"], {"all": 2, "sent": 1, "attention": 1})
        self.assertEqual(list_requests(self.path, {"to": "2026-11-22"})["total"], 1)
        detail = request_detail(self.path, request_row(self.path, "review"))
        self.assertIn("некорректный адрес", detail["issues"][0]["message"])

    def test_pagination_clamps_to_last_page(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.executemany("INSERT INTO requests (message_id, status) VALUES (?, 'sent')",
                                 [(str(index),) for index in range(PAGE_SIZE + 1)])
        first = list_requests(self.path, {})
        last = list_requests(self.path, {"page": "10"})
        self.assertEqual(first["total"], PAGE_SIZE + 1)
        self.assertEqual((last["page"], len(last["items"])), (2, 1))
        self.assertNotIn(last["items"][0]["id"], [item["id"] for item in first["items"]])

    def test_reply_preview_uses_plain_text_and_archive_is_local(self):
        room = self.path.parent / "responses"
        room.mkdir()
        archive = room / "reply.eml"
        message = EmailMessage()
        message.set_content("Ответ на заказ")
        message.add_alternative("<p>HTML письма</p>", subtype="html")
        archive.write_bytes(message.as_bytes())
        other_file = room.parent / archive.name
        other_file.write_bytes(message.as_bytes())
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("INSERT INTO requests (message_id, status) VALUES ('quote', 'sent')")
            database.execute("INSERT INTO order_analyses (request_id, price_sha256, outcome, result_json) "
                             "VALUES ('quote', 'price', 'quote', '{\"total\": \"2790.00\"}')")
            database.execute("INSERT INTO responses (request_id, created_at, archive_path) VALUES ('quote', ?, ?)",
                             ("2026-11-23T09:00:00+08:00", str(archive)))

        result = request_detail(self.path, request_row(self.path, "quote"))
        self.assertEqual(result["archive"], {"name": "reply.eml", "text": "Ответ на заказ"})
        self.assertEqual(result["total"], "2790.00")
        self.assertIsNone(archive_file(self.path, str(other_file)))

    def test_run_starts_once_while_busy_and_reports_robot_errors(self):
        config = self.path.with_suffix(".toml")
        control = RunControl(config)
        with patch("rpa_course.web_run.Thread") as thread:
            self.assertEqual(control.start()["state"], "running")
            self.assertEqual(control.start()["state"], "running")
            thread.return_value.start.assert_called_once()

        completion = CompletedProcess([], 1, stdout="Прайс загружен.\n", stderr="Ошибка IMAP.\n")
        with patch("rpa_course.web_run.subprocess.run", return_value=completion) as launch:
            control.run()
            command = launch.call_args.args[0]
            self.assertEqual(command[-4:], ["-m", "rpa_course", "--config", str(config)])
        self.assertEqual(control.status()["state"], "failed")
        self.assertEqual(control.status()["output"], "Прайс загружен.\nОшибка IMAP.")
