"""Получение пароля почты и ключа GigaChat из окружения или хранилища Windows."""

from os import getenv
import sys


CREDENTIAL_SERVICE = "RPA-course"


def read_credential(name: str, variable: str) -> str | None:
    """Берёт значение из окружения и при отсутствии переменной проверяет хранилище Windows."""
    value = getenv(variable)
    if value is not None:
        return value.strip() or None
    if sys.platform == "win32":
        return read_windows_credential(name)
    return None


def read_windows_credential(name: str) -> str | None:
    """Читает секрет с указанным именем из записи RPA-course в Windows."""
    if sys.platform != "win32":
        raise ValueError("Нужен диспетчер учётных данных Windows.")

    from keyring.backends.Windows import WinVaultKeyring
    from keyring.errors import KeyringError
    from win32ctypes.pywin32.pywintypes import error as WindowsCredentialError

    try:
        return WinVaultKeyring().get_password(CREDENTIAL_SERVICE, name)
    except (KeyringError, OSError, WindowsCredentialError):
        raise ValueError("Не удалось прочитать запись из хранилища Windows.") from None
