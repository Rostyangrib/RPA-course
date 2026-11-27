"""Проверки локального разбора, ответа модели, арифметики и очереди расчётов."""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from rpa_course.__main__ import DEMO_ORDER
from rpa_course.gigachat import GigaChatError, GigaChatSetupError
from rpa_course.mail_message import IncomingMessage
from rpa_course.order_analysis import analyze_order
from rpa_course.order_queue import analyze_pending_orders
from rpa_course.price import load_price
from rpa_course.storage import get_or_create_inbox_cursor, initialize_database, save_intake_result, save_price_snapshot


ROOT = Path(__file__).resolve().parents[1]
MAILBOX = "robot@example.com"


def matched(fragment, sku="P002", quantity=20, unit="шт."):
    """Создаёт тестовый ответ модели для однозначно сопоставленной позиции."""
    return {"fragment": fragment, "match": "matched", "sku": sku, "quantity": quantity, "unit": unit}


def model_result(*positions):
    """Собирает тестовый JSON модели с идентификаторами позиций."""
    return json.dumps({"too_many_positions": False, "positions": [
        {"id": index, **position} for index, position in enumerate(positions, 1)
    ]}, ensure_ascii=False)


class OrderAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Загружает учебный прайс для общего использования в тестах."""
        cls.snapshot = load_price(ROOT / "data/price.xlsx")

    def test_example_uses_catalogue_prices_and_schema(self):
        """Проверяет расчёт примера из ТЗ и состав списка товаров, передаваемого модели."""
        complete = Mock(return_value=model_result(
            matched("Бумага А4 — 5 упаковок", "P001", 5, "упак."),
            matched("синие ручки — 20 штук"),
            matched("регистратор А4 75 мм — 3 шт.", "P004", 3),
        ))
        result = analyze_order(DEMO_ORDER, self.snapshot.products, complete)
        self.assertEqual(result.outcome, "quote")
        self.assertEqual(result.total, Decimal("2790.00"))
        payload = complete.call_args.args[0]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertNotIn("tools", payload)
        catalog = json.loads(payload["messages"][1]["content"])["catalog"]
        self.assertTrue(all("price" not in product for product in catalog))

    def test_merges_quantities_and_preserves_cents(self):
        """Проверяет объединение одинаковых товаров и точность расчёта до копеек."""
        products = tuple(replace(p, price=Decimal("2.55")) if p.sku == "P002" else p
                         for p in self.snapshot.products)
        text = "Синие ручки — 2 шт. и ещё 3 синих ручки."
        complete = lambda _: model_result(matched("Синие ручки — 2 шт.", quantity=2),
                                          matched("3 синих ручки.", quantity=3))
        result = analyze_order(text, products, complete)
        self.assertEqual([line.quantity for line in result.lines], [5])
        self.assertEqual(str(result.total), "12.75")

    def test_invalid_shape_or_invented_article_gets_one_retry(self):
        """Проверяет один повтор при неверном формате или неизвестном артикуле."""
        text = "Синие ручки — 20 штук"
        complete = Mock(side_effect=[model_result(matched(text, sku="fake")), model_result(matched(text))])
        self.assertEqual(analyze_order(text, self.snapshot.products, complete).outcome, "quote")
        self.assertEqual(complete.call_count, 2)

        complete = Mock(return_value=model_result(matched(text, quantity=True)))
        self.assertEqual(analyze_order(text, self.snapshot.products, complete).outcome, "needs_review")
        self.assertEqual(complete.call_count, 2)

    def test_incomplete_order_does_not_produce_partial_quote(self):
        """Проверяет запрос уточнения вместо частичного КП для неполного заказа."""
        text = "Синие ручки — 20 штук, ручки, чёрные ручки, бумага А4 — 5 штук"
        response = model_result(
            matched("Синие ручки — 20 штук"),
            {"fragment": "ручки", "match": "ambiguous"},
            matched("чёрные ручки", "P003", None),
            matched("бумага А4 — 5 штук", "P001", 5),
        )
        result = analyze_order(text, self.snapshot.products, lambda _: response)
        self.assertEqual(result.outcome, "clarification")
        self.assertIsNone(result.total)
        self.assertFalse(result.lines)
        self.assertEqual([issue.code for issue in result.issues], ["ambiguous", "missing_quantity", "invalid_unit"])

    def test_omitted_and_duplicate_ids_cannot_produce_a_quote(self):
        """Проверяет, что пропуск и повтор ID не приводят к выдаче КП."""
        first, second, third = (
            matched("Бумага А4 — 5 упаковок", "P001", 5, "упак."),
            matched("синие ручки — 20 штук"),
            matched("регистратор А4 75 мм — 3 шт.", "P004", 3),
        )
        complete = Mock(side_effect=[model_result(first, third), model_result(first, {**second, 'id': 1}, third)])
        result = analyze_order(DEMO_ORDER, self.snapshot.products, complete)
        self.assertEqual(result.outcome, 'needs_review')
        self.assertFalse(result.lines)
        self.assertEqual(complete.call_count, 2)

    def test_mail_line_wrap_does_not_split_a_product_from_its_quantity(self):
        """Проверяет сохранение количества при переносе строки в письме."""
        text = ('Добрый день! Посчитайте, пожалуйста: бумага А4 – 5 упаковок, синие ручки –\n'
                '20 штук, регистратор А4 75 мм – 3 шт.')
        complete = Mock(return_value=model_result(
            matched('бумага А4 – 5 упаковок', 'P001', 5, 'упак.'),
            matched('синие ручки – 20 штук'),
            matched('регистратор А4 75 мм – 3 шт.', 'P004', 3),
        ))
        result = analyze_order(text, self.snapshot.products, complete)
        self.assertEqual(result.total, Decimal('2790.00'))
        complete.assert_called_once()

    def test_checks_language_before_calling_model_and_accepts_brand_and_size(self):
        """Проверяет проверку языка до GigaChat и приём русского заказа с брендом латиницей."""
        complete = Mock()
        for text in ('Please quote 20 blue pens.', 'Нам потрібні сині ручки, 20 штук.'):
            result = analyze_order(text, self.snapshot.products, complete)
            self.assertEqual([issue.code for issue in result.issues], ['russian_required'])
        complete.assert_not_called()

        text = 'Картридж HP 305 – 2 шт.'
        complete.return_value = model_result({'fragment': text, 'match': 'not_found'})
        self.assertEqual(analyze_order(text, self.snapshot.products, complete).issues[0].code, 'not_found')
        text = 'Бумага А4, 500 листов – 5 упаковок'
        complete.return_value = model_result(matched(text, 'P001', 5, 'упак.'))
        self.assertEqual(analyze_order(text, self.snapshot.products, complete).total, Decimal('1750.00'))

    def test_asks_for_separate_lines_when_order_boundaries_are_unclear(self):
        """Проверяет запрос отдельных строк, когда состав заказа нельзя уверенно разделить."""
        complete = Mock()
        for text in ('Нужны синие и красные ручки по 10 штук.', 'Нужна бумага 5 ручки 10'):
            result = analyze_order(text, self.snapshot.products, complete)
            self.assertEqual([issue.code for issue in result.issues], ['unclear_positions'])
        complete.assert_not_called()

    def test_pack_synonym_preserves_russian_and_flags_conflicting_quantities(self):
        """Проверяет, что пачки считаются упаковками, а два количества требуют уточнения."""
        complete = Mock()
        ambiguous = 'Нужна бумага А4 5 пачек, 500 листов – 5 штук.'
        result = analyze_order(ambiguous, self.snapshot.products, complete)
        self.assertEqual([issue.code for issue in result.issues], ['unclear_positions'])
        complete.assert_not_called()

        request = 'Нужна бумага А4, 500 листов – 5 пачек.'
        complete.return_value = model_result(matched(request, 'P001', 5, 'упак.'))
        self.assertEqual(analyze_order(request, self.snapshot.products, complete).total, Decimal('1750.00'))

    def test_rejects_model_changes_to_written_quantity_and_unit(self):
        """Проверяет сверку ответа модели с явно записанным количеством и единицей."""
        for text, unit, quantity, code in (
            ("Нужна бумага А4, 500 листов – 5 штук.", "упак.", 5, "invalid_unit"),
            ("Нужна бумага А4 – 5 упаковок.", "упак.", 10, "invalid_quantity"),
        ):
            response = model_result(matched(text, "P001", quantity, unit))
            result = analyze_order(text, self.snapshot.products, lambda _: response)
            self.assertEqual(result.outcome, "clarification")
            self.assertEqual(result.issues[0].code, code)

    def test_queue_uses_one_snapshot_and_does_not_repeat_finished_analysis(self):
        """Проверяет единый снимок цен и отсутствие повторного анализа завершённых заявок."""
        db_path = Path(self.enterContext(TemporaryDirectory())) / "state.sqlite3"
        initialize_database(db_path)
        save_price_snapshot(db_path, self.snapshot)
        text = "Синие ручки — 20 штук"
        with closing(sqlite3.connect(db_path)) as db:
            get_or_create_inbox_cursor(db, MAILBOX, 7, 0)
            save_intake_result(db, MAILBOX, 7, 1, IncomingMessage("registered", body=text))
            save_intake_result(db, MAILBOX, 7, 2, IncomingMessage("registered", issue="attachment_only"))
            save_intake_result(db, MAILBOX, 7, 3, IncomingMessage("registered", body=text))

        reject = Mock(side_effect=GigaChatSetupError("authorization"))
        with self.assertRaises(GigaChatSetupError):
            analyze_pending_orders(db_path, MAILBOX, self.snapshot, reject)

        complete = Mock(side_effect=[model_result(matched(text)), GigaChatError("network")])
        counts = analyze_pending_orders(db_path, MAILBOX, self.snapshot, complete)
        self.assertEqual(counts, {"quote": 1, "clarification": 1, "error": 1})
        self.assertEqual(analyze_pending_orders(db_path, MAILBOX, self.snapshot, complete), {})
        self.assertEqual(complete.call_count, 2)

        with closing(sqlite3.connect(db_path)) as db:
            snapshots = db.execute("SELECT DISTINCT price_sha256 FROM order_analyses").fetchall()
            self.assertEqual(snapshots, [(self.snapshot.sha256,)])
            self.assertEqual(db.execute("SELECT status FROM requests ORDER BY rowid").fetchall(),
                             [("registered",), ("registered",), ("error",)])
