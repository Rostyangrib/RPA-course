from dataclasses import dataclass
from email.errors import InvalidHeaderDefect, MessageError
from email.headerregistry import Address
from pathlib import Path
import tomllib


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
GIGACHAT_BASE_URL = "https://api.giga.chat/v1"


@dataclass(frozen=True)
class GigaChatConfig:
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat-2-Pro"
    base_url: str = GIGACHAT_BASE_URL
    ca_bundle_file: Path | None = None


@dataclass(frozen=True)
class Config:
    price_list: Path
    state_db: Path
    mail_address: str | None = None
    gigachat: GigaChatConfig = GigaChatConfig()


def load_config(path: Path) -> Config:
    with path.open("rb") as source:
        data = tomllib.load(source)

    defaults = GigaChatConfig()
    try:
        paths = data["paths"]
        price_file, state_file = paths["price_list"], paths["state_db"]
        price_list = (path.parent / price_file).resolve()
        state_db = (path.parent / state_file).resolve()
        address = data.get("mail", {}).get("address", "").strip()
        gigachat = data.get("gigachat", {})
        scope = gigachat.get("scope", defaults.scope)
        model = gigachat.get("model", defaults.model).strip()
        base_url = gigachat.get("base_url", defaults.base_url).rstrip("/")
        ca_bundle = gigachat.get("ca_bundle_file", "")
        ca_path = (path.parent / ca_bundle).resolve() if ca_bundle != "" else None
    except (AttributeError, KeyError, TypeError):
        raise ValueError(f"Проверьте поля таблиц [paths], [mail] и [gigachat] в {path}.") from None

    if not price_file.strip() or not state_file.strip():
        raise ValueError(f"В конфигурации {path} укажите paths.price_list и paths.state_db.")
    if price_list == state_db:
        raise ValueError("Для прайс-листа и базы SQLite укажите разные файлы.")
    if address:
        try:
            parsed = Address(addr_spec=address)
            if not parsed.username or not parsed.domain or not address.isascii():
                raise ValueError
        except (ValueError, IndexError, MessageError, InvalidHeaderDefect) as error:
            raise ValueError(f"В конфигурации {path} укажите полный адрес Яндекс Почты.") from error

    if scope not in ("GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"):
        raise ValueError("Укажите действительный gigachat.scope.")
    if not model:
        raise ValueError("Укажите модель в gigachat.model.")
    if base_url != GIGACHAT_BASE_URL:
        raise ValueError(f"Укажите официальный адрес GigaChat {GIGACHAT_BASE_URL}.")
    if gigachat.get("verify_ssl_certs", True) is not True:
        raise ValueError("Для GigaChat требуется проверка TLS-сертификатов.")
    return Config(price_list, state_db, mail_address=address or None,
                  gigachat=GigaChatConfig(scope=scope, model=model, ca_bundle_file=ca_path))
