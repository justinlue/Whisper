"""Incognito / recording state for one run of the app."""
import time
from pathlib import Path

from .files import wipe_incognito
from .history import HistoryLog
from .paths import files_dir, history_path, incognito_dir


class Session:
    """Starts incognito on every launch. While incognito nothing is written:
    messages live only in the UI's memory and received files go to a directory
    that is emptied on close."""

    def __init__(self):
        self.incognito = True
        self.history: HistoryLog | None = None

    def has_history(self) -> bool:
        return HistoryLog.exists(history_path())

    def disable_incognito(self, pin: str | None = None) -> None:
        """Start recording. The PIN is needed the first time in a session;
        raises WrongPin / ValueError and stays incognito if it is rejected."""
        if self.history is None:
            if pin is None:
                raise ValueError("PIN required")
            path = history_path()
            self.history = (HistoryLog.open(path, pin) if HistoryLog.exists(path)
                            else HistoryLog.create(path, pin))
        self.incognito = False

    def enable_incognito(self) -> None:
        """Stop recording. Nothing already saved is deleted."""
        self.incognito = True

    def reset_history(self) -> None:
        """Forgotten PIN: throw the log away so a new PIN can be set."""
        if self.history is not None:
            self.history.close()
            self.history = None
        self.incognito = True
        history_path().unlink(missing_ok=True)

    def record(self, **event) -> None:
        if self.incognito or self.history is None:
            return
        self.history.append({"ts": time.time(), **event})

    def receive_dir(self) -> Path:
        return incognito_dir() if self.incognito else files_dir()

    def close(self) -> list[Path]:
        if self.history is not None:
            self.history.close()
            self.history = None
        return wipe_incognito()
