from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from imaplib import IMAP4
from pathlib import Path
import sqlite3
from ssl import SSLCertVerificationError
from time import sleep

from .imap_client import MailError, fetch_message, get_mail_password, inbox_bounds, open_inbox, search_uids
from .mail_message import parse_message
from .storage import get_or_create_inbox_cursor, save_intake_result


MAX_ATTEMPTS = 3
RETRY_INTERVAL = 30


@dataclass(frozen=True)
class IntakeReport:
    initialized: bool
    registered: int
    skipped: int
    needs_review: int


def receive_inbox(database_path: Path, address: str) -> IntakeReport:
    password = get_mail_password(address)
    counts: Counter[str] = Counter()

    with closing(sqlite3.connect(database_path)) as database:
        database.execute("PRAGMA foreign_keys = ON")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                initialized = scan_inbox(database, address, password, counts)
                return IntakeReport(initialized, counts["registered"], counts["skipped"], counts["needs_review"])
            except SSLCertVerificationError:
                raise MailError("Не удалось проверить сертификат IMAP-сервера.") from None
            except (OSError, IMAP4.abort):
                if attempt < MAX_ATTEMPTS:
                    sleep(RETRY_INTERVAL)
            except IMAP4.error:
                raise MailError("Сервер Яндекс Почты не смог выполнить команду IMAP.") from None
    raise MailError("Яндекс Почта недоступна после трёх попыток подключения или чтения.")


def scan_inbox(
    database: sqlite3.Connection, address: str, password: str, counts: Counter[str]
) -> bool:
    mailbox = address.casefold()
    with open_inbox(address, password) as connection:
        uid_validity, last_available_uid = inbox_bounds(connection)
        stored_validity, checkpoint, initialized = get_or_create_inbox_cursor(
            database, mailbox, uid_validity, last_available_uid
        )
        if initialized:
            return True
        if stored_validity != uid_validity:
            raise MailError("Яндекс Почта изменила идентификаторы папки «Входящие». Нужна проверка архива обращений.")

        for uid in search_uids(connection, checkpoint, last_available_uid):
            raw = fetch_message(connection, uid)
            if raw is None:
                continue
            message = parse_message(raw, address)
            if save_intake_result(database, mailbox, uid_validity, uid, message):
                counts[message.status] += 1

        if last_available_uid > checkpoint:
            save_intake_result(database, mailbox, uid_validity, last_available_uid, None)
    return False
