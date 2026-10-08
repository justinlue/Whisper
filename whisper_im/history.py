"""Encrypted append-only message log (non-incognito mode).

File layout:

    header   magic(8) format(1) argon_time(4) argon_mem_kib(4) argon_par(1)
             salt(16) verifier_nonce(12) verifier_ct(30)
    record*  length(4) nonce(12) ciphertext(length - 12)

The verifier is a fixed plaintext encrypted under the PIN-derived key, with
the header prefix as associated data; it lets a wrong PIN be rejected instead
of silently encrypting under a different key. Each record is AES-256-GCM with
its own nonce and its index as associated data, so records cannot be reordered
or dropped from the middle undetected.

The app only ever appends. read_all() exists so the format is provably
recoverable; nothing in the UI calls it yet.
"""
import json
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import argon2_params, derive_key, validate_pin

MAGIC = b"WHSPRLOG"
FORMAT = 1
_CHECK = b"whisper-pin-ok"
_PREFIX = struct.Struct(">8sBIIB16s")
_VERIFIER_LEN = 12 + len(_CHECK) + 16
HEADER_LEN = _PREFIX.size + _VERIFIER_LEN
_LEN = struct.Struct(">I")
_MIN_RECORD = 12 + 16


class WrongPin(Exception):
    pass


def _unlock(path: Path, pin: str) -> bytes:
    with open(path, "rb") as f:
        header = f.read(HEADER_LEN)
    if len(header) < HEADER_LEN:
        raise ValueError(f"{path} is not a Whisper history file")
    prefix, verifier = header[:_PREFIX.size], header[_PREFIX.size:]
    magic, fmt, time_cost, mem, par, salt = _PREFIX.unpack(prefix)
    if magic != MAGIC or fmt != FORMAT:
        raise ValueError(f"{path} is not a Whisper history file")
    key = derive_key(pin, salt, time_cost, mem, par)
    try:
        AESGCM(key).decrypt(verifier[:12], verifier[12:], prefix)
    except InvalidTag:
        raise WrongPin("wrong PIN") from None
    return key


def _records(path: Path):
    """Yield (end_offset, blob) for every complete record."""
    with open(path, "rb") as f:
        f.seek(HEADER_LEN)
        offset = HEADER_LEN
        while True:
            head = f.read(_LEN.size)
            if len(head) < _LEN.size:
                return
            (n,) = _LEN.unpack(head)
            blob = f.read(n)
            if n < _MIN_RECORD or len(blob) < n:
                return
            offset += _LEN.size + n
            yield offset, blob


class HistoryLog:
    def __init__(self, path: Path, key: bytes, count: int):
        self.path = path
        self._aes = AESGCM(key)
        self._count = count
        self._f = open(path, "ab")

    @staticmethod
    def exists(path: Path) -> bool:
        return path.exists() and path.stat().st_size > 0

    @classmethod
    def create(cls, path: Path, pin: str) -> "HistoryLog":
        validate_pin(pin)
        time_cost, mem, par = argon2_params()
        salt = os.urandom(16)
        prefix = _PREFIX.pack(MAGIC, FORMAT, time_cost, mem, par, salt)
        key = derive_key(pin, salt, time_cost, mem, par)
        nonce = os.urandom(12)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(prefix + nonce + AESGCM(key).encrypt(nonce, _CHECK, prefix))
        return cls(path, key, 0)

    @classmethod
    def open(cls, path: Path, pin: str) -> "HistoryLog":
        key = _unlock(path, pin)
        count, end = 0, HEADER_LEN
        for end, _ in _records(path):
            count += 1
        # A crash mid-append leaves a partial record; drop it so later
        # records stay reachable.
        if end < path.stat().st_size:
            with open(path, "r+b") as f:
                f.truncate(end)
        return cls(path, key, count)

    def append(self, record: dict) -> None:
        nonce = os.urandom(12)
        pt = json.dumps(record, ensure_ascii=False).encode()
        blob = nonce + self._aes.encrypt(nonce, pt, struct.pack(">Q", self._count))
        self._f.write(_LEN.pack(len(blob)) + blob)
        self._f.flush()
        os.fsync(self._f.fileno())
        self._count += 1

    def close(self) -> None:
        self._f.close()

    @staticmethod
    def read_all(path: Path, pin: str) -> list[dict]:
        aes = AESGCM(_unlock(path, pin))
        out = []
        for index, (_, blob) in enumerate(_records(path)):
            pt = aes.decrypt(blob[:12], blob[12:], struct.pack(">Q", index))
            out.append(json.loads(pt))
        return out
