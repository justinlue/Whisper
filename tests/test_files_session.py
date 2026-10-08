import pytest

from whisper_im.files import (incognito_files, sanitize_filename, sweep,
                              unique_path, wipe_incognito)
from whisper_im.history import HistoryLog, WrongPin
from whisper_im.paths import files_dir, history_path, incognito_dir
from whisper_im.session import Session

PIN = "246810"


@pytest.mark.parametrize("raw, safe", [
    ("report.pdf", "report.pdf"),
    ("../../Windows/System32/evil.dll", "evil.dll"),
    ("..\\..\\evil.bat", "evil.bat"),
    ("C:\\Users\\me\\secret.txt", "secret.txt"),
    ("/etc/passwd", "passwd"),
    ("..", "file"),
    ("", "file"),
    ("   ", "file"),
    ("a<b>c:d\"e|f?g*.txt", "abcdefg.txt"),
    ("nul", "_nul"),
    ("COM1.txt", "_COM1.txt"),
    ("trailing.dot. ", "trailing.dot"),
    ("line\nbreak\x00.txt", "linebreak.txt"),
    ("movie.part", "movie.part_"),
    ("报告.docx", "报告.docx"),
])
def test_sanitize_filename(raw, safe):
    assert sanitize_filename(raw) == safe


def test_sanitize_caps_length_but_keeps_extension():
    name = sanitize_filename("x" * 500 + ".tar")
    assert len(name) == 200 and name.endswith(".tar")


def test_unique_path_avoids_files_and_transfers_in_progress(tmp_path):
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"
    (tmp_path / "a.txt").write_text("x")
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (1).txt"
    (tmp_path / "a (1).txt.part").write_text("x")
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"


def _populate():
    incognito_dir().mkdir(parents=True)
    (incognito_dir() / "secret.txt").write_text("x")
    (incognito_dir() / "half.bin.part").write_text("x")
    (files_dir() / "kept.txt").write_text("x")
    (files_dir() / "half.bin.part").write_text("x")


def test_wipe_removes_only_incognito_files():
    _populate()
    assert [p.name for p in incognito_files()] == ["secret.txt"]
    assert wipe_incognito() == []
    assert list(incognito_dir().iterdir()) == []
    assert (files_dir() / "kept.txt").exists()


def test_startup_sweep_removes_crash_leftovers():
    _populate()
    sweep()
    assert list(incognito_dir().iterdir()) == []
    assert sorted(p.name for p in files_dir().iterdir() if p.is_file()) == ["kept.txt"]


def test_sweep_on_fresh_home_is_a_noop():
    assert sweep() == []


def test_session_starts_incognito_and_writes_nothing(whisper_home):
    s = Session()
    assert s.incognito
    s.record(kind="msg", text="hi")
    assert s.receive_dir() == incognito_dir()
    s.close()
    assert not history_path().exists()
    assert not (whisper_home / "data").exists()


def test_recording_starts_at_the_switch_and_stops_on_return():
    s = Session()
    s.record(kind="msg", text="before")
    s.disable_incognito(PIN)
    assert s.receive_dir() == files_dir()
    s.record(kind="msg", text="during")
    s.enable_incognito()
    s.record(kind="msg", text="after")
    s.disable_incognito()          # same session: no PIN needed again
    s.record(kind="msg", text="again")
    s.close()
    texts = [r["text"] for r in HistoryLog.read_all(history_path(), PIN)]
    assert texts == ["during", "again"]


def test_wrong_pin_keeps_session_incognito():
    s = Session()
    s.disable_incognito(PIN)
    s.close()
    s = Session()
    with pytest.raises(WrongPin):
        s.disable_incognito("000000")
    assert s.incognito and s.history is None


def test_history_survives_relaunch_and_new_launch_is_incognito_again():
    s = Session()
    s.disable_incognito(PIN)
    s.record(kind="msg", text="one")
    s.close()
    s = Session()
    assert s.incognito and s.has_history()
    s.disable_incognito(PIN)
    s.record(kind="msg", text="two")
    s.close()
    assert [r["text"] for r in HistoryLog.read_all(history_path(), PIN)] == ["one", "two"]


def test_reset_discards_history_and_allows_a_new_pin():
    s = Session()
    s.disable_incognito(PIN)
    s.record(kind="msg", text="old")
    s.reset_history()
    assert s.incognito and not s.has_history()
    s.disable_incognito("135790")
    s.record(kind="msg", text="new")
    s.close()
    assert [r["text"] for r in HistoryLog.read_all(history_path(), "135790")] == ["new"]


def test_close_deletes_incognito_files_and_keeps_recorded_ones():
    _populate()
    s = Session()
    s.close()
    assert not (incognito_dir() / "secret.txt").exists()
    assert (files_dir() / "kept.txt").exists()
