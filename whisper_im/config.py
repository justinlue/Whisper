"""config.json: the only thing Whisper persists in incognito mode."""
import getpass
import json
import socket

from .paths import config_path

DEFAULT_PORT = 48765
MAX_NICK = 64


def default_nick() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"[:MAX_NICK]
    except Exception:
        return "anonymous"


def load() -> dict:
    cfg = {"nickname": default_nick(), "port": DEFAULT_PORT, "bind": ""}
    try:
        stored = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(stored, dict):
        nick = stored.get("nickname")
        if isinstance(nick, str) and nick.strip():
            cfg["nickname"] = nick.strip()[:MAX_NICK]
        port = stored.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            cfg["port"] = port
        if isinstance(stored.get("bind"), str):
            cfg["bind"] = stored["bind"]
    return cfg


def save(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
