"""Received-file handling: safe names, and the incognito wipe."""
import shutil
from pathlib import Path

from .paths import files_dir, incognito_dir

PART_SUFFIX = ".part"
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_FORBIDDEN = set('<>:"|?*')
_MAX_NAME = 200


def sanitize_filename(name: str) -> str:
    """Reduce a peer-supplied name to a bare filename that is safe to create
    inside our directory on Windows and POSIX."""
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in _FORBIDDEN)
    name = name.strip().rstrip(". ")
    if not name:
        return "file"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if name.lower().endswith(PART_SUFFIX):
        name += "_"
    if len(name) > _MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 20:
            name = stem[:_MAX_NAME - len(ext) - 1] + "." + ext
        else:
            name = name[:_MAX_NAME]
    return name


def unique_path(directory: Path, name: str) -> Path:
    """A path in `directory` that collides with neither a finished file nor a
    transfer in progress: name.ext, name (1).ext, name (2).ext, ..."""
    def taken(p: Path) -> bool:
        return p.exists() or p.with_name(p.name + PART_SUFFIX).exists()

    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while taken(candidate):
        candidate = directory / f"{stem} ({i}){suffix}"
        i += 1
    return candidate


def incognito_files() -> list[Path]:
    d = incognito_dir()
    if not d.is_dir():
        return []
    return [p for p in d.iterdir() if not p.name.endswith(PART_SUFFIX)]


def wipe_incognito() -> list[Path]:
    """Delete everything received in incognito. Returns what could not be
    deleted (e.g. a file still open in another program)."""
    d = incognito_dir()
    if not d.is_dir():
        return []
    for p in d.iterdir():
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError:
            pass
    return list(d.iterdir())


def sweep() -> list[Path]:
    """Startup cleanup after a crash or kill: incognito leftovers and
    half-received files."""
    left = wipe_incognito()
    d = files_dir()
    if d.is_dir():
        for p in d.glob("*" + PART_SUFFIX):
            try:
                p.unlink()
            except OSError:
                pass
    return left
