"""Расчёт зарегистрированных заявок и сохранение результатов по снимку прайса."""

from collections import Counter
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from .gigachat import GigaChatError
from .journal import EventRecorder, ignore_event
from .order_analysis import Issue, OrderAnalysis, analyze_order
from .price import PriceSnapshot
from .storage import pending_orders, save_order_analysis


def analyze_pending_orders(
    database_path: Path, mailbox: str, snapshot: PriceSnapshot, complete: Callable[[dict], str],
    log: EventRecorder = ignore_event,
) -> Counter[str]:
    """Обрабатывает заявки без результата, сохраняет расчёт или причину и пишет события."""
    counts: Counter[str] = Counter()
    with closing(sqlite3.connect(database_path)) as database:
        database.execute("PRAGMA foreign_keys = ON")
        for request_id, text, issue in pending_orders(database, mailbox):
            if issue or not text:
                result = OrderAnalysis("clarification", (Issue(issue or "empty_body"),))
            else:
                try:
                    result = analyze_order(text, snapshot.products, complete)
                except GigaChatError:
                    result = OrderAnalysis("error", (Issue("gigachat_unavailable"),))

            if save_order_analysis(database, request_id, snapshot.sha256, result.outcome, result.to_json()):
                counts[result.outcome] += 1
                code = result.issues[0].code if result.issues else None
                log('analysis', result.outcome, request_id, code)
                if result.outcome == 'quote':
                    log('calculation', 'completed', request_id, None)
    return counts
