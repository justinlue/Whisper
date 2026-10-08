"""The network node: presence, the Everyone channel, P2P messages and files.

One TCP connection per message or file transfer; one UDP socket for presence
and broadcasts. Everything runs on daemon threads and reports to the owner
through `on_event(dict)`, which is called from those threads.

Events (all carry "type"):
    peer           addr, nick, incognito, online
    message        addr, nick, text
    broadcast      addr, nick, text
    file_progress  addr, name, done, size
    file_received  addr, nick, name, size, path
    file_failed    addr, name, error
    incompatible   ip
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

from .config import DEFAULT_PORT, MAX_NICK
from .files import PART_SUFFIX, sanitize_filename, unique_path
from .paths import files_dir
from .wire import (KIND_CHUNK, IncompatibleVersion, ProtocolError, decode_datagram,
                   encode_datagram, handshake)

CHUNK = 64 * 1024
MAX_TEXT = 64 * 1024
MAX_BROADCAST = 1000
CONNECT_TIMEOUT = 5
IO_TIMEOUT = 30
OFFER_TIMEOUT = 180


class DeliveryError(Exception):
    pass


class Declined(DeliveryError):
    pass


def local_ipv4() -> list[str]:
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _port_of(msg: dict) -> int:
    port = msg.get("port")
    return port if isinstance(port, int) and 1 <= port <= 65535 else DEFAULT_PORT


def _nick_of(msg: dict, ip: str) -> str:
    nick = msg.get("nick")
    return nick.strip()[:MAX_NICK] if isinstance(nick, str) and nick.strip() else ip


class Node:
    def __init__(self, nick: str, port: int = DEFAULT_PORT, on_event=None, *,
                 bind_host: str = "", broadcast_targets=None,
                 presence_interval: float = 5.0):
        self.nick = nick
        self.port = port
        self.incognito = True
        self.on_event = on_event or (lambda event: None)
        # callable(offer dict) -> bool; may block while the user decides.
        self.offer_handler = None
        # callable() -> directory for the next received file.
        self.receive_dir = files_dir
        self._bind_host = bind_host
        # None: broadcast on every IPv4 interface. A list replaces that
        # (used by tests, where loopback has no broadcast).
        self._targets = broadcast_targets
        self._interval = presence_interval
        self._iid = os.urandom(8).hex()
        self._peers: dict[tuple[str, int], dict] = {}
        self._unicast: set[tuple[str, int]] = set()
        self._blocked: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tcp = self._udp = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Raises OSError if the port is taken (another instance running)."""
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # On Windows SO_REUSEADDR would let a second instance share the
            # port, defeating the single-instance check.
            if sys.platform != "win32":
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind((self._bind_host, self.port))
            tcp.listen(16)
            tcp.settimeout(0.5)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.bind((self._bind_host, self.port))
            udp.settimeout(0.5)
        except OSError:
            tcp.close()
            udp.close()
            raise
        self._tcp, self._udp = tcp, udp
        for target in (self._accept_loop, self._udp_loop, self._presence_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        if self._tcp is None or self._stop.is_set():
            return
        self._announce("bye")
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self._tcp.close()
        self._udp.close()

    # ── public API ───────────────────────────────────────────────────────────

    def announce(self) -> None:
        """Push our nickname / mode to peers now rather than at the next tick."""
        self._announce("hello")

    def add_peer(self, addr: tuple[str, int]) -> None:
        """Also send presence straight to this address, for peers that subnet
        broadcast cannot reach."""
        with self._lock:
            self._unicast.add(addr)
        self._announce("hello")

    def block(self, ip: str) -> None:
        with self._lock:
            self._blocked.add(ip)

    def unblock(self, ip: str) -> None:
        with self._lock:
            self._blocked.discard(ip)

    def send_message(self, addr: tuple[str, int], text: str) -> None:
        if len(text.encode()) > MAX_TEXT:
            raise DeliveryError("message too long")
        ch = self._connect(addr)
        try:
            ch.send_json({"t": "msg", "text": text, **self._ident()})
            if ch.recv_json().get("t") != "ack":
                raise DeliveryError("peer refused the message")
        except (OSError, ProtocolError) as e:
            raise DeliveryError(str(e) or "connection lost") from None
        finally:
            ch.close()

    def send_broadcast(self, text: str) -> None:
        if len(text.encode()) > MAX_BROADCAST:
            raise DeliveryError("broadcast too long")
        if not self._send_all({"t": "msg", "text": text}):
            raise DeliveryError("no network interface to broadcast on")

    def send_file(self, addr: tuple[str, int], path, progress=None) -> None:
        path = Path(path)
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError as e:
            raise DeliveryError(f"cannot read file: {e}") from None
        with f:
            ch = self._connect(addr)
            try:
                ch.send_json({"t": "file", "name": path.name, "size": size,
                              **self._ident()})
                ch.sock.settimeout(OFFER_TIMEOUT)
                if ch.recv_json().get("t") != "accept":
                    raise Declined("declined")
                ch.sock.settimeout(IO_TIMEOUT)
                sent, last = 0, -1
                while chunk := f.read(CHUNK):
                    ch.send(KIND_CHUNK, chunk)
                    sent += len(chunk)
                    pct = sent * 100 // size
                    if progress and pct != last:
                        last = pct
                        progress(sent, size)
                if ch.recv_json().get("t") != "ack":
                    raise DeliveryError("peer did not confirm the file")
            except (OSError, ProtocolError) as e:
                raise DeliveryError(str(e) or "connection lost") from None
            finally:
                ch.close()

    # ── internals ────────────────────────────────────────────────────────────

    def _ident(self) -> dict:
        return {"nick": self.nick, "port": self.port, "incognito": self.incognito}

    def _emit(self, type_: str, **fields) -> None:
        try:
            self.on_event({"type": type_, **fields})
        except Exception:
            pass

    def _is_blocked(self, ip: str) -> bool:
        with self._lock:
            return ip in self._blocked

    def _connect(self, addr):
        try:
            sock = socket.create_connection(addr, timeout=CONNECT_TIMEOUT)
        except OSError:
            raise DeliveryError("peer is unreachable") from None
        try:
            # Still the short timeout: a non-Whisper service on this port may
            # accept the connection and then never answer.
            ch = handshake(sock, initiator=True)
            sock.settimeout(IO_TIMEOUT)
            return ch
        except IncompatibleVersion:
            sock.close()
            raise DeliveryError("peer runs an incompatible version") from None
        except (OSError, ProtocolError):
            sock.close()
            raise DeliveryError("peer is not running Whisper") from None

    def _touch(self, addr, nick: str, incognito) -> None:
        incognito = incognito if isinstance(incognito, bool) else None
        with self._lock:
            old = self._peers.get(addr)
            self._peers[addr] = {"nick": nick, "incognito": incognito,
                                 "seen": time.monotonic()}
        if old is None or old["nick"] != nick or old["incognito"] != incognito:
            self._emit("peer", addr=addr, nick=nick, incognito=incognito, online=True)

    def _drop(self, addr) -> None:
        with self._lock:
            old = self._peers.pop(addr, None)
        if old is not None:
            self._emit("peer", addr=addr, nick=old["nick"],
                       incognito=old["incognito"], online=False)

    # UDP ---------------------------------------------------------------------

    def _send_all(self, body: dict) -> bool:
        """Send a datagram to the whole subnet. True if anything went out."""
        data = encode_datagram({**body, **self._ident(), "iid": self._iid})
        ok = False
        if self._targets is not None:
            for target in self._targets:
                try:
                    self._udp.sendto(data, target)
                    ok = True
                except OSError:
                    pass
            return ok
        # Bind one socket per interface: a bare send to 255.255.255.255 only
        # leaves through the default route on Windows.
        for ip in local_ipv4():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.bind((ip, 0))
                    s.sendto(data, ("255.255.255.255", self.port))
                    ok = True
            except OSError:
                pass
        if not ok:
            try:
                self._udp.sendto(data, ("255.255.255.255", self.port))
                ok = True
            except OSError:
                pass
        return ok

    def _announce(self, kind: str) -> None:
        if self._udp is None or self._stop.is_set():
            return
        self._send_all({"t": kind})
        data = encode_datagram({"t": kind, "u": 1, **self._ident(), "iid": self._iid})
        with self._lock:
            unicast = list(self._unicast)
        for addr in unicast:
            try:
                self._udp.sendto(data, addr)
            except OSError:
                pass

    def _presence_loop(self) -> None:
        while not self._stop.is_set():
            self._announce("hello")
            cutoff = time.monotonic() - 3 * self._interval
            with self._lock:
                stale = [a for a, p in self._peers.items() if p["seen"] < cutoff]
            for addr in stale:
                self._drop(addr)
            self._stop.wait(self._interval)

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, (ip, _) = self._udp.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports an ICMP "port unreachable" from an earlier
                # sendto as an error on the next recvfrom; it is not fatal.
                if self._stop.is_set():
                    return
                continue
            try:
                msg = decode_datagram(data)
            except IncompatibleVersion:
                self._emit("incompatible", ip=ip)
                continue
            except ProtocolError:
                continue
            if msg.get("iid") == self._iid or self._is_blocked(ip):
                continue
            addr = (ip, _port_of(msg))
            nick = _nick_of(msg, ip)
            kind = msg.get("t")
            if kind == "bye":
                self._drop(addr)
                continue
            if kind not in ("hello", "msg"):
                continue
            self._touch(addr, nick, msg.get("incognito"))
            if msg.get("u"):
                with self._lock:
                    self._unicast.add(addr)
            text = msg.get("text")
            if kind == "msg" and isinstance(text, str) and text:
                self._emit("broadcast", addr=addr, nick=nick, text=text)

    # TCP ---------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, (ip, _) = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._is_blocked(ip):
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock, ip), daemon=True).start()

    def _serve(self, sock: socket.socket, ip: str) -> None:
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            try:
                ch = handshake(sock, initiator=False)
                sock.settimeout(IO_TIMEOUT)
            except IncompatibleVersion:
                self._emit("incompatible", ip=ip)
                return
            msg = ch.recv_json()
            addr = (ip, _port_of(msg))
            nick = _nick_of(msg, ip)
            self._touch(addr, nick, msg.get("incognito"))
            if msg.get("t") == "msg":
                text = msg.get("text")
                if isinstance(text, str) and text and len(text.encode()) <= MAX_TEXT:
                    self._emit("message", addr=addr, nick=nick, text=text)
                    ch.send_json({"t": "ack"})
            elif msg.get("t") == "file":
                self._receive_file(ch, addr, nick, msg)
        except (OSError, ProtocolError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _receive_file(self, ch, addr, nick: str, msg: dict) -> None:
        size = msg.get("size")
        if not isinstance(size, int) or size < 0:
            return
        name = sanitize_filename(msg.get("name", ""))
        offer = {"addr": addr, "nick": nick, "name": name, "size": size}
        handler = self.offer_handler
        if handler is None or not handler(offer) or self._is_blocked(addr[0]):
            ch.send_json({"t": "decline"})
            return
        directory = Path(self.receive_dir())
        directory.mkdir(parents=True, exist_ok=True)
        path = unique_path(directory, name)
        part = path.with_name(path.name + PART_SUFFIX)
        try:
            with open(part, "wb") as f:
                ch.send_json({"t": "accept"})
                done, last = 0, -1
                while done < size:
                    kind, data = ch.recv()
                    if kind != KIND_CHUNK or not data or done + len(data) > size:
                        raise ProtocolError("unexpected data during transfer")
                    f.write(data)
                    done += len(data)
                    pct = done * 100 // size
                    if pct != last:
                        last = pct
                        self._emit("file_progress", addr=addr, name=name,
                                   done=done, size=size)
            part.rename(path)
        except (OSError, ProtocolError) as e:
            part.unlink(missing_ok=True)
            self._emit("file_failed", addr=addr, name=name,
                       error=str(e) or "connection lost")
            return
        self._emit("file_received", addr=addr, nick=nick, name=path.name,
                   size=size, path=str(path))
        ch.send_json({"t": "ack"})
