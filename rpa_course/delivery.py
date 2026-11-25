from collections import Counter
from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from email.errors import MessageDefect, MessageError
from email.message import EmailMessage
from hashlib import sha256
import json
from os import fsync
from pathlib import Path
import smtplib
import sqlite3
import ssl

from .imap_client import MAIL_TIMEOUT, get_mail_password, mail_login
from .journal import EventRecorder, ignore_event
from .reply import build_reply
from .storage import pending_responses, reserve_response, set_delivery_status


IRKUTSK = timezone(timedelta(hours=8))


def save_archive(path: Path, message: EmailMessage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as archive:
            archive.write(message.as_bytes())
            archive.flush()
            fsync(archive.fileno())
        temporary.replace(path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def send_response(database: sqlite3.Connection, request_id: str, sender: str, password: str,
                  recipient: str, message: EmailMessage, archive: Path, log: EventRecorder = ignore_event) -> str:
    smtp = None
    started = False
    try:
        smtp = smtplib.SMTP_SSL("smtp.yandex.ru", 465, context=ssl.create_default_context(), timeout=MAIL_TIMEOUT)
        smtp.login(mail_login(sender), password)
        set_delivery_status(database, request_id, "sending", archive_path=archive)
        log('delivery', 'sending', request_id, None)
        started = True
        smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
        status, error_code = "sent", None
    except smtplib.SMTPAuthenticationError:
        set_delivery_status(database, request_id, "error", "smtp_auth_rejected", archive)
        log('delivery', 'error', request_id, 'smtp_auth_rejected')
        raise ValueError("Яндекс Почта отклонила авторизацию SMTP.") from None
    except smtplib.SMTPRecipientsRefused:
        status, error_code = "needs_review", "smtp_recipient_rejected"
    except smtplib.SMTPNotSupportedError:
        if not started:
            set_delivery_status(database, request_id, "error", "smtp_auth_unsupported", archive)
            log('delivery', 'error', request_id, 'smtp_auth_unsupported')
            raise ValueError("Яндекс Почта не поддерживает запрошенный способ авторизации SMTP.") from None
        status, error_code = "needs_review", "smtp_recipient_unsupported"
    except smtplib.SMTPResponseException as error:
        status = "send_unconfirmed" if started and not 400 <= error.smtp_code < 600 else "error"
        error_code = f"smtp_{error.smtp_code}"
    except OSError:
        status, error_code = ("send_unconfirmed", "smtp_result_unknown") if started else ("error", "smtp_connection_failed")
    finally:
        if smtp is not None:
            with suppress(OSError):
                smtp.close()
    set_delivery_status(database, request_id, status, error_code, archive)
    log('delivery', status, request_id, error_code)
    return status


def deliver_pending_responses(path: Path, mailbox: str, log: EventRecorder = ignore_event) -> Counter[str]:
    counts: Counter[str] = Counter()
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        pending = pending_responses(database, mailbox)
        if not pending:
            return counts
        password = get_mail_password(mailbox)
        for row in pending:
            request_id = row["request_id"]
            try:
                created_at, number = reserve_response(database, request_id, row["outcome"], datetime.now(IRKUTSK))
                message = build_reply(mailbox, row["reply_to"], row["subject"], row["internet_message_id"],
                                      row["message_references"], created_at, number, json.loads(row["result_json"]))
                log('reply', 'prepared', request_id, None)
                reference = sha256(request_id.encode()).hexdigest()[:20]
                file_name = number or f"{created_at:%Y%m%d}-уточнение-{reference}"
                archive = path.parent / "responses" / f"{file_name}.eml"
                save_archive(archive, message)
                log('archive', 'saved', request_id, None)
            except (OSError, ValueError, MessageError, MessageDefect, KeyError) as error:
                code = "archive_failed" if isinstance(error, OSError) else "invalid_response"
                set_delivery_status(database, request_id, "needs_review", code)
                log('archive' if code == 'archive_failed' else 'reply', 'needs_review', request_id, code)
                counts["needs_review"] += 1
                continue
            status = send_response(database, request_id, mailbox, password, row["reply_to"], message, archive, log)
            counts[status] += 1
    return counts
