import socket
import threading

import pytest

from whisper_im.wire import (KIND_CHUNK, KIND_JSON, IncompatibleVersion, ProtocolError,
                             decode_datagram, encode_datagram, handshake)


class Tap:
    """Socket wrapper that keeps a copy of everything sent."""

    def __init__(self, sock):
        self._sock = sock
        self.sent = b""

    def sendall(self, data):
        self.sent += data
        self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _pair(version_a=1, version_b=1, tap=False):
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    if tap:
        a = Tap(a)
    out = {}

    def responder():
        try:
            out["b"] = handshake(b, initiator=False, version=version_b)
        except Exception as e:
            out["b"] = e

    t = threading.Thread(target=responder)
    t.start()
    try:
        out["a"] = handshake(a, initiator=True, version=version_a)
    except Exception as e:
        out["a"] = e
    t.join()
    return out["a"], out["b"], a


def test_channel_round_trip_both_directions():
    a, b, _ = _pair()
    a.send_json({"t": "msg", "text": "héllo"})
    assert b.recv_json() == {"t": "msg", "text": "héllo"}
    b.send(KIND_CHUNK, b"\x00\x01binary")
    assert a.recv() == (KIND_CHUNK, b"\x00\x01binary")
    a.close()
    b.close()


def test_payload_is_not_readable_on_the_wire():
    a, b, tap = _pair(tap=True)
    a.send_json({"text": "the-secret-phrase"})
    assert b.recv_json() == {"text": "the-secret-phrase"}
    assert b"the-secret-phrase" not in tap.sent
    a.close()
    b.close()


def test_tampered_frame_is_rejected():
    a, b, _ = _pair()
    a.send_json({"text": "x"})
    raw = bytearray(b.sock.recv(4096))
    raw[-1] ^= 1
    left, right = socket.socketpair()
    left.sendall(bytes(raw))
    b.sock = right
    with pytest.raises(ProtocolError):
        b.recv()
    for s in (left, right):
        s.close()
    a.close()


def test_replayed_frame_is_rejected():
    a, b, _ = _pair()
    a.send_json({"text": "x"})
    raw = b.sock.recv(4096)
    left, right = socket.socketpair()
    left.sendall(raw + raw)
    b.sock = right
    assert b.recv_json() == {"text": "x"}
    with pytest.raises(ProtocolError):
        b.recv()
    for s in (left, right):
        s.close()
    a.close()


def test_version_mismatch_is_reported_by_both_ends():
    a, b, _ = _pair(version_a=1, version_b=2)
    assert isinstance(a, IncompatibleVersion)
    assert isinstance(b, IncompatibleVersion)


def test_non_whisper_peer_is_rejected():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.sendall(b"HTTP/1.1 400 Bad Request\r\n" + b" " * 40)
    with pytest.raises(ProtocolError):
        handshake(a, initiator=True)
    a.close()
    b.close()


def test_datagram_round_trip():
    assert decode_datagram(encode_datagram({"t": "hello", "nick": "阿明"})) == {
        "t": "hello", "nick": "阿明"}


def test_datagram_version_mismatch():
    with pytest.raises(IncompatibleVersion):
        decode_datagram(encode_datagram({"t": "hello"}, version=9))


@pytest.mark.parametrize("data", [b"", b"WSP", b"XXXX\x01{}", b"WSPR\x01not json",
                                  b"WSPR\x01[1,2]"])
def test_garbage_datagrams_rejected(data):
    with pytest.raises(ProtocolError):
        decode_datagram(data)


# What a peer gets by cutting an emoji in half: json.loads accepts the escape,
# but the resulting string cannot be encoded, saved or sent on.
HALF_EMOJI = rb'{"t": "msg", "text": "\ud83d"}'


def test_datagram_with_half_an_emoji_is_rejected():
    with pytest.raises(ProtocolError):
        decode_datagram(b"WSPR\x01" + HALF_EMOJI)
    with pytest.raises(ProtocolError):
        decode_datagram(b"WSPR\x01" + rb'{"t": "hello", "nick": {"\udc00": 1}}')


def test_control_frame_with_half_an_emoji_is_rejected():
    a, b, _ = _pair()
    a.send(KIND_JSON, HALF_EMOJI)
    with pytest.raises(ProtocolError):
        b.recv_json()
    a.close()
    b.close()


def test_emoji_sequences_survive_both_transports():
    text = "👍🏽 👨‍👩‍👧 🇨🇳 ❤️"
    assert decode_datagram(encode_datagram({"text": text})) == {"text": text}
    assert decode_datagram(b"WSPR\x01" + rb'{"text": "\ud83d' + rb'\ude00"}') == {"text": "😀"}
    a, b, _ = _pair()
    a.send_json({"text": text})
    assert b.recv_json() == {"text": text}
    a.close()
    b.close()
