from datetime import datetime, timedelta
from decimal import Decimal
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import make_msgid
from html import escape
from pathlib import Path
import re
from string import Template


TEMPLATES = Path(__file__).with_name("templates")
MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
REASONS = {
    "ambiguous": "подходит несколько товаров, уточните характеристики",
    "not_found": "товар не найден в прайсе, уточните наименование",
    "missing_quantity": "не указано количество",
    "invalid_quantity": "укажите целое количество от 1 до 10 000",
    "missing_unit": "не указана единица измерения",
    "invalid_unit": "единица измерения не совпадает с прайсом",
    "too_many_positions": "в одном письме можно указать не более 10 позиций",
    "no_positions": "не удалось определить товары из запроса",
    "attachment_only": "перенесите запрос из вложения в тело письма",
    "body_too_long": "текст запроса должен содержать не более 4 000 символов",
    "empty_body": "укажите товары в теле письма",
}


def render(name: str, **fields: str) -> str:
    return Template((TEMPLATES / name).read_text(encoding="utf-8")).substitute(fields)


def format_amount(amount: str) -> str:
    return f"{Decimal(amount):,.2f}".replace(",", " ").replace(".", ",")


def quote_bodies(result: dict, number: str, created_at: datetime) -> tuple[str, str]:
    fields = {
        "number": number,
        "date": created_at.strftime("%d.%m.%Y"),
        "expires": (created_at.date() + timedelta(days=5)).strftime("%d.%m.%Y"),
        "total": format_amount(result["total"]),
    }
    rows = [(line["sku"], line["name"], str(line["quantity"]), line["unit"],
             format_amount(line["price"]), format_amount(line["amount"])) for line in result["lines"]]
    text = "\n".join(" | ".join(row) for row in rows)
    html = "\n".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return render("quote.txt", rows=text, **fields), render("quote.html", rows=html, **fields)


def clarification_bodies(result: dict) -> tuple[str, str]:
    issues = []
    for issue in result["issues"]:
        reason = REASONS.get(issue["code"], "уточните данные заказа")
        fragment = " ".join(issue["fragment"].split())
        issues.append(f"«{fragment}» — {reason}." if fragment else reason.capitalize() + ".")
    return (
        render("clarification.txt", issues="\n".join(f"- {issue}" for issue in issues)),
        render("clarification.html", issues="\n".join(f"<li>{escape(issue)}</li>" for issue in issues)),
    )


def build_reply(sender: str, recipient: str | None, subject: str, original_id: str | None,
                references: str | None, created_at: datetime, number: str | None, result: dict) -> EmailMessage:
    if not original_id or not MESSAGE_ID.fullmatch(original_id.strip()):
        raise ValueError("В обращении отсутствует корректный Message-ID.")
    history = dict.fromkeys(MESSAGE_ID.findall(references or "") + [original_id.strip()])
    message = EmailMessage(policy=SMTP)
    address = Address(addr_spec=recipient)
    if not address.username or not address.domain:
        raise ValueError("В обращении отсутствует адрес ответа.")
    message["From"] = sender
    message["To"] = address
    message["Subject"] = subject if subject.casefold().startswith("re:") else f"Re: {subject}"
    message["Date"] = created_at
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    message["In-Reply-To"] = original_id.strip()
    message["References"] = " ".join(history)
    plain, html = quote_bodies(result, number, created_at) if number else clarification_bodies(result)
    message.set_content(plain, cte="quoted-printable")
    message.add_alternative(html, subtype="html", cte="quoted-printable")
    return message
