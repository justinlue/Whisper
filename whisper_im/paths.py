"""Where Whisper keeps its data. Everything hangs off one home directory."""
import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("WHISPER_HOME")
    return Path(override) if override else Path(__file__).resolve().parent.parent


def files_dir() -> Path:
    """Files received while recording (non-incognito). Kept on close."""
    return home() / "files"


def incognito_dir() -> Path:
    """Files received while incognito. Emptied on close and at startup."""
    return files_dir() / "incognito"


def data_dir() -> Path:
    return home() / "data"


def history_path() -> Path:
    return data_dir() / "history.whisper"


def config_path() -> Path:
    return home() / "config.json"
