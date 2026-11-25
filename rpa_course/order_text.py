import re

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

from .price import Product


DetectorFactory.seed = 0
WORD = re.compile(r'[^\W\d_]+')
RUSSIAN_WORD = re.compile(r'[а-яё]{2,}')
RUSSIAN_ENDING = re.compile(r'(?<=.{3})(?:ыми|ими|ых|их|ый|ий|ой|ая|яя|ое|ее|ые|ие|ую|юю|[аеёиоуыья])$')
ORDER_WORDS = set('шт штук штука штуки уп упак упаковка упаковок упаковки единиц нужно нужны нужен нужна и еще ещё'.split())
INTRO = re.compile(
    r'^(?:\s*(?:здравствуйте|добрый день|доброе утро|добрый вечер)[!.,\s]+|'
    r'\s*(?:посчитайте|рассчитайте|прошу (?:посчитать|рассчитать))(?:\s+заказ)?'
    r'(?:,?\s+пожалуйста)?\s*:\s*)+', re.IGNORECASE,
)
OUTRO = re.compile(r'(?:[.,!;]|\n)\s*(?:(?:заранее\s+)?спасибо\s*[.!]*\s*$|с уважением\b[\s\S]*$)', re.IGNORECASE)
SEPARATOR = re.compile(r'[,;\n]+|[ \t]+и[ \t]+(?:ещ[её][ \t]+)?', re.IGNORECASE)
AMOUNT = re.compile(r'(?<![\d.,])([-−]?\d+(?:[.,]\d+)?)\s*(шт|штук[аи]?|уп|упак|упаков(?:ок|ки|ка)|единиц[аы]?)\b', re.IGNORECASE)
SPECIFICATION = re.compile(r'(?<![\w.,])\s*\d+(?:[.,]\d+)?\s*(?:мм|см|мкм|мл|лист(?:а|ов)?)\b', re.IGNORECASE)
UNSEPARATED_AMOUNTS = re.compile(r'(?<![\w.,])\d+(?:\s+[а-яё]{2,})+\s+\d+(?![\w.,])', re.IGNORECASE)


def is_russian(text: str, products: tuple[Product, ...]) -> bool:
    words = {word.casefold() for word in WORD.findall(text)}
    if not any(RUSSIAN_WORD.fullmatch(word) for word in words - ORDER_WORDS):
        return False
    try:
        if detect(text) == 'ru':
            return True
    except LangDetectException:
        pass
    # Одно наименование часто совпадает в нескольких языках. Проверяем его по прайсу.
    catalog = {RUSSIAN_ENDING.sub('', word) for product in products for name in (product.name, *product.synonyms)
               for word in WORD.findall(name.casefold())}
    terms = {RUSSIAN_ENDING.sub('', word) for word in words - ORDER_WORDS}
    return terms <= catalog and any(RUSSIAN_WORD.fullmatch(term) for term in terms)


def order_items(text: str) -> list[str] | None:
    text = OUTRO.split(INTRO.sub('', text), maxsplit=1)[0].strip()
    parts = []
    start = 0
    for separator in SEPARATOR.finditer(text):
        before = text[start:separator.start()].strip()
        after = text[separator.end():]
        mark = separator.group()
        if mark == ',':
            decimal_comma = before[-1:].isdigit() and after[:1].isdigit()
            quantity_after_comma = AMOUNT.match(after.lstrip()) and not AMOUNT.search(before)
            if decimal_comma or quantity_after_comma or SPECIFICATION.match(after):
                continue
        if mark.strip().casefold() == 'и' and not (AMOUNT.search(before) or re.match(r'\d+\s', after.lstrip())):
            return None
        if before:
            parts.append(before)
        start = separator.end()
    if text[start:].strip():
        parts.append(text[start:].strip())
    if any(len(AMOUNT.findall(part)) > 1 or ' / ' in part or UNSEPARATED_AMOUNTS.search(SPECIFICATION.sub('', part))
           for part in parts):
        return None
    return parts
