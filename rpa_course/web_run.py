"""Запуск штатного цикла робота из локальной панели."""

from pathlib import Path
import subprocess
import sys
from threading import Lock, Thread

from .journal import CycleJournal, JOURNAL_PATH, notify_employee


class RunControl:
    def __init__(self, config: Path | None, note: str = ""):
        self.config = config
        self.lock = Lock()
        self.result = {"state": "idle" if config else "disabled", "output": "", "note": note}

    def status(self) -> dict:
        with self.lock:
            return self.result.copy()

    def start(self) -> dict:
        with self.lock:
            if self.result["state"] not in ("disabled", "running"):
                self.result.update(state="running", output="")
                Thread(target=self.run, daemon=False).start()
            return self.result.copy()

    def run(self):
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
