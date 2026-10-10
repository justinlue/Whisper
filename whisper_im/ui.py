"""tkinter front end. Kept thin: the rules live in session.py and net.py.

Network threads never touch widgets; they put events on a queue that the UI
thread drains from a timer.
"""
import ctypes
import ipaddress
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import config
from .crypto import MIN_PIN_LEN, validate_pin
from .files import incognito_files
from .history import WrongPin
from .net import MAX_BROADCAST, MAX_TEXT, Declined, DeliveryError, Node
from .paths import files_dir, incognito_dir
from .session import Session

EVERYONE = "*"
INCOGNITO_BG = "#3b2a5a"
RECORDING_BG = "#a32020"
OFFER_WAIT = 120
ME_COLOR = "#1a5fb4"
# Dark enough to read as body text on white; no blue (me, links) or red (failures).
PEER_COLORS = ("#1b7a43", "#a8550a", "#813d9c", "#0b7285", "#b0306a", "#6b6b00")


def speaker_color(assigned: dict, who) -> str:
    """Colour for a peer's lines; the first sighting of `who` claims the next one."""
    if who not in assigned:
        assigned[who] = PEER_COLORS[len(assigned) % len(PEER_COLORS)]
    return assigned[who]


def whole_characters(text: str) -> str:
    """Text as Tk hands it over, made safe to encode. Tk 8.6 counts an emoji
    as two characters, so an edit can leave half of one behind; a half becomes
    U+FFFD, and two halves that Tk returned separately are joined."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_addr(text: str, default_port: int) -> tuple[str, int]:
    """'192.168.1.20' or '192.168.1.20:48765' -> (ip, port). Raises ValueError."""
    host, sep, port = text.strip().partition(":")
    ip = str(ipaddress.IPv4Address(host))
    if not sep:
        return ip, default_port
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return ip, port


class Conversation:
    def __init__(self, key, nick: str):
        self.key = key            # EVERYONE or (ip, port)
        self.nick = nick
        self.online = False
        self.incognito = None     # the peer's mode, None if unknown
        self.unread = 0
        self.blocked = False
        self.items: list[dict] = []


class PinDialog(tk.Toplevel):
    """result: the PIN, "reset", or None if cancelled."""

    def __init__(self, parent, creating: bool):
        super().__init__(parent)
        self.result = None
        self.creating = creating
        self.title("Set a PIN" if creating else "Enter PIN")
        self.resizable(False, False)
        self.transient(parent)
        frame = ttk.Frame(self, padding=14)
        frame.pack()
        prompt = (f"Choose a numeric PIN (at least {MIN_PIN_LEN} digits).\n"
                  "It encrypts the messages Whisper saves."
                  if creating else "Enter your PIN to start recording.")
        ttk.Label(frame, text=prompt, justify="left").pack(anchor="w")
        self.pin = ttk.Entry(frame, show="•", width=28)
        self.pin.pack(pady=(8, 0), fill="x")
        self.confirm = None
        if creating:
            ttk.Label(frame, text="Confirm PIN").pack(anchor="w", pady=(8, 0))
            self.confirm = ttk.Entry(frame, show="•", width=28)
            self.confirm.pack(fill="x")
        self.error = ttk.Label(frame, foreground="#b00020")
        self.error.pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        if not creating:
            ttk.Button(buttons, text="Forgot PIN…", command=self._reset).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.pin.focus_set()
        self.grab_set()
        self.wait_window()

    def _ok(self):
        pin = self.pin.get()
        try:
            validate_pin(pin)
        except ValueError as e:
            self.error.config(text=str(e))
            return
        if self.confirm is not None and self.confirm.get() != pin:
            self.error.config(text="PINs do not match")
            return
        self.result = pin
        self.destroy()

    def _reset(self):
        self.result = "reset"
        self.destroy()


class App:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.session = Session()
        self.events: queue.Queue = queue.Queue()
        self.convs = {EVERYONE: Conversation(EVERYONE, "Everyone")}
        self.order = [EVERYONE]
        self.current = EVERYONE
        self.colors: dict = {}    # peer addr -> colour of their lines
        self.node = Node(cfg["nickname"], cfg["port"], self.events.put,
                         bind_host=cfg.get("bind", ""))
        self.node.offer_handler = self._offer_from_thread
        self.node.receive_dir = self.session.receive_dir
        self.node.start()
        self._closed = False
        self._build()
        self._render_mode()
        self._render_list()
        self._render_chat()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(100, self._pump)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        root = self.root
        root.title("Whisper")
        root.geometry("880x560")
        root.minsize(680, 420)

        self.header = tk.Frame(root)
        self.header.pack(fill="x")
        self.mode_label = tk.Label(self.header, fg="white", anchor="w",
                                   font=("Segoe UI", 10, "bold"))
        self.mode_label.pack(side="left", padx=10, pady=7)
        self.incog_var = tk.BooleanVar(value=True)
        self.incog_check = tk.Checkbutton(
            self.header, text="Incognito", variable=self.incog_var,
            command=self._toggle_incognito, fg="white", activeforeground="white",
            selectcolor="#222222", bd=0, highlightthickness=0)
        self.incog_check.pack(side="right", padx=10)
        self.nick_entry = ttk.Entry(self.header, width=26)
        self.nick_entry.insert(0, self.cfg["nickname"])
        self.nick_entry.pack(side="right")
        self.nick_entry.bind("<Return>", self._save_nick)
        self.nick_entry.bind("<FocusOut>", self._save_nick)
        self.name_label = tk.Label(self.header, text="Name", fg="white")
        self.name_label.pack(side="right", padx=6)

        status = ttk.Frame(root, padding=(8, 3))
        status.pack(side="bottom", fill="x")
        self.status = ttk.Label(status, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        ttk.Button(status, text="Open files folder",
                   command=self._open_folder).pack(side="right")
        self.progress = ttk.Progressbar(status, length=160, maximum=100)
        self.progress.pack(side="right", padx=8)

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, padding=6)
        add = ttk.Frame(left)
        add.pack(fill="x")
        self.add_entry = ttk.Entry(add)
        self.add_entry.pack(side="left", fill="x", expand=True)
        self.add_entry.bind("<Return>", lambda e: self._add_peer())
        ttk.Button(add, text="Add IP", width=7, command=self._add_peer).pack(side="left", padx=(4, 0))
        self.listbox = tk.Listbox(left, exportselection=False, activestyle="none",
                                  font=("Segoe UI", 10), width=40)
        self.listbox.pack(fill="both", expand=True, pady=(6, 0))
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        body.add(left, weight=1)

        right = ttk.Frame(body, padding=6)
        top = ttk.Frame(right)
        top.pack(fill="x")
        self.title_label = ttk.Label(top, font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left")
        self.block_btn = ttk.Button(top, text="Block", width=9, command=self._toggle_block)
        self.block_btn.pack(side="right")

        bottom = ttk.Frame(right)
        bottom.pack(side="bottom", fill="x")
        self.counter = ttk.Label(bottom, anchor="w")
        self.counter.pack(side="left")
        ttk.Button(bottom, text="Send", command=self._send).pack(side="right")
        self.file_btn = ttk.Button(bottom, text="Send file…", command=self._send_files)
        self.file_btn.pack(side="right", padx=6)
        self.input = tk.Text(right, height=3, wrap="word", font=("Segoe UI", 10))
        self.input.pack(side="bottom", fill="x", pady=6)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<KeyRelease>", lambda e: self._render_counter())

        chat = ttk.Frame(right)
        chat.pack(fill="both", expand=True, pady=(6, 0))
        scroll = ttk.Scrollbar(chat)
        scroll.pack(side="right", fill="y")
        self.chat = tk.Text(chat, state="disabled", wrap="word", font=("Segoe UI", 10),
                            padx=10, pady=6, spacing2=2, yscrollcommand=scroll.set)
        self.chat.pack(fill="both", expand=True)
        scroll.config(command=self.chat.yview)
        # "gap" sits on the first character of an item, so the space opens
        # between messages and not between the lines of one message.
        self.chat.tag_config("gap", spacing1=9)
        self.chat.tag_config("meta", foreground="#777777")
        for color in (ME_COLOR, *PEER_COLORS):
            self.chat.tag_config(color, foreground=color)
        self.chat.tag_config("name", font=("Segoe UI", 10, "bold"))
        self.chat.tag_config("fail", foreground="#b00020")
        self.chat.tag_config("link", foreground="#1a5fb4", underline=True)
        body.add(right, weight=3)

    # ── rendering ────────────────────────────────────────────────────────────

    def _render_mode(self):
        incognito = self.session.incognito
        bg = INCOGNITO_BG if incognito else RECORDING_BG
        text = ("INCOGNITO — nothing is saved; received files are deleted on close"
                if incognito else
                "RECORDING — messages are saved encrypted; received files are kept")
        for w in (self.header, self.mode_label, self.name_label):
            w.config(bg=bg)
        self.incog_check.config(bg=bg, activebackground=bg)
        self.mode_label.config(text=text)

    def _label(self, conv: Conversation) -> str:
        if conv.key == EVERYONE:
            text = "Everyone on this subnet"
        else:
            ip, port = conv.key
            where = ip if port == self.cfg["port"] else f"{ip}:{port}"
            dot = "●" if conv.online else "○"
            text = f"{dot} {conv.nick}  {where}"
            if conv.online and conv.incognito is not None:
                text += "  [incognito]" if conv.incognito else "  [recording]"
            if conv.blocked:
                text += "  [blocked]"
        if conv.unread:
            text += f"  ({conv.unread})"
        return text

    def _render_list(self):
        self.listbox.delete(0, "end")
        for key in self.order:
            self.listbox.insert("end", self._label(self.convs[key]))
        self.listbox.selection_set(self.order.index(self.current))

    def _render_chat(self):
        conv = self.convs[self.current]
        if conv.key == EVERYONE:
            self.title_label.config(text="Everyone (not encrypted on the wire)")
            self.block_btn.state(["disabled"])
            self.file_btn.state(["disabled"])
        else:
            self.title_label.config(text=f"{conv.nick}  —  {conv.key[0]}")
            self.block_btn.state(["!disabled"])
            self.block_btn.config(text="Unblock" if conv.blocked else "Block")
            self.file_btn.state(["!disabled"])
        chat = self.chat
        chat.config(state="normal")
        chat.delete("1.0", "end")
        for tag in chat.tag_names():
            if tag.startswith("retry-"):
                chat.tag_delete(tag)
        for index, item in enumerate(conv.items):
            self._render_item(conv, index, item)
        chat.config(state="disabled")
        chat.see("end")
        self._render_counter()

    def _render_item(self, conv: Conversation, index: int, item: dict):
        chat = self.chat
        stamp = time.strftime("%H:%M", time.localtime(item["ts"]))
        color = ME_COLOR if item["out"] else speaker_color(self.colors, item["who"])
        chat.insert("end", f"[{stamp}] ", ("meta", "gap"))
        chat.insert("end", ("me" if item["out"] else item["nick"]) + ": ", (color, "name"))
        if item["kind"] == "msg":
            chat.insert("end", item["text"], color)
        else:
            chat.insert("end", f"[file] {item['name']} ({human_size(item['size'])})", color)
            if not item["out"]:
                note = " — deleted when Whisper closes" if item.get("incognito") else ""
                chat.insert("end", f"\n        saved to {item['path']}{note}", "meta")
        status = item.get("status")
        if status == "sending":
            chat.insert("end", "  sending…", "meta")
        elif status == "declined":
            chat.insert("end", "  ✗ declined", "fail")
        elif status == "failed":
            chat.insert("end", f"  ✗ not delivered ({item.get('error', 'error')})  ", "fail")
            tag = f"retry-{index}"
            chat.insert("end", "retry", ("link", tag))
            chat.tag_bind(tag, "<Button-1>", lambda e, c=conv, i=item: self._retry(c, i))
            chat.tag_bind(tag, "<Enter>", lambda e: chat.config(cursor="hand2"))
            chat.tag_bind(tag, "<Leave>", lambda e: chat.config(cursor=""))
        elif status == "ok" and item["kind"] == "file" and item["out"]:
            chat.insert("end", "  ✓ sent", "meta")
        chat.insert("end", "\n")

    def _render_counter(self):
        used = len(whole_characters(self.input.get("1.0", "end-1c")).encode())
        limit = MAX_BROADCAST if self.current == EVERYONE else MAX_TEXT
        if self.current == EVERYONE or used > limit:
            self.counter.config(text=f"{used} / {limit} bytes",
                                foreground="#b00020" if used > limit else "")
        else:
            self.counter.config(text="Enter sends · Shift+Enter new line", foreground="")

    def _set_status(self, text: str, percent: float | None = None):
        self.status.config(text=text)
        self.progress["value"] = percent or 0

    # ── conversations ────────────────────────────────────────────────────────

    def _conv(self, addr, nick: str | None = None) -> Conversation:
        conv = self.convs.get(addr)
        if conv is None:
            conv = self.convs[addr] = Conversation(addr, nick or addr[0])
            self.order.append(addr)
        elif nick:
            conv.nick = nick
        return conv

    def _add_item(self, conv: Conversation, **item) -> dict:
        item.setdefault("ts", time.time())
        conv.items.append(item)
        return item

    def _incoming(self, conv: Conversation):
        focused = self.root.focus_displayof() is not None
        if conv.key != self.current or not focused:
            if conv.key != self.current:
                conv.unread += 1
            self._flash()
        self._render_list()
        if conv.key == self.current:
            self._render_chat()

    def _flash(self):
        if self.root.focus_displayof() is not None:
            return
        try:
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.c_void_p
            hwnd = user32.GetParent(ctypes.c_void_p(self.root.winfo_id()))
            # FLASHW_TRAY | FLASHW_TIMERNOFG: flash until the window is focused
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 2 | 12, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            self.root.bell()

    def _on_select(self, event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        self.current = self.order[selection[0]]
        self.convs[self.current].unread = 0
        self._render_list()
        self._render_chat()

    def _add_peer(self):
        text = self.add_entry.get()
        if not text.strip():
            return
        try:
            addr = parse_addr(text, self.cfg["port"])
        except ValueError:
            self._set_status(f"'{text.strip()}' is not a valid IPv4 address")
            return
        self.add_entry.delete(0, "end")
        self._conv(addr)
        self.node.add_peer(addr)
        self.current = addr
        self._render_list()
        self._render_chat()
        self.input.focus_set()

    def _toggle_block(self):
        conv = self.convs[self.current]
        if conv.key == EVERYONE:
            return
        conv.blocked = not conv.blocked
        (self.node.block if conv.blocked else self.node.unblock)(conv.key[0])
        self._render_list()
        self._render_chat()

    # ── mode ─────────────────────────────────────────────────────────────────

    def _toggle_incognito(self):
        if self.incog_var.get():
            self.session.enable_incognito()
        elif not self._start_recording():
            self.incog_var.set(True)
        self.node.incognito = self.session.incognito
        self.node.announce()
        self._render_mode()

    def _start_recording(self) -> bool:
        if self.session.history is not None:
            self.session.disable_incognito()
            return True
        while True:
            result = PinDialog(self.root, creating=not self.session.has_history()).result
            if result is None:
                return False
            if result == "reset":
                if messagebox.askyesno(
                        "Forgot PIN",
                        "Without the PIN the saved messages cannot be read.\n\n"
                        "Permanently delete all saved message history so you can "
                        "set a new PIN?", icon="warning", default="no"):
                    self.session.reset_history()
                continue
            self.root.config(cursor="watch")
            self.root.update_idletasks()
            try:
                self.session.disable_incognito(result)
                return True
            except WrongPin:
                messagebox.showerror("Wrong PIN", "That PIN is not correct.")
            except (ValueError, OSError) as e:
                messagebox.showerror("Cannot open history", str(e))
                return False
            finally:
                self.root.config(cursor="")

    def _save_nick(self, event=None):
        nick = whole_characters(self.nick_entry.get()).strip()[:config.MAX_NICK]
        if not nick or nick == self.cfg["nickname"]:
            return
        self.cfg["nickname"] = self.node.nick = nick
        try:
            config.save(self.cfg)
        except OSError as e:
            self._set_status(f"Could not save the name: {e}")
        self.node.announce()

    # ── sending ──────────────────────────────────────────────────────────────

    def _on_return(self, event):
        if event.state & 0x1:          # Shift: let Tk insert the newline
            return None
        self._send()
        return "break"

    def _send(self):
        text = whole_characters(self.input.get("1.0", "end-1c")).strip()
        if not text:
            return
        conv = self.convs[self.current]
        limit = MAX_BROADCAST if conv.key == EVERYONE else MAX_TEXT
        if len(text.encode()) > limit:
            self._set_status(f"Message is over the {limit}-byte limit")
            return
        self.input.delete("1.0", "end")
        item = self._add_item(conv, kind="msg", out=True, nick="me", text=text)
        self._dispatch(conv, item)

    def _retry(self, conv: Conversation, item: dict):
        if item.get("status") == "failed":
            self._dispatch(conv, item)

    def _dispatch(self, conv: Conversation, item: dict):
        item["status"] = "sending"
        self._render_chat()

        def work():
            error = None
            try:
                if conv.key == EVERYONE:
                    self.node.send_broadcast(item["text"])
                else:
                    self.node.send_message(conv.key, item["text"])
            except (DeliveryError, OSError) as e:
                error = str(e) or "error"
            self.events.put({"type": "sent", "conv": conv, "item": item, "error": error})

        threading.Thread(target=work, daemon=True).start()

    def _send_files(self):
        conv = self.convs[self.current]
        if conv.key == EVERYONE:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="Send file")
        if not paths:
            return
        jobs = []
        for p in paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            jobs.append((p, self._add_item(conv, kind="file", out=True, nick="me",
                                           name=os.path.basename(p), size=size,
                                           status="sending")))
        self._render_chat()

        def work():
            for path, item in jobs:
                def progress(done, size, name=item["name"]):
                    self.events.put({"type": "send_progress", "name": name,
                                     "done": done, "size": size})
                error, status = None, "ok"
                try:
                    self.node.send_file(conv.key, path, progress)
                except Declined:
                    status = "declined"
                except (DeliveryError, OSError) as e:
                    status, error = "failed", str(e) or "error"
                self.events.put({"type": "file_sent", "conv": conv, "item": item,
                                 "status": status, "error": error})

        threading.Thread(target=work, daemon=True).start()

    # ── events from the network threads ──────────────────────────────────────

    def _offer_from_thread(self, offer: dict) -> bool:
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.events.put({"type": "offer", "offer": offer, "reply": reply,
                         "expires": time.monotonic() + OFFER_WAIT})
        try:
            return reply.get(timeout=OFFER_WAIT)
        except queue.Empty:
            return False

    def _pump(self):
        if self._closed:
            return
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(100, self._pump)

    def _handle(self, ev: dict):
        kind = ev["type"]
        if kind == "peer":
            conv = self._conv(ev["addr"], ev["nick"])
            conv.online = ev["online"]
            conv.incognito = ev["incognito"]
            self._render_list()
            if conv.key == self.current:
                self._render_chat()
        elif kind in ("message", "broadcast"):
            sender = self._conv(ev["addr"], ev["nick"])
            conv = sender if kind == "message" else self.convs[EVERYONE]
            self._add_item(conv, kind="msg", out=False, nick=ev["nick"], who=ev["addr"],
                           text=ev["text"])
            self.session.record(kind=kind, dir="in", peer=ev["addr"][0],
                                nick=ev["nick"], text=ev["text"])
            self._incoming(conv)
        elif kind == "sent":
            conv, item = ev["conv"], ev["item"]
            item["status"], item["error"] = ("failed", ev["error"]) if ev["error"] else ("ok", None)
            if not ev["error"]:
                self.session.record(
                    kind="broadcast" if conv.key == EVERYONE else "message", dir="out",
                    peer=None if conv.key == EVERYONE else conv.key[0], text=item["text"])
            if conv.key == self.current:
                self._render_chat()
        elif kind == "offer":
            self._handle_offer(ev)
        elif kind in ("file_progress", "send_progress"):
            verb = "Receiving" if kind == "file_progress" else "Sending"
            pct = ev["done"] * 100 / ev["size"] if ev["size"] else 100
            self._set_status(f"{verb} {ev['name']} — {pct:.0f}%", pct)
        elif kind == "file_received":
            conv = self._conv(ev["addr"], ev["nick"])
            incognito = Path(ev["path"]).parent == incognito_dir()
            self._add_item(conv, kind="file", out=False, nick=ev["nick"], who=ev["addr"],
                           name=ev["name"], size=ev["size"], path=ev["path"],
                           incognito=incognito)
            self.session.record(kind="file", dir="in", peer=ev["addr"][0], nick=ev["nick"],
                                name=ev["name"], size=ev["size"], path=ev["path"])
            self._set_status(f"Received {ev['name']}")
            self._incoming(conv)
        elif kind == "file_failed":
            self._set_status(f"Receiving {ev['name']} failed: {ev['error']}")
        elif kind == "file_sent":
            conv, item = ev["conv"], ev["item"]
            item["status"], item["error"] = ev["status"], ev["error"]
            if ev["status"] == "ok":
                self.session.record(kind="file", dir="out", peer=conv.key[0],
                                    name=item["name"], size=item["size"])
            self._set_status({"ok": "Sent ", "declined": "Declined: ",
                              "failed": "Failed: "}[ev["status"]] + item["name"])
            if conv.key == self.current:
                self._render_chat()
        elif kind == "incompatible":
            self._set_status(f"{ev['ip']} runs an incompatible version of Whisper")

    def _handle_offer(self, ev: dict):
        offer = ev["offer"]
        if time.monotonic() > ev["expires"]:
            return
        self._flash()
        fate = ("It will be deleted when you close Whisper (incognito)."
                if self.session.incognito else f"It will be kept in {files_dir()}.")
        accept = messagebox.askyesno(
            "Incoming file",
            f"{offer['nick']} ({offer['addr'][0]}) wants to send you:\n\n"
            f"{offer['name']}  ({human_size(offer['size'])})\n\n{fate}\n\nAccept?",
            parent=self.root)
        try:
            ev["reply"].put_nowait(accept)
        except queue.Full:
            pass

    # ── misc ─────────────────────────────────────────────────────────────────

    def _open_folder(self):
        folder = self.session.receive_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)
        except (AttributeError, OSError):
            self._set_status(str(folder))

    def quit(self):
        doomed = incognito_files()
        if doomed and not messagebox.askyesno(
                "Quit Whisper",
                f"{len(doomed)} file(s) received in incognito mode will be "
                f"permanently deleted from:\n{incognito_dir()}\n\n"
                "Copy out anything you want to keep first.\n\nQuit now?",
                icon="warning", default="no", parent=self.root):
            return
        self._closed = True
        self.node.stop()
        left = self.session.close()
        if left:
            messagebox.showwarning(
                "Whisper",
                "These files are open in another program and could not be deleted. "
                "They will be removed the next time Whisper starts:\n\n"
                + "\n".join(p.name for p in left), parent=self.root)
        self.root.destroy()
