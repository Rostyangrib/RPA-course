"""Защищённое подключение к Яндекс Почте и чтение писем по IMAP."""

from contextlib import contextmanager, suppress
from imaplib import IMAP4, IMAP4_SSL
import ssl

from .credentials import read_credential


HOST = "imap.yandex.ru"
PORT = 993
MAIL_TIMEOUT = 30


class MailError(ValueError):
    """Ошибка приёма почты, которую можно показать без данных письма."""


def mail_login(address: str) -> str:
    """Возвращает короткий логин для личного ящика Яндекса и полный адрес для остальных."""
    login, _, domain = address.rpartition("@")
    return login if domain.casefold() == "yandex.ru" else address


def get_mail_password(address: str) -> str:
    """Загружает пароль приложения из переменной окружения или хранилища Windows."""
    password = read_credential(address, "RPA_MAIL_PASSWORD")
    if not password:
        raise MailError("Укажите пароль приложения Яндекс Почты в RPA_MAIL_PASSWORD или хранилище Windows.")
    return password


@contextmanager
def open_inbox(address: str, password: str):
    """Открывает Входящие только для чтения и закрывает соединение при выходе."""
    connection = IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=MAIL_TIMEOUT)
    try:
        try:
            connection.login(mail_login(address), password)
        except IMAP4.abort:
            raise
        except IMAP4.error:
            raise MailError("Яндекс Почта отклонила авторизацию IMAP.") from None
        if connection.select("INBOX", readonly=True)[0] != "OK":
            raise MailError("Не удалось открыть папку «Входящие».")
        yield connection
    finally:
        try:
            connection.logout()
        except (OSError, IMAP4.error):
            with suppress(OSError):
                connection.shutdown()


def inbox_bounds(connection: IMAP4_SSL) -> tuple[int, int]:
    """Возвращает версию папки и верхний UID для текущей проверки входящих."""
    values = []
    for name in ("UIDVALIDITY", "UIDNEXT"):
        data = connection.response(name)[1]
        raw = data[-1] if data else None
        if not isinstance(raw, bytes) or not raw.isdigit() or int(raw) < 1:
            raise MailError(f"IMAP не вернул корректное значение {name}.")
        values.append(int(raw))
    return values[0], values[1] - 1


def search_uids(connection: IMAP4_SSL, after_uid: int, through_uid: int) -> list[int]:
    """Находит UID новых писем между сохранённой позицией и границей текущего цикла."""
    if after_uid >= through_uid:
        return []
    status, data = connection.uid("SEARCH", None, "UID", f"{after_uid + 1}:{through_uid}")
    if status != "OK" or not data or not isinstance(data[0], bytes):
        raise MailError("Не удалось получить список новых писем через IMAP.")
    uids = data[0].split()
    if any(not uid.isdigit() for uid in uids):
        raise MailError("IMAP вернул некорректный список идентификаторов писем.")
    return sorted(uid for uid in set(map(int, uids)) if after_uid < uid <= through_uid)


def fetch_message(connection: IMAP4_SSL, uid: int) -> bytes | None:
    """Читает полное письмо по UID без отметки о прочтении."""
    status, data = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        raise MailError("Не удалось прочитать письмо через IMAP.")
    # Удалённое между SEARCH и FETCH письмо не имеет литерала в ответе.
    for item in data or ():
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    return None
