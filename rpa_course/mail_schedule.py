"""Чтение и атомарная запись настроек автообработки для панели и скрипта Windows."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock


SCHEDULE_PATH = Path(__file__).with_name('.runtime') / 'schedule' / 'settings.json'
DEFAULT_SCHEDULE = {'enabled': True, 'interval_minutes': 5}
SCHEDULE_WRITE_LOCK = Lock()


def validate_schedule(value: object) -> dict:
    """Проверяет переключатель и целый интервал в пределах рабочего окна."""
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('Укажите, включена ли автообработка.')
    interval = value.get('interval_minutes')
    if type(interval) is not int or not 1 <= interval <= 540:
        raise ValueError('Укажите целый интервал от 1 до 540 минут.')
    return {'enabled': value['enabled'], 'interval_minutes': interval}


def read_schedule(path: Path) -> dict:
    """Возвращает сохранённое расписание или настройки по умолчанию."""
    try:
        return validate_schedule(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return DEFAULT_SCHEDULE.copy()


def write_schedule(path: Path, value: object) -> dict:
    """Атомарно сохраняет расписание или меняет только состояние автообработки."""
    with SCHEDULE_WRITE_LOCK:
        if isinstance(value, dict) and value.keys() == {'enabled'}:
            value = read_schedule(path) | value
        settings = validate_schedule(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=path.parent) as directory:
            pending = Path(directory) / path.name
            pending.write_text(json.dumps(settings), encoding='utf-8')
            pending.replace(path)
    return settings
