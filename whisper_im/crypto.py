"""PIN handling and key derivation (Argon2id, same construction as Vault)."""
import os

from argon2.low_level import hash_secret_raw, Type as Argon2Type

MIN_PIN_LEN = 6


def validate_pin(pin: str) -> None:
    if not pin.isascii() or not pin.isdigit():
        raise ValueError("PIN must contain digits only")
    if len(pin) < MIN_PIN_LEN:
        raise ValueError(f"PIN must be at least {MIN_PIN_LEN} digits")


def argon2_params() -> tuple[int, int, int]:
    """(time_cost, memory_kib, parallelism). Heavier than Vault's because a
    numeric PIN has far less entropy than a password."""
    return (
        int(os.environ.get("WHISPER_ARGON2_TIME", "3")),
        int(os.environ.get("WHISPER_ARGON2_MEM", "262144")),
        int(os.environ.get("WHISPER_ARGON2_PARALLELISM", "4")),
    )


def derive_key(pin: str, salt: bytes, time_cost: int, memory_kib: int,
               parallelism: int) -> bytes:
    return hash_secret_raw(
        pin.encode(), salt,
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=32,
        type=Argon2Type.ID,
    )
