"""Фоновый запуск штатного цикла робота и передача его результата в веб-панель."""

from pathlib import Path
import subprocess
import sys
from threading import Lock, Thread

from .journal import CycleJournal, JOURNAL_PATH, notify_employee


class RunControl:
    """Управляет одним активным процессом робота и хранит состояние последнего запуска."""
    def __init__(self, config: Path | None, note: str = ""):
        """Настраивает источник конфигурации, блокировку и начальное состояние запуска."""
        self.config = config
        self.lock = Lock()
        self.result = {"state": "idle" if config else "disabled", "output": "", "note": note}

    def status(self) -> dict:
        """Возвращает копию состояния под блокировкой для безопасного чтения из панели."""
        with self.lock:
            return self.result.copy()

    def start(self) -> dict:
        """Запускает рабочий поток при свободном роботе и возвращает текущее состояние."""
        with self.lock:
            if self.result["state"] not in ("disabled", "running"):
                self.result.update(state="running", output="")
                Thread(target=self.run, daemon=False).start()
            return self.result.copy()

    def run(self):
        """Вызывает штатный цикл как процесс и сохраняет вывод или ошибку его запуска."""
        try:
            process = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "rpa_course", "--config", str(self.config)],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            state = "failed" if process.returncode else "succeeded"
            output = "\n".join(line.strip() for line in (process.stdout, process.stderr) if line.strip())
        except OSError as error:
            state, output = "failed", "Не удалось запустить процесс робота."
            with CycleJournal(JOURNAL_PATH) as journal:
                journal.record('startup', 'error', error_code=type(error).__name__)
                notify_employee(journal)
        with self.lock:
            self.result.update(state=state, output=output)
