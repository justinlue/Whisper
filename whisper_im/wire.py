"""Wire formats: the encrypted TCP channel and plaintext UDP datagrams.

TCP: both ends send  MAGIC(4) VERSION(1) X25519_PUBLIC(32), derive one key per
direction with HKDF, then exchange frames  length(4) AES-256-GCM(kind(1) body)
with a per-direction counter as nonce. Keys are ephemeral per connection.
This defeats passive sniffing; peers are not authenticated, so an active
man-in-the-middle is out of scope.

UDP: MAGIC(4) VERSION(1) JSON. Used for presence and the "Everyone" channel,
and is not encrypted.
"""
import json
import socket
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"WSPR"
VERSION = 1
KIND_JSON = 1
KIND_CHUNK = 2
MAX_FRAME = 1 << 20
_HELLO_LEN = len(MAGIC) + 1 + 32
_LEN = struct.Struct(">I")


class ProtocolError(Exception):
    pass


class IncompatibleVersion(ProtocolError):
    pass


def _loads(data: bytes) -> dict:
    """JSON object from a peer. Raises ValueError if it is not one."""
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    # "\ud83d" is legal JSON for half an emoji, but the resulting str cannot
    # be encoded, so it could be neither saved to history nor sent on.
    json.dumps(obj, ensure_ascii=False).encode()
    return obj


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProtocolError("connection closed")
        buf += chunk
    return bytes(buf)


class Channel:
    def __init__(self, sock: socket.socket, send_key: bytes, recv_key: bytes):
        self.sock = sock
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._sent = 0
        self._received = 0

    def send(self, kind: int, data: bytes = b"") -> None:
        nonce = self._sent.to_bytes(12, "big")
        self._sent += 1
        ct = self._send.encrypt(nonce, bytes([kind]) + data, None)
        self.sock.sendall(_LEN.pack(len(ct)) + ct)

    def recv(self) -> tuple[int, bytes]:
        (n,) = _LEN.unpack(recv_exact(self.sock, _LEN.size))
        if not 17 <= n <= MAX_FRAME:
            raise ProtocolError("bad frame length")
        nonce = self._received.to_bytes(12, "big")
        self._received += 1
        try:
            pt = self._recv.decrypt(nonce, recv_exact(self.sock, n), None)
        except InvalidTag:
            raise ProtocolError("frame failed authentication") from None
        return pt[0], pt[1:]

    def send_json(self, obj: dict) -> None:
        self.send(KIND_JSON, json.dumps(obj, ensure_ascii=False).encode())

    def recv_json(self) -> dict:
        kind, data = self.recv()
        if kind != KIND_JSON:
            raise ProtocolError("expected a control frame")
        try:
            return _loads(data)
        except ValueError:
            raise ProtocolError("malformed control frame") from None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def handshake(sock: socket.socket, initiator: bool, version: int = VERSION) -> Channel:
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()
    sock.sendall(MAGIC + bytes([version]) + pub)
    hello = recv_exact(sock, _HELLO_LEN)
    if hello[:4] != MAGIC:
        raise ProtocolError("not a Whisper peer")
    if hello[4] != version:
        raise IncompatibleVersion(f"peer speaks protocol version {hello[4]}")
    peer_pub = hello[5:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    i_pub, r_pub = (pub, peer_pub) if initiator else (peer_pub, pub)
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=i_pub + r_pub,
               info=b"whisper/1 channel").derive(shared)
    i2r, r2i = okm[:32], okm[32:]
    return Channel(sock, i2r, r2i) if initiator else Channel(sock, r2i, i2r)


def encode_datagram(obj: dict, version: int = VERSION) -> bytes:
    return MAGIC + bytes([version]) + json.dumps(obj, ensure_ascii=False).encode()


def decode_datagram(data: bytes) -> dict:
    if len(data) < 5 or data[:4] != MAGIC:
        raise ProtocolError("not a Whisper datagram")
    if data[4] != VERSION:
        raise IncompatibleVersion(f"peer speaks protocol version {data[4]}")
    try:
        return _loads(data[5:])
    except ValueError:
        raise ProtocolError("malformed datagram") from None
