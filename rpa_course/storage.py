"""SQLite-схема, очередь заявок, снимки цен и состояния ответных писем."""

from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3

from .mail_message import IncomingMessage
from .journal import EventRecorder, ignore_event
from .price import PriceSnapshot


def initialize_database(path: Path) -> None:
    """Создаёт таблицы состояний, прайса, входящих, расчётов и ответов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.executescript("""
            BEGIN;
            CREATE TABLE IF NOT EXISTS requests (
                message_id TEXT PRIMARY KEY NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'registered', 'skipped', 'sending', 'sent',
                    'send_unconfirmed', 'needs_review', 'error'
                )),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS price_snapshots (
                sha256 TEXT PRIMARY KEY NOT NULL,
                content BLOB NOT NULL,
                saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS inbox_cursors (
                mailbox TEXT PRIMARY KEY NOT NULL,
                uid_validity INTEGER NOT NULL,
                last_uid INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbox_messages (
                request_id TEXT PRIMARY KEY NOT NULL REFERENCES requests(message_id),
                subject TEXT NOT NULL,
                reply_to TEXT,
                body TEXT,
                issue TEXT,
                internet_message_id TEXT,
                message_references TEXT
            );
            CREATE TABLE IF NOT EXISTS order_analyses (
                request_id TEXT PRIMARY KEY NOT NULL REFERENCES requests(message_id),
                price_sha256 TEXT NOT NULL REFERENCES price_snapshots(sha256),
                outcome TEXT NOT NULL CHECK (outcome IN ('quote', 'clarification', 'needs_review', 'error')),
                result_json TEXT NOT NULL,
                analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS responses (
                request_id TEXT PRIMARY KEY NOT NULL REFERENCES order_analyses(request_id),
                created_at TEXT NOT NULL,
                quote_number TEXT UNIQUE,
                archive_path TEXT,
                error_code TEXT
            );
            COMMIT;
        """)


def save_price_snapshot(path: Path, snapshot: PriceSnapshot) -> None:
    """Сохраняет снимок Excel по контрольной сумме без дублирования одинаковых версий."""
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(
            "INSERT INTO price_snapshots (sha256, content) VALUES (?, ?) ON CONFLICT(sha256) DO NOTHING",
            (snapshot.sha256, snapshot.content),
        )


def get_or_create_inbox_cursor(
    database: sqlite3.Connection, mailbox: str, uid_validity: int, last_uid: int
) -> tuple[int, int, bool]:
    """Возвращает сохранённую позицию IMAP или фиксирует границу при первом запуске."""
    with database:
        inserted = database.execute(
            "INSERT INTO inbox_cursors VALUES (?, ?, ?) ON CONFLICT(mailbox) DO NOTHING",
            (mailbox, uid_validity, last_uid),
        ).rowcount
        validity, checkpoint = database.execute(
            "SELECT uid_validity, last_uid FROM inbox_cursors WHERE mailbox = ?", (mailbox,)
        ).fetchone()
    return validity, checkpoint, bool(inserted)


def save_intake_result(
    database: sqlite3.Connection,
    mailbox: str,
    uid_validity: int,
    uid: int,
    message: IncomingMessage | None,
    log: EventRecorder = ignore_event,
) -> bool:
    """Одной транзакцией регистрирует письмо и продвигает позицию чтения IMAP."""
    inserted = 0
    with database:
        if message is not None:
            # UID может повториться после смены UIDVALIDITY, поэтому храним обе части.
            request_id = f"{mailbox}:{uid_validity}:{uid}"
            inserted = database.execute(
                "INSERT INTO requests (message_id, status) VALUES (?, ?) ON CONFLICT(message_id) DO NOTHING",
                (request_id, message.status),
            ).rowcount
            if inserted and message.status != "skipped":
                database.execute(
                    """INSERT INTO inbox_messages (
                        request_id, subject, reply_to, body, issue, internet_message_id, message_references
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (request_id, message.subject, message.reply_to, message.body, message.issue,
                     message.internet_message_id, message.references),
                )
        database.execute(
            "UPDATE inbox_cursors SET last_uid = MAX(last_uid, ?) WHERE mailbox = ? AND uid_validity = ?",
            (uid, mailbox, uid_validity),
        )
    if message is not None and inserted:
        log('intake', message.status, request_id, message.issue)
    return bool(inserted)


def pending_orders(database: sqlite3.Connection, mailbox: str) -> list[tuple[str, str | None, str | None]]:
    """Выбирает зарегистрированные обращения без результата анализа в порядке их приёма."""
    prefix = f"{mailbox.casefold()}:"
    return database.execute(
        """SELECT r.message_id, m.body, m.issue
           FROM requests AS r JOIN inbox_messages AS m ON m.request_id = r.message_id
           WHERE r.status = 'registered' AND substr(r.message_id, 1, ?) = ?
             AND NOT EXISTS (SELECT 1 FROM order_analyses AS a WHERE a.request_id = r.message_id)
           ORDER BY r.rowid""",
        (len(prefix), prefix),
    ).fetchall()


def save_order_analysis(
    database: sqlite3.Connection, request_id: str, price_sha256: str, outcome: str, result_json: str
) -> bool:
    """Записывает результат с версией прайса и меняет статус при ошибке или ручной проверке."""
    with database:
        inserted = database.execute(
            """INSERT INTO order_analyses (request_id, price_sha256, outcome, result_json)
               SELECT message_id, ?, ?, ? FROM requests WHERE message_id = ? AND status = 'registered'
               ON CONFLICT(request_id) DO NOTHING""",
            (price_sha256, outcome, result_json, request_id),
        ).rowcount
        if inserted and outcome in ("needs_review", "error"):
            database.execute(
                "UPDATE requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE message_id = ?",
                (outcome, request_id),
            )
    return bool(inserted)


@contextmanager
def lock_cycle(path: Path):
    # Отдельный файл блокировки оставляет рабочую базу доступной для записи.
    """Ставит отдельную SQLite-блокировку, чтобы два цикла не обрабатывали одну почту."""
    lock_path = path.with_name(path.name + ".lock")
    with closing(sqlite3.connect(lock_path, timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            raise ValueError("Другой экземпляр робота уже выполняет цикл.") from None
        yield


def recover_interrupted_sends(path: Path, log: EventRecorder = ignore_event) -> int:
    """Помечает прерванные отправки как неподтверждённые и записывает события восстановления."""
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("""
            UPDATE responses SET error_code = 'smtp_interrupted'
            WHERE request_id IN (SELECT message_id FROM requests WHERE status = 'sending')
        """)
        recovered = database.execute("""
            UPDATE requests SET status = 'send_unconfirmed', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'sending'
            RETURNING message_id
        """).fetchall()
    for (request_id,) in recovered:
        log('delivery', 'send_unconfirmed', request_id, 'smtp_interrupted')
    return len(recovered)


def pending_responses(database: sqlite3.Connection, mailbox: str) -> list[sqlite3.Row]:
    """Выбирает зарегистрированные обращения с готовым КП или уточнением для отправки."""
    prefix = f"{mailbox.casefold()}:"
    return database.execute(
        """SELECT r.message_id AS request_id, m.subject, m.reply_to, m.internet_message_id,
                  m.message_references, a.outcome, a.result_json
           FROM requests AS r JOIN inbox_messages AS m ON m.request_id = r.message_id
           JOIN order_analyses AS a ON a.request_id = r.message_id
           WHERE r.status = 'registered' AND substr(r.message_id, 1, ?) = ?
             AND a.outcome IN ('quote', 'clarification')
           ORDER BY r.rowid""",
        (len(prefix), prefix),
    ).fetchall()


def reserve_response(database: sqlite3.Connection, request_id: str, kind: str,
                     now: datetime) -> tuple[datetime, str | None]:
    """Возвращает существующие реквизиты ответа или резервирует дату и очередной номер КП."""
    with database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            "SELECT created_at, quote_number FROM responses WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing:
            return datetime.fromisoformat(existing[0]), existing[1]
        number = None
        if kind == "quote":
            prefix = f"КП-{now:%Y%m%d}-"
            last = database.execute(
                "SELECT MAX(quote_number) FROM responses WHERE quote_number LIKE ?", (prefix + "%",)
            ).fetchone()[0]
            sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
            if sequence > 999:
                raise ValueError("Исчерпана дневная нумерация КП.")
            number = f"{prefix}{sequence:03d}"
        database.execute(
            "INSERT INTO responses (request_id, created_at, quote_number) VALUES (?, ?, ?)",
            (request_id, now.isoformat(timespec="seconds"), number),
        )
        return now, number


def set_delivery_status(database: sqlite3.Connection, request_id: str, status: str,
                        error_code: str | None = None, archive_path: Path | None = None) -> None:
    """Сохраняет статус отправки, код ошибки и путь к архиву ответа."""
    with database:
        database.execute(
            "UPDATE requests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE message_id = ?",
            (status, request_id),
        )
        database.execute(
            "UPDATE responses SET error_code = ?, archive_path = COALESCE(?, archive_path) WHERE request_id = ?",
            (error_code, str(archive_path) if archive_path else None, request_id),
        )
