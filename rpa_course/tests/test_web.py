from contextlib import closing
from email.message import EmailMessage
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from rpa_course.price import HEADERS
from rpa_course.storage import initialize_database
from rpa_course.web import PAGE_SIZE, PanelServer, archive_file, list_requests, request_detail, request_row
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

    def test_price_reads_configured_workbook_on_each_request(self):
        price_path = self.path.with_suffix(".xlsx")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Прайс"
        sheet.append(HEADERS)
        sheet.append(("P1", "Бумага", "А4", "лист; бумага А4", "упак.", 350.25))
        workbook.save(price_path)
        self.addCleanup(workbook.close)

        with PanelServer(self.path, 0, RunControl(None), price_path) as server:
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            worker.start()
            try:
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                    connection.request("GET", "/api/price")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    payload = json.load(response)
                    self.assertEqual(payload["items"], [{
                        "sku": "P1", "name": "Бумага", "description": "А4", "synonyms": ["лист", "бумага А4"],
                        "unit": "упак.", "price": "350.25",
                    }])

                    sheet["F2"] = 400
                    workbook.save(price_path)
                    connection.request("GET", "/api/price")
                    self.assertEqual(json.load(connection.getresponse())["items"][0]["price"], "400")

                    price_path.unlink()
                    connection.request("GET", "/api/price")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 503)
                    self.assertIn("прайс-листе", json.load(response)["error"])
            finally:
                server.shutdown()
                worker.join()
