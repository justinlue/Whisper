import pytest
from cryptography.exceptions import InvalidTag

from whisper_im.crypto import validate_pin
from whisper_im.history import HEADER_LEN, HistoryLog, WrongPin

PIN = "246810"


@pytest.mark.parametrize("pin", ["", "12345", "12345a", "abcdef", "12 456", "１２３４５６"])
def test_bad_pins_rejected(pin):
    with pytest.raises(ValueError):
        validate_pin(pin)


def test_good_pins_accepted():
    validate_pin("123456")
    validate_pin("1" * 20)


def test_create_refuses_bad_pin(tmp_path):
    with pytest.raises(ValueError):
        HistoryLog.create(tmp_path / "h", "123")
    assert not (tmp_path / "h").exists()


def test_records_round_trip(tmp_path):
    path = tmp_path / "data" / "h"
    log = HistoryLog.create(path, PIN)
    records = [{"kind": "msg", "text": "hello"}, {"kind": "msg", "text": "你好 🌙"}]
    for r in records:
        log.append(r)
    log.close()
    assert HistoryLog.read_all(path, PIN) == records


def test_plaintext_never_on_disk(tmp_path):
    path = tmp_path / "h"
    log = HistoryLog.create(path, PIN)
    log.append({"text": "the-secret-phrase", "peer": "192.168.1.77"})
    log.close()
    raw = path.read_bytes()
    assert b"the-secret-phrase" not in raw
    assert b"192.168.1.77" not in raw
    assert PIN.encode() not in raw


def test_wrong_pin_rejected(tmp_path):
    path = tmp_path / "h"
    HistoryLog.create(path, PIN).close()
    with pytest.raises(WrongPin):
        HistoryLog.open(path, "000000")
    with pytest.raises(WrongPin):
        HistoryLog.read_all(path, "000000")


def test_reopen_appends_after_existing_records(tmp_path):
    path = tmp_path / "h"
    log = HistoryLog.create(path, PIN)
    log.append({"n": 1})
    log.close()
    log = HistoryLog.open(path, PIN)
    log.append({"n": 2})
    log.close()
    assert HistoryLog.read_all(path, PIN) == [{"n": 1}, {"n": 2}]


def test_partial_trailing_record_is_dropped_on_open(tmp_path):
    path = tmp_path / "h"
    log = HistoryLog.create(path, PIN)
    log.append({"n": 1})
    log.close()
    with open(path, "ab") as f:
        f.write(b"\x00\x00\x00\x40half-a-record")
    log = HistoryLog.open(path, PIN)
    log.append({"n": 2})
    log.close()
    assert HistoryLog.read_all(path, PIN) == [{"n": 1}, {"n": 2}]


def test_reordered_records_are_detected(tmp_path):
    path = tmp_path / "h"
    log = HistoryLog.create(path, PIN)
    log.append({"n": 1})
    log.append({"n": 2})
    log.close()
    raw = path.read_bytes()
    body = raw[HEADER_LEN:]
    half = len(body) // 2
    path.write_bytes(raw[:HEADER_LEN] + body[half:] + body[:half])
    with pytest.raises(InvalidTag):
        HistoryLog.read_all(path, PIN)


def test_not_a_history_file(tmp_path):
    path = tmp_path / "h"
    path.write_bytes(b"x" * 200)
    with pytest.raises(ValueError):
        HistoryLog.open(path, PIN)
