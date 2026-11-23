from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipReadError

from openpyxl import load_workbook


HEADERS = ("Артикул", "Наименование", "Описание", "Синонимы", "Единица", "Цена")
UNITS = {"шт.", "упак."}
MAX_PRODUCTS = 50
MAX_TEXT_LENGTH = 120
FORMULA_ELEMENTS = {"f", "calculatedColumnFormula", "totalsRowFormula"}


class PriceError(ValueError):
    """Прайс-лист не соответствует ТЗ или недоступен."""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    unit: str
    price: Decimal


@dataclass(frozen=True)
class PriceSnapshot:
    sha256: str
    products: tuple[Product, ...]
    content: bytes = field(repr=False)


def load_price(path: Path) -> PriceSnapshot:
    if path.suffix.lower() != ".xlsx":
        raise PriceError(f"Укажите прайс-лист в формате .xlsx: {path}")

    try:
        content = path.read_bytes()
        check_workbook_contents(content)
        with closing(load_workbook(BytesIO(content), read_only=True, keep_links=False)) as workbook:
            sheet = next((sheet for sheet in workbook.worksheets if sheet.title == "Прайс"), None)
            if sheet is None:
                raise PriceError('Отсутствует лист «Прайс».')
            sheet.reset_dimensions()
            products = read_products(sheet.iter_rows())
    except (OSError, BadZipFile, ZipReadError, ElementTree.ParseError,
            KeyError, ValueError, TypeError, IndexError) as error:
        raise PriceError(f"Ошибка в прайс-листе {path}: {error}") from error

    return PriceSnapshot(sha256(content).hexdigest(), products, content)


def check_workbook_contents(content: bytes) -> None:
    with ZipFile(BytesIO(content)) as archive:
        content_types = archive.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vbaproject" in content_types:
            raise PriceError("Макросы запрещены.")

        for name in archive.namelist():
            if name.lower().endswith("/vbaproject.bin"):
                raise PriceError("Макросы запрещены.")
            if name.startswith("xl/externalLinks/"):
                raise PriceError("Внешние ссылки запрещены.")
            if name.endswith(".rels"):
                relationships = ElementTree.fromstring(archive.read(name))
                if any(link.get("TargetMode") == "External" for link in relationships):
                    raise PriceError("Внешние ссылки запрещены.")
            if name.startswith(("xl/worksheets/", "xl/tables/")) and name.endswith(".xml"):
                sheet_xml = ElementTree.fromstring(archive.read(name))
                if any(node.tag.rsplit("}", 1)[-1] in FORMULA_ELEMENTS for node in sheet_xml.iter()):
                    raise PriceError("Формулы запрещены.")


def read_products(rows) -> tuple[Product, ...]:
    header = tuple(cell.value for cell in next(rows, ()))
    if header[:len(HEADERS)] != HEADERS or any(value is not None for value in header[len(HEADERS):]):
        raise PriceError(f"Ожидаются столбцы в таком порядке: {', '.join(HEADERS)}.")

    products = {}
    for row_number, row in enumerate(rows, start=2):
        values = tuple(cell.value for cell in row)
        if all(value is None for value in values):
            continue
        if any(cell.data_type in {"e", "f"} for cell in row):
            raise PriceError(f"Строка {row_number}: формула или ошибка Excel.")
        if len(values) < len(HEADERS) or any(value is not None for value in values[len(HEADERS):]):
            raise PriceError(f"Строка {row_number}: заполните столбцы прайса и уберите лишние данные.")
        if len(products) == MAX_PRODUCTS:
            raise PriceError(f"Прайс-лист может содержать не более {MAX_PRODUCTS} товаров.")

        product = parse_product(values[:len(HEADERS)], row_number)
        if product.sku in products:
            raise PriceError(f"Строка {row_number}: повторяется артикул {product.sku}.")
        products[product.sku] = product

    if not products:
        raise PriceError("В прайс-листе нет товаров.")
    return tuple(products.values())


def parse_product(values: tuple, row: int) -> Product:
    sku, name, description, synonyms, unit, value = values
    if type(sku) is int:
        sku = str(sku)
    if not isinstance(sku, str) or not sku.strip():
        raise PriceError(f"Строка {row}: укажите артикул.")
    if not isinstance(name, str) or not name.strip():
        raise PriceError(f"Строка {row}: укажите наименование.")
    if unit not in UNITS:
        raise PriceError(f"Строка {row}: единица должна быть «шт.» или «упак.».")

    for label, text in (("Описание", description), ("Синонимы", synonyms)):
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH):
            raise PriceError(f"Строка {row}: поле «{label}» должно быть текстом до {MAX_TEXT_LENGTH} символов.")

    if type(value) not in (int, float):
        raise PriceError(f"Строка {row}: цена должна быть числом.")
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0 or price.as_tuple().exponent < -2:
        raise PriceError(f"Строка {row}: цена должна быть положительной и иметь до двух знаков после запятой.")

    aliases = tuple(alias.strip() for alias in (synonyms or "").split(";") if alias.strip())
    return Product(sku.strip(), name.strip(), (description or "").strip(), aliases, unit, price)
