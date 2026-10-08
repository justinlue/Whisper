import pytest


@pytest.fixture(autouse=True)
def whisper_home(tmp_path, monkeypatch):
    """Every test gets its own home and a cheap Argon2 so the suite is fast."""
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setenv("WHISPER_ARGON2_TIME", "1")
    monkeypatch.setenv("WHISPER_ARGON2_MEM", "1024")
    return tmp_path
