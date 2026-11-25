"""Локальная HTTP-панель сотрудника и API обращений, прайса и технических событий."""

import argparse
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import date
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, quote, urlsplit

from .config import DEFAULT_CONFIG, load_config
from .journal import JOURNAL_PATH
from .price import PriceError, load_price
from .reply import REASONS as CLARIFICATION_REASONS
from .web_run import RunControl


ASSETS = Path(__file__).with_name("web_assets")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/price": ("index.html", "text/html; charset=utf-8"),
    "/technical-journal": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/lada-logo.png": ("lada-logo.png", "image/png"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
PAGE_SIZE = 20
ATTENTION = ("needs_review", "send_unconfirmed", "error")
STATUSES = {"registered", "skipped", "sending", "sent", *ATTENTION}
REASONS = {
    **CLARIFICATION_REASONS,
    "invalid_reply_address": "В письме указан некорректный адрес ответа",
    "unreadable_message": "Не удалось разобрать входящее письмо",
    "invalid_model_response": "Ответ модели не прошёл проверку после повтора",
    "gigachat_unavailable": "Не удалось получить ответ от GigaChat",
    "archive_failed": "Не удалось сохранить копию ответа",
    "invalid_response": "Не удалось сформировать ответ или сохранить цепочку писем",
    "smtp_auth_rejected": "Почта отклонила авторизацию SMTP",
    "smtp_auth_unsupported": "Сервер не поддержал способ авторизации SMTP",
    "smtp_connection_failed": "Не удалось подключиться к SMTP",
    "smtp_recipient_rejected": "Сервер отклонил адрес получателя",
    "smtp_recipient_unsupported": "Сервер не поддержал формат адреса получателя",
    "smtp_result_unknown": "SMTP не подтвердил результат отправки",
    "smtp_interrupted": "Цикл был прерван во время отправки",
}
REQUESTS = """
    FROM requests AS r
    LEFT JOIN inbox_messages AS m ON m.request_id = r.message_id
    LEFT JOIN order_analyses AS a ON a.request_id = r.message_id
    LEFT JOIN responses AS p ON p.request_id = r.message_id
"""


def fold(text: str | None) -> str:
    """Приводит текст к одному регистру и приравнивает ё к е для поиска."""
    return (text or "").casefold().replace("ё", "е")


@contextmanager
def read_database(path: Path):
    """Открывает SQLite только на чтение и фиксирует согласованный снимок данных."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
        database.row_factory = sqlite3.Row
        database.create_function("fold", 1, fold)
        database.execute("BEGIN")
        yield database


def list_requests(path: Path, filters: dict[str, str]) -> dict:
    """Фильтрует обращения, считает статусы и возвращает выбранную страницу."""
    clauses, values = [], []
    status = filters.get("status", "")
    if status == "attention":
        clauses.append("r.status IN (?, ?, ?)")
        values.extend(ATTENTION)
    elif status:
        if status not in STATUSES:
            raise ValueError("Выберите допустимый статус.")
        clauses.append("r.status = ?")
        values.append(status)

    dates = {key: date.fromisoformat(filters[key]) for key in ("from", "to") if filters.get(key)}
    if "from" in dates and "to" in dates and dates["from"] > dates["to"]:
        raise ValueError("Дата начала должна быть раньше даты окончания или совпадать с ней.")
    for key, operator in (("from", ">="), ("to", "<=")):
        if key in dates:
            clauses.append(f"date(r.updated_at, '+8 hours') {operator} ?")
            values.append(dates[key].isoformat())

    search = fold(filters.get("q", "").strip())
    if search:
        clauses.append("instr(fold(r.message_id || ' ' || coalesce(m.subject, '') || ' ' || "
                       "coalesce(m.reply_to, '') || ' ' || coalesce(p.quote_number, '')), ?) > 0")
        values.append(search)

    page = max(1, int(filters.get("page", "1")))
    result = {"items": [], "total": 0, "page": 1, "page_size": PAGE_SIZE,
              "counts": {"all": 0, "sent": 0, "attention": 0}, "ready": path.is_file()}
    if not result["ready"]:
        return result

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with read_database(path) as database:
        counts = dict(database.execute("SELECT status, count(*) FROM requests GROUP BY status").fetchall())
        total = database.execute("SELECT count(*) " + REQUESTS + where, values).fetchone()[0]
        page = min(page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        rows = database.execute("""
            SELECT r.message_id AS id, r.status, replace(r.updated_at, ' ', 'T') || 'Z' AS updated_at,
                   m.subject, m.reply_to, a.outcome, p.quote_number,
                   CASE WHEN json_valid(a.result_json) THEN json_extract(a.result_json, '$.total') END AS total
            """ + REQUESTS + where + " ORDER BY r.updated_at DESC, r.rowid DESC LIMIT ? OFFSET ?",
            [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    result.update(items=[dict(row) for row in rows], total=total, page=page,
                  counts={"all": sum(counts.values()), "sent": counts.get("sent", 0),
                          "attention": sum(counts.get(state, 0) for state in ATTENTION)})
    return result


def list_events(path: Path, filters: dict[str, str]) -> dict:
    """Возвращает страницу технических событий с возможностью выбрать только ошибки."""
    page = max(1, int(filters.get("page", "1")))
    result = {"items": [], "total": 0, "page": 1, "page_size": PAGE_SIZE, "ready": path.is_file()}
    if not result["ready"]:
        return result

    where = " WHERE status = 'error'" if filters.get("errors") == "1" else ""
    with read_database(path) as database:
        total = database.execute("SELECT count(*) FROM events" + where).fetchone()[0]
        page = min(page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        rows = database.execute("""
            SELECT replace(occurred_at, ' ', 'T') || 'Z' AS occurred_at, request_id, stage, status, error_code
            FROM events
        """ + where + " ORDER BY occurred_at DESC, rowid DESC LIMIT ? OFFSET ?",
                                (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    result.update(items=[dict(row) for row in rows], total=total, page=page)
    return result


def archive_file(path: Path, stored: str | None) -> Path | None:
    """Проверяет, что скачиваемый EML существует и находится в папке архива базы."""
    if not stored:
        return None
    root = path.resolve().parent / "responses"
    target = Path(stored).resolve()
    if target.parent == root and target.suffix.lower() == ".eml" and target.is_file():
        return target
    return None


def request_row(path: Path, request_id: str) -> sqlite3.Row | None:
    """Находит обращение по ID и объединяет сведения о письме, анализе и отправке."""
    if path.is_file():
        with read_database(path) as database:
            return database.execute("""
                SELECT r.message_id AS id, r.status, replace(r.updated_at, ' ', 'T') || 'Z' AS updated_at,
                       m.subject, m.reply_to, m.body, m.issue, a.outcome, a.result_json, a.price_sha256,
                       replace(a.analyzed_at, ' ', 'T') || 'Z' AS analyzed_at,
                       p.created_at, p.quote_number, p.archive_path, p.error_code
                """ + REQUESTS + " WHERE r.message_id = ?", (request_id,)).fetchone()
    return None


def request_detail(path: Path, row: sqlite3.Row) -> dict:
    """Подготавливает для карточки расчёт, причины и текстовую часть архивного письма."""
    result = dict(row)
    calculation = json.loads(result.pop("result_json") or "{}")
    issues = calculation.get("issues", [])
    for code in (result.pop("issue"), result.pop("error_code")):
        if code and not any(issue["code"] == code for issue in issues):
            issues.append({"code": code})
    result.update(total=calculation.get("total"), lines=calculation.get("lines", []), issues=[
        {"message": REASONS.get(issue["code"], issue["code"]), "fragment": issue.get("fragment", "")}
        for issue in issues
    ])
    archive = archive_file(path, result.pop("archive_path"))
    result["archive"] = None
    if archive:
        message = BytesParser(policy=policy.default).parsebytes(archive.read_bytes())
        body = message.get_body(("plain",))
        result["archive"] = {"name": archive.name, "text": body.get_content().strip() if body else ""}
    return result


class PanelServer(ThreadingHTTPServer):
    """Связывает HTTP-обработчик с путями к базе и прайсу и управлением запуска."""
    def __init__(self, database: Path, port: int, run_control: RunControl, price_list: Path | None = None,
                 host: str = "127.0.0.1"):
        """Подключает обработчик панели и открывает заданный локальный адрес и порт."""
        self.database = database
        self.run_control = run_control
        self.price_list = price_list
        super().__init__((host, port), PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    """Обрабатывает HTTP-запросы панели и защищает запуск цикла проверкой источника."""
    def log_message(self, format, *args):
        # Параметры интерфейса могут содержать тему письма и адрес клиента.
        """Подавляет стандартный журнал HTTP, чтобы темы и адреса из URL не записывались."""
        pass

    def respond(self, status: int, content: bytes, mime: str, filename: str | None = None):
        """Возвращает тело ответа с нужным MIME-типом и заголовками безопасности."""
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename=response.eml; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(content)

    def json_response(self, value: dict, status: int = 200):
        """Сериализует объект API в JSON в кодировке UTF-8."""
        self.respond(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def panel_origin(self) -> str | None:
        """Проверяет адрес Host и возвращает допустимый источник локального запроса."""
        port = self.server.server_port
        host = self.headers.get("Host", "").lower()
        if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return f"http://{host}"
        return None

    def do_POST(self):
        """Принимает команду запуска робота из локальной панели."""
        origin = self.panel_origin()
        if not origin or self.headers.get("Origin") != origin:
            self.json_response({"error": "Запустите обработку из локальной панели."}, 403)
        elif self.path != "/api/run":
            self.json_response({"error": "Действие не найдено."}, 404)
        else:
            result = self.server.run_control.start()
            self.json_response(result, 202 if result["state"] == "running" else 409)

    def do_GET(self):
        """Возвращает страницы, записи API или архив ответа в зависимости от маршрута."""
        if not self.panel_origin():
            self.json_response({"error": "Панель доступна на этом компьютере."}, 403)
            return
        try:
            url = urlsplit(self.path)
            if url.path in STATIC:
                filename, mime = STATIC[url.path]
                self.respond(200, (ASSETS / filename).read_bytes(), mime)
                return
            filters = {key: values[-1] for key, values in parse_qs(url.query).items()}
            path = self.server.database
            if url.path == "/api/requests":
                self.json_response(list_requests(path, filters))
                return
            if url.path == "/api/events":
                self.json_response(list_events(JOURNAL_PATH, filters))
                return
            if url.path == "/api/run":
                self.json_response(self.server.run_control.status())
                return
            if url.path == "/api/price":
                if self.server.price_list is None:
                    raise PriceError("Для просмотра прайса запустите панель с конфигурацией робота.")
                products = load_price(self.server.price_list).products
                self.json_response({"items": [{**asdict(product), "price": format(product.price, "f")}
                                              for product in products]})
                return
            if url.path in ("/api/detail", "/api/archive"):
                row = request_row(path, filters.get("id", ""))
                if row and url.path == "/api/detail":
                    self.json_response(request_detail(path, row))
                    return
                archive = archive_file(path, row["archive_path"]) if row else None
                if archive:
                    self.respond(200, archive.read_bytes(), "message/rfc822", archive.name)
                    return
        except PriceError as error:
            self.json_response({"error": str(error)}, 503)
            return
        except (ValueError, OverflowError) as error:
            self.json_response({"error": str(error)}, 400)
            return
        except (sqlite3.Error, OSError):
            self.json_response({"error": "Не удалось прочитать данные. Повторите обновление."}, 503)
            return
        self.json_response({"error": "Запись или файл не найдены."}, 404)


def main():
    """Читает настройки панели и запускает HTTP-сервер для выбранной базы."""
    parser = argparse.ArgumentParser(description="Локальный журнал обращений «ОфисСнаб».")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML-файл с настройками робота.")
    source.add_argument("--db", type=Path, help="Открыть другую локальную базу SQLite для просмотра.")
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1", help="Адрес привязки панели.")
    parser.add_argument("--port", type=int, default=8765, help="Локальный порт панели.")
    args = parser.parse_args()
    try:
        price_list = None
        if args.db:
            database = args.db
            run_control = RunControl(None, "Открыта база в режиме просмотра.")
        else:
            config = load_config(args.config)
            database = config.state_db
            price_list = config.price_list
            run_control = (RunControl(args.config.resolve()) if config.mail_address else
                           RunControl(None, "Для обработки почты укажите mail.address или RPA_MAIL_ADDRESS."))
        with PanelServer(database.resolve(), args.port, run_control, price_list, args.host) as server:
            print(f"Панель «ОфисСнаб»: http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(1, f"Не удалось запустить панель: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
