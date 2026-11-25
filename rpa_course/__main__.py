"""Точка входа робота и последовательность этапов обработки заказов."""

import argparse
from pathlib import Path
import sqlite3
import sys

from .config import DEFAULT_CONFIG, load_config
from .delivery import deliver_pending_responses
from .gigachat import GigaChatClient
from .intake import receive_inbox
from .journal import CycleJournal, JOURNAL_PATH, notify_employee
from .order_analysis import analyze_order
from .order_queue import analyze_pending_orders
from .price import load_price
from .storage import initialize_database, lock_cycle, recover_interrupted_sends, save_price_snapshot


DEMO_ORDER = "Бумага А4 — 5 упаковок, синие ручки — 20 штук, регистратор А4 75 мм — 3 шт."


def main() -> int:
    """Читает аргументы запуска и открывает технический журнал для цикла."""
    parser = argparse.ArgumentParser(description="Ручной запуск робота «ОфисСнаб».")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Путь к TOML-файлу.")
    parser.add_argument("--check-gigachat", action="store_true", help="Проверить GigaChat на примере из ТЗ без почты.")
    args = parser.parse_args()
    with CycleJournal(JOURNAL_PATH) as journal:
        return run_cycle(args, journal)


def run_cycle(args: argparse.Namespace, journal: CycleJournal) -> int:
    """Проверяет настройки и прайс, принимает заявки, рассчитывает заказы и отправляет ответы."""
    log = journal.record
    stage, mailbox, exit_code = 'startup', None, 1
    log('cycle', 'started')
    try:
        config = load_config(args.config)
        mailbox = config.mail_address
        initialize_database(config.state_db)
        log(stage, 'ready')
        stage = 'price'
        snapshot = load_price(config.price_list)
        save_price_snapshot(config.state_db, snapshot)
        log(stage, 'loaded')
        gigachat = GigaChatClient(config.gigachat)
        if args.check_gigachat:
            stage = 'analysis'
            result = analyze_order(DEMO_ORDER, snapshot.products, gigachat.complete)
            print(result.to_json())
            exit_code = 0 if result.outcome == "quote" else 1
            log(stage, result.outcome, error_code=result.issues[0].code if result.issues else None)
            return exit_code

        print(f"База состояний готова: {config.state_db}")
        print(f"Прайс-лист загружен. Товаров: {len(snapshot.products)}. SHA-256: {snapshot.sha256}")
        if config.mail_address is None:
            print("Приём почты отключён. Укажите mail.address или RPA_MAIL_ADDRESS для подключения учебного ящика.")
            exit_code = 0
            return exit_code

        stage = 'startup'
        with lock_cycle(config.state_db):
            stage = 'delivery'
            recovered = recover_interrupted_sends(config.state_db, log)
            stage = 'intake'
            report = receive_inbox(config.state_db, mailbox, log)
            log(stage, 'completed')
            stage = 'analysis'
            counts = analyze_pending_orders(config.state_db, mailbox, snapshot, gigachat.complete, log)
            stage = 'delivery'
            delivery = deliver_pending_responses(config.state_db, mailbox, log)
        exit_code = 0
    except (OSError, ValueError, sqlite3.Error) as error:
        log(stage, 'error', error_code=type(error).__name__)
        print(f"Цикл остановлен: {error}", file=sys.stderr)
        return exit_code
    finally:
        log('cycle', 'completed' if exit_code == 0 else 'failed')
        if not args.check_gigachat:
            notify_employee(journal, mailbox)

    if report.initialized:
        print("Зафиксирована граница входящих. Робот начнёт приём с писем, поступивших после неё.")
    else:
        print(f"Почта проверена. Зарегистрировано: {report.registered}, пропущено: {report.skipped}, "
              f"требуют проверки: {report.needs_review}.")
    print(f"Расчёты готовы: {counts['quote']}, уточнения: {counts['clarification']}, "
          f"требуют проверки: {counts['needs_review']}, ошибки: {counts['error']}.")
    print(f"Ответы отправлены: {delivery['sent']}, требуют проверки: {delivery['needs_review']}, "
          f"отправка не подтверждена: {delivery['send_unconfirmed'] + recovered}, ошибки: {delivery['error']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
