"""Two real nodes talking over loopback."""
import json
import os
import socket
import time

import pytest

from whisper_im.net import MAX_BROADCAST, Declined, DeliveryError, Node
from whisper_im.wire import encode_datagram

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def of_type(events, type_):
    return [e for e in events if e["type"] == type_]


class Pair:
    def __init__(self, tmp_path):
        pa, pb = free_port(), free_port()
        self.ea, self.eb = [], []
        self.a = Node("alice", pa, self.ea.append, bind_host=HOST,
                      broadcast_targets=[(HOST, pb)], presence_interval=0.2)
        self.b = Node("bob", pb, self.eb.append, bind_host=HOST,
                      broadcast_targets=[(HOST, pa)], presence_interval=0.2)
        self.inbox = tmp_path / "inbox"
        self.b.receive_dir = lambda: self.inbox
        self.addr_a, self.addr_b = (HOST, pa), (HOST, pb)


@pytest.fixture
def pair(tmp_path):
    p = Pair(tmp_path)
    p.a.start()
    p.b.start()
    yield p
    p.a.stop()
    p.b.stop()


def test_peers_discover_each_other(pair):
    peer = wait_for(lambda: of_type(pair.ea, "peer"))[0]
    assert peer["addr"] == pair.addr_b
    assert peer["nick"] == "bob" and peer["online"] and peer["incognito"] is True
    wait_for(lambda: of_type(pair.eb, "peer"))


def test_mode_change_is_announced(pair):
    wait_for(lambda: of_type(pair.eb, "peer"))
    pair.a.incognito = False
    pair.a.announce()
    wait_for(lambda: any(e["incognito"] is False for e in of_type(pair.eb, "peer")))


def test_peer_goes_offline_on_stop(pair):
    wait_for(lambda: of_type(pair.ea, "peer"))
    pair.b.stop()
    wait_for(lambda: any(not e["online"] for e in of_type(pair.ea, "peer")))


def test_peer_goes_offline_when_it_falls_silent(pair):
    wait_for(lambda: of_type(pair.ea, "peer"))
    pair.b._stop.set()          # dies without saying goodbye
    wait_for(lambda: any(not e["online"] for e in of_type(pair.ea, "peer")))


def test_p2p_message_is_delivered(pair):
    pair.a.send_message(pair.addr_b, "hello bob\nsecond line 🌙")
    msg = wait_for(lambda: of_type(pair.eb, "message"))[0]
    assert msg["text"] == "hello bob\nsecond line 🌙"
    assert msg["nick"] == "alice" and msg["addr"] == pair.addr_a


def test_broadcast_reaches_everyone_but_the_sender(pair):
    pair.a.send_broadcast("hi all")
    msg = wait_for(lambda: of_type(pair.eb, "broadcast"))[0]
    assert msg["text"] == "hi all" and msg["nick"] == "alice"
    assert not of_type(pair.ea, "broadcast")


def test_oversized_broadcast_is_refused(pair):
    with pytest.raises(DeliveryError):
        pair.a.send_broadcast("x" * (MAX_BROADCAST + 1))


def test_unreachable_peer_fails_visibly(pair):
    with pytest.raises(DeliveryError):
        pair.a.send_message((HOST, free_port()), "anyone there?")


def test_non_whisper_listener_fails_visibly(pair):
    with socket.socket() as other:
        other.bind((HOST, 0))
        other.listen(1)
        with pytest.raises(DeliveryError):
            pair.a.send_message(other.getsockname(), "hello?")


def test_file_transfer_accepted(pair, tmp_path):
    src = tmp_path / "payload.bin"
    data = os.urandom(300_000)
    src.write_bytes(data)
    offers = []
    pair.b.offer_handler = lambda offer: offers.append(offer) or True
    progress = []
    pair.a.send_file(pair.addr_b, src, progress=lambda done, size: progress.append(done))
    got = wait_for(lambda: of_type(pair.eb, "file_received"))[0]
    assert offers[0]["name"] == "payload.bin" and offers[0]["size"] == len(data)
    assert (pair.inbox / "payload.bin").read_bytes() == data
    assert got["path"] == str(pair.inbox / "payload.bin")
    assert progress[-1] == len(data)
    assert not list(pair.inbox.glob("*.part"))
    assert src.read_bytes() == data


def test_second_copy_is_renamed_not_overwritten(pair, tmp_path):
    src = tmp_path / "note.txt"
    pair.b.offer_handler = lambda offer: True
    src.write_text("one")
    pair.a.send_file(pair.addr_b, src)
    src.write_text("two")
    pair.a.send_file(pair.addr_b, src)
    assert (pair.inbox / "note.txt").read_text() == "one"
    assert (pair.inbox / "note (1).txt").read_text() == "two"


def test_empty_file_transfers(pair, tmp_path):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    pair.b.offer_handler = lambda offer: True
    pair.a.send_file(pair.addr_b, src)
    assert (pair.inbox / "empty.txt").read_bytes() == b""


def test_file_declined_writes_nothing(pair, tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"data")
    pair.b.offer_handler = lambda offer: False
    with pytest.raises(Declined):
        pair.a.send_file(pair.addr_b, src)
    assert not pair.inbox.exists()


def test_file_refused_when_nobody_can_answer(pair, tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"data")
    with pytest.raises(Declined):
        pair.a.send_file(pair.addr_b, src)


def test_blocked_ip_is_ignored(pair):
    pair.b.block(HOST)
    with pytest.raises(DeliveryError):
        pair.a.send_message(pair.addr_b, "let me in")
    pair.a.send_broadcast("hello?")
    time.sleep(0.5)
    assert not of_type(pair.eb, "message") and not of_type(pair.eb, "broadcast")
    pair.b.unblock(HOST)
    pair.a.send_message(pair.addr_b, "now?")
    wait_for(lambda: of_type(pair.eb, "message"))


def test_second_instance_on_same_port_fails(pair):
    clone = Node("clone", pair.addr_a[1], bind_host=HOST, broadcast_targets=[])
    with pytest.raises(OSError):
        clone.start()


def test_incompatible_datagram_is_reported(pair):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(encode_datagram({"t": "hello"}, version=9), pair.addr_b)
    wait_for(lambda: of_type(pair.eb, "incompatible"))


def test_emoji_nickname_and_broadcast_arrive_whole(pair):
    pair.a.nick = "alice 🦊"
    pair.a.send_broadcast("lunch? 🍜👍🏽")
    msg = wait_for(lambda: of_type(pair.eb, "broadcast"))[0]
    assert msg["text"] == "lunch? 🍜👍🏽" and msg["nick"] == "alice 🦊"


def test_half_an_emoji_from_a_peer_never_reaches_the_ui(pair):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"WSPR\x01" + rb'{"t": "msg", "text": "\ud83d", "iid": "x"}',
                 pair.addr_b)
        s.sendto(b"WSPR\x01" + rb'{"t": "hello", "nick": "\ude00", "iid": "x"}',
                 pair.addr_b)
        s.sendto(encode_datagram({"t": "msg", "text": "after", "iid": "x"}), pair.addr_b)
    wait_for(lambda: any(e["text"] == "after" for e in of_type(pair.eb, "broadcast")))
    for event in pair.eb:
        json.dumps(event, ensure_ascii=False).encode()
