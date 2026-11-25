"""Отбор писем и извлечение адреса ответа и актуального текста заказа."""

from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.message import EmailMessage
from email.parser import BytesParser
import re

from bs4 import BeautifulSoup


MAX_BODY_LENGTH = 4000
QUOTE_START = re.compile(
    r"^\s*(?:On .+ wrote:|.+ (?:писал[аи]?|написал[аи]?):|"
    r"-{2,}\s*(?:original message|forwarded message|исходное сообщение|пересылаемое сообщение).*|"
    r"(?:From|От):\s*.*@.*)\s*$",
    re.IGNORECASE,
)
HIDDEN_HTML = (
    "head, script, style, template, noscript, iframe, svg, blockquote, "
    ".gmail_quote, .yahoo_quoted, .moz-cite-prefix, [hidden]"
)


@dataclass(frozen=True)
class IncomingMessage:
    """Представляет результат разбора письма перед его регистрацией в базе."""
    status: str
    subject: str = ""
    reply_to: str | None = None
    body: str | None = None
    issue: str | None = None
    internet_message_id: str | None = None
    references: str | None = None


def single_address(message: EmailMessage, header_name: str) -> str | None:
    """Извлекает один корректный адрес из указанного почтового заголовка."""
    headers = message.get_all(header_name, [])
    if len(headers) != 1 or headers[0].defects or len(headers[0].addresses) != 1:
        return None
    address = headers[0].addresses[0]
    if address.username and address.domain and not any(ord(c) < 32 for c in address.addr_spec):
        return address.addr_spec
    return None


def is_automatic(message: EmailMessage, sender: str | None) -> bool:
    """Определяет автоответы и уведомления о доставке по заголовкам и отправителю."""
    submitted = str(message.get("Auto-Submitted", "no")).partition(";")[0].strip().casefold()
    report_type = str(message.get_param("report-type", "")).casefold()
    sender_name = (sender or "").partition("@")[0].casefold()
    return (
        submitted != "no"
        or "X-Autoreply" in message
        or "X-Autorespond" in message
        or str(message.get("Return-Path", "")).strip() == "<>"
        or message.get_content_type() in {"message/delivery-status", "message/global-delivery-status"}
        or report_type in {"delivery-status", "global-delivery-status", "disposition-notification"}
        or sender_name == "mailer-daemon"
    )


def html_text(html: str) -> str:
    """Преобразует HTML письма в текст, убирая скрытые элементы и цитаты переписки."""
    content = BeautifulSoup(html, "html.parser")
    outlook_quote = content.select_one("#divRplyFwdMsg")
    if outlook_quote is not None:
        for node in list(outlook_quote.next_elements):
            node.extract()
        outlook_quote.decompose()
    for node in content.select(HIDDEN_HTML):
        node.extract()
    for node in content.find_all(("p", "div", "li", "tr", "section")):
        node.insert_before("\n")
        node.insert_after("\n")
    for node in content.find_all("br"):
        node.replace_with("\n")
    for node in content.find_all(("td", "th")):
        node.insert_after(" ")
    return content.get_text()


def current_text(text: str) -> str:
    """Отсекает распознанные цитаты и оставляет текст текущего письма."""
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line.rstrip())
    return "\n".join(lines).strip()


def parse_message(raw: bytes, own_address: str) -> IncomingMessage:
    """Отбирает заявку по теме и отправителю, извлекает текст и проверяет адрес ответа."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        subject = str(message.get("Subject", ""))
        sender = single_address(message, "From")
        if (
            "расчет заказа" not in subject.casefold().replace("ё", "е")
            or (sender and sender.casefold() == own_address.casefold())
            or is_automatic(message, sender)
        ):
            return IncomingMessage("skipped")

        reply_to = single_address(message, "Reply-To") if "Reply-To" in message else sender
        if reply_to is None:
            return IncomingMessage("needs_review", subject, issue="invalid_reply_address")

        # Файл с именем считается вложением, даже если клиент опустил Content-Disposition.
        for part in message.walk():
            if part.get_filename() is not None and not part.is_attachment():
                del part["Content-Disposition"]
                part["Content-Disposition"] = "attachment"

        body = ""
        for content_type in ("plain", "html"):
            part = message.get_body(preferencelist=(content_type,))
            if part is None:
                continue
            text = part.get_content()
            body = current_text(html_text(text) if content_type == "html" else text)
            if body:
                break

        issue = None
        if len(body) > MAX_BODY_LENGTH:
            issue = "body_too_long"
        elif not body:
            issue = "attachment_only" if any(part.is_attachment() for part in message.walk()) else "empty_body"

        return IncomingMessage(
            "registered",
            subject,
            reply_to,
            body if issue is None else None,
            issue,
            message.get("Message-ID"),
            message.get("References"),
        )
    except (MessageError, LookupError, ValueError, RecursionError):
        return IncomingMessage("needs_review", issue="unreadable_message")
