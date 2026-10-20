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
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import config
from .crypto import MIN_PIN_LEN, validate_pin
from .files import incognito_files
from .groups import Groups
from .history import WrongPin
from .images import dib, from_clipboard, is_image_file, thumbnail
from .markup import MENTION, MENTION_ME, emoji_pieces, mentions, mentions_me
from .net import MAX_BROADCAST, MAX_GROUP_PICTURE, MAX_TEXT, Declined, DeliveryError, Node
from .paths import files_dir, incognito_dir
from .session import Session

EVERYONE = "*"
INCOGNITO_BG = "#3b2a5a"
RECORDING_BG = "#a32020"
OFFER_WAIT = 120
ME_COLOR = "#1a5fb4"
# Dark enough to read as body text on white; no blue (me, links) or red (failures).
PEER_COLORS = ("#1b7a43", "#a8550a", "#813d9c", "#0b7285", "#b0306a", "#6b6b00")
MENTION_BG = "#fff1b8"
MENTION_ME_BG = "#ffd43b"
# Single code points only: a flag, family or skin-toned emoji is drawn as its
# separate parts.
EMOJI = tuple(
    "😀😁😂🤣😊😉😍😘😎🤔"
    "😅😇🙂🙃😋😜🤗🤩🥳😴"
    "😐😑😶🙄😏😬😮😲😳🥺"
    "😢😭😤😠😡🤯😱😨🤒🤮"
    "👍👎👌👏🙏💪👋🤝🤞🤙"
    "💯🔥✨🎉🎂🎁💡📌📎🔒"
    "❤💔💕💖💘💝🎵🎶💰💤"
    "☕🍺🍜🍕🍰🌙🌞🌧⭐⚡"
    "✅❌❓❗⏰📅📞💻🚀👀")
EMOJI_COLUMNS = 10
PICTURE_SIDE = 240        # longest side of a picture drawn in the chat
CF_DIB = 8                # Win32 clipboard format: a BMP without its file header
GMEM_MOVEABLE = 2         # GlobalAlloc flag the clipboard requires


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


def split_by_size(sources, limit: int):
    """(sources of at most `limit` bytes, names of the larger ones), for
    sources as from_clipboard gives them. A file that cannot be measured is
    kept, for sending to skip."""
    fit, too_big = [], []
    for source in sources:
        if isinstance(source, tuple):
            name, size = source[0], len(source[1])
        else:
            try:
                name, size = os.path.basename(source), os.path.getsize(source)
            except OSError:
                fit.append(source)
                continue
        if size > limit:
            too_big.append(name)
        else:
            fit.append(source)
    return fit, too_big


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


def clipboard_dib(data: bytes) -> bool:
    """Put a picture on the Windows clipboard, given as CF_DIB bytes.
    False if it could not be done, including on other systems."""
    # Own copies of the DLLs, so the argtypes set here do not leak into
    # other users of ctypes.windll.
    try:
        kernel32, user32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    except (AttributeError, OSError):
        return False
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        # On success the clipboard owns the memory.
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


class EmojiImages:
    """Colour pictures of emoji. Tk 8.6 draws emoji in one colour, so they are
    rendered with Pillow from the system emoji font and put in the widgets as
    images. Without Pillow or the font, `get` returns None and the caller
    shows the character as text."""

    FONT = "seguiemj.ttf"
    ABSENT = "􏿿"       # in no font

    def __init__(self, size: int):
        self.size = size          # side of the square picture, in pixels
        self._images: dict = {}
        self._font = self._draw = None
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            self._font = ImageFont.truetype(self.FONT, size * 2)
            self._draw = (Image, ImageDraw, ImageTk)
        except (ImportError, OSError):
            pass

    def get(self, emoji: str):
        if self._font is None:
            return None
        if emoji not in self._images:
            self._images[emoji] = self._render(emoji)
        return self._images[emoji]

    def _render(self, emoji: str):
        Image, ImageDraw, ImageTk = self._draw
        try:
            left, top, right, bottom = self._font.getbbox(emoji)
            glyph = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
            ImageDraw.Draw(glyph).text((-left, -top), emoji, font=self._font,
                                       embedded_color=True)
            ink = glyph.getbbox()
            # A character the font lacks comes out blank or as the same box
            # as any other it lacks.
            if ink is None or (bytes(self._font.getmask(emoji))
                               == bytes(self._font.getmask(self.ABSENT))):
                return None
            # Drawn large and cut to its ink, so it fills the square when shrunk.
            glyph = glyph.crop(ink)
            glyph.thumbnail((self.size, self.size))
            image = Image.new("RGBA", (self.size, self.size))
            image.paste(glyph, ((self.size - glyph.width) // 2,
                                (self.size - glyph.height) // 2))
            return ImageTk.PhotoImage(image)
        except (OSError, ValueError, tk.TclError):
            return None


class Conversation:
    def __init__(self, key, nick: str, group=None):
        self.key = key            # EVERYONE, (ip, port), or a group's id
        self.group = group        # the Group, for a group channel
        self.nick = nick
        self.online = False
        self.incognito = None     # the peer's mode, None if unknown
        self.unread = 0
        self.mentioned = False    # an unread message mentions me
        self.blocked = False
        self.items: list[dict] = []

    @property
    def is_peer(self) -> bool:
        return isinstance(self.key, tuple)


class GroupDialog(tk.Toplevel):
    """result: the Group that `create(name, members)` returned, or None if
    cancelled. `create` raises ValueError to turn a choice down."""

    def __init__(self, parent, peers, create):
        super().__init__(parent)
        self.result = None
        self.create = create
        self.addrs = [addr for addr, _ in peers]
        self.title("New group")
        self.resizable(False, False)
        self.transient(parent)
        frame = ttk.Frame(self, padding=14)
        frame.pack()
        ttk.Label(frame, text="Group name (cannot be changed later)").pack(anchor="w")
        self.name = ttk.Entry(frame, width=40)
        self.name.pack(fill="x", pady=(4, 10))
        ttk.Label(frame, text="Members (cannot be changed later)").pack(anchor="w")
        self.members = tk.Listbox(frame, selectmode="multiple", exportselection=False,
                                  activestyle="none", font=("Segoe UI", 10),
                                  height=min(12, max(4, len(peers))))
        for _, label in peers:
            self.members.insert("end", label)
        self.members.pack(fill="x", pady=(4, 0))
        self.error = ttk.Label(frame, foreground="#b00020")
        self.error.pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Create", command=self._ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.name.focus_set()
        self.grab_set()
        self.wait_window()

    def _ok(self):
        chosen = [self.addrs[i] for i in self.members.curselection()]
        try:
            self.result = self.create(whole_characters(self.name.get()), chosen)
        except ValueError as e:
            self.error.config(text=str(e))
            return
        self.destroy()


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
        self.groups = Groups()
        self.picker_emoji = EmojiImages(24)
        self.emoji = EmojiImages(tkfont.Font(font=("Segoe UI", 10)).metrics("linespace"))
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
        ttk.Button(left, text="New group…",
                   command=self._new_group).pack(side="bottom", fill="x", pady=(6, 0))
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
        ttk.Button(bottom, text="Emoji", width=7,
                   command=self._toggle_emoji).pack(side="right")
        self.emoji_win = None
        self.input = tk.Text(right, height=3, wrap="word", font=("Segoe UI", 10))
        self.input.pack(side="bottom", fill="x", pady=6)
        self.input.bind("<Return>", self._on_return)
        # Picking an emoji hands focus back here, so Escape must work from here too.
        self.input.bind("<Escape>", lambda e: self.emoji_win and self._close_emoji())
        self.input.bind("<KeyRelease>", lambda e: self._render_counter())
        self.input.bind("<<Paste>>", self._on_paste)

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
        self.chat.tag_config(MENTION, font=("Segoe UI", 10, "bold"),
                             background=MENTION_BG)
        self.chat.tag_config(MENTION_ME, font=("Segoe UI", 10, "bold"),
                             background=MENTION_ME_BG)
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
        elif conv.group:
            text = f"# {conv.nick}"
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
            text += f"  (@ {conv.unread})" if conv.mentioned else f"  ({conv.unread})"
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
        elif conv.group:
            who = ", ".join(self._name(m) for m in conv.group.members)
            self.title_label.config(text=f"# {conv.nick}  —  you, {who}")
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
            if tag.startswith(("retry-", "picture-")):
                chat.tag_delete(tag)
        names = self._mentionable(conv)
        for index, item in enumerate(conv.items):
            self._render_item(conv, index, item, names)
        chat.config(state="disabled")
        chat.see("end")
        self._render_counter()

    def _mentionable(self, conv: Conversation) -> list[str]:
        """The names an @ picks out in this conversation."""
        if conv.group:
            return [self._name(m) for m in conv.group.members]
        if conv.key == EVERYONE:
            return [c.nick for c in self.convs.values() if c.is_peer]
        return []

    def _insert(self, text: str, tags, names=None):
        """Message text into the chat: emoji as colour pictures and, where
        `names` are given, @mentions of them (and of me) highlighted."""
        chat = self.chat
        cut = ([(text, None)] if names is None
               else mentions(text, names, self.cfg["nickname"]))
        for piece, kind in cut:
            piece_tags = (*tags, kind) if kind else tags
            for part, emoji in emoji_pieces(piece):
                image = self.emoji.get(emoji) if emoji else None
                if image is None:
                    chat.insert("end", part, piece_tags)
                else:
                    at = chat.index("end-1c")
                    chat.image_create(at, image=image, align="center", padx=1)
                    for tag in piece_tags:    # a mention keeps its background
                        chat.tag_add(tag, at)

    def _render_item(self, conv: Conversation, index: int, item: dict, names=None):
        chat = self.chat
        stamp = time.strftime("%H:%M", time.localtime(item["ts"]))
        chat.insert("end", f"[{stamp}] ", ("meta", "gap"))
        if item["kind"] == "note":
            self._insert(item["text"] + "\n", ("meta",))
            return
        color = ME_COLOR if item["out"] else speaker_color(self.colors, item["who"])
        self._insert(("me" if item["out"] else item["nick"]) + ": ", (color, "name"))
        if item["kind"] == "msg":
            self._insert(item["text"], (color,), names)
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
            error = item.get("error", "error")
            # A group message says who it missed; the rest have it.
            chat.insert("end", f"  ✗ {error}  " if item.get("missed")
                        else f"  ✗ not delivered ({error})  ", "fail")
            tag = f"retry-{index}"
            chat.insert("end", "retry", ("link", tag))
            chat.tag_bind(tag, "<Button-1>", lambda e, c=conv, i=item: self._retry(c, i))
            chat.tag_bind(tag, "<Enter>", lambda e: chat.config(cursor="hand2"))
            chat.tag_bind(tag, "<Leave>", lambda e: chat.config(cursor=""))
        elif status == "ok" and item["kind"] == "file" and item["out"]:
            chat.insert("end", "  ✓ sent", "meta")
        if item.get("thumb"):
            chat.insert("end", "\n")
            at = chat.index("end-1c")
            chat.image_create(at, image=item["thumb"], padx=16, pady=4)
            tag = f"picture-{index}"
            chat.tag_add(tag, at)
            chat.tag_bind(tag, "<Button-3>", lambda e, i=item: self._picture_menu(e, i))
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

    def _name(self, addr) -> str:
        conv = self.convs.get(addr)
        return conv.nick if conv else addr[0]

    def _incoming(self, conv: Conversation, mention_by: str | None = None):
        """`mention_by`: who mentioned me in the message that just arrived."""
        focused = self.root.focus_displayof() is not None
        if conv.key != self.current or not focused:
            if conv.key != self.current:
                conv.unread += 1
                conv.mentioned = conv.mentioned or mention_by is not None
            if mention_by is not None:
                self._set_status(f"{mention_by} mentioned you in {conv.nick}")
                self.root.bell()
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
        self.convs[self.current].mentioned = False
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

    def _group_conv(self, group) -> Conversation:
        conv = self.convs.get(group.gid)
        if conv is None:
            conv = self.convs[group.gid] = Conversation(group.gid, group.name, group)
            self.order.append(group.gid)
        return conv

    def _join_group(self, addr, nick: str, gid: str, name: str, members):
        """(conversation, whether we just joined) of the group that a message
        or picture from `addr` says it belongs to; (None, False) if `addr` is
        not in the group as we know it."""
        self._conv(addr, nick)
        new = self.groups.get(gid) is None
        group = self.groups.receive(gid, name, members)
        if group is None:
            return None, False
        conv = self._group_conv(group)
        if new:
            self._add_item(conv, kind="note", text=f"{nick} added you to this group.")
        return conv, new

    def _new_group(self):
        peers = [(c.key, self._label(c)) for c in self.convs.values()
                 if c.is_peer and not c.blocked]
        if not peers:
            self._set_status("Nobody to make a group with yet")
            return
        group = GroupDialog(self.root, peers, self.groups.create).result
        if group is None:
            return
        conv = self._group_conv(group)
        who = ", ".join(self._name(m) for m in group.members)
        self._add_item(conv, kind="note", text=f"You created this group with {who}.")
        self.current = conv.key
        self._render_list()
        self._render_chat()
        self.input.focus_set()

        def work():
            missed = self._to_group(group, "", group.members)
            if missed:
                self.events.put({"type": "invited", "conv": conv, "missed": missed})

        threading.Thread(target=work, daemon=True).start()

    def _to_group(self, group, text: str, members) -> list:
        """Send each of `members` their copy of a message. Returns the members
        whose copy did not arrive. Runs on a worker thread."""
        return self._to_members(members, lambda member: self.node.send_group(
            member, group.gid, group.name, [m for m in group.members if m != member], text))

    def _to_members(self, members, send) -> list:
        """Call `send(member)` for each of `members`, all at once so one
        unreachable member does not hold up the rest. Returns the members it
        failed for. Runs on a worker thread."""
        missed = []

        def attempt(member):
            try:
                send(member)
            except (DeliveryError, OSError):
                missed.append(member)

        threads = [threading.Thread(target=attempt, args=(m,), daemon=True) for m in members]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [m for m in members if m in missed]

    def _toggle_block(self):
        conv = self.convs[self.current]
        if not conv.is_peer:
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

    def _toggle_emoji(self):
        if self.emoji_win is not None:
            self._close_emoji()
            return
        win = self.emoji_win = tk.Toplevel(self.root)
        win.title("Emoji")
        win.resizable(False, False)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_emoji)
        win.bind("<Escape>", lambda e: self._close_emoji())
        for n, emoji in enumerate(EMOJI):
            image = self.picker_emoji.get(emoji)
            look = ({"image": image, "padx": 4, "pady": 4} if image else
                    {"text": emoji, "font": ("Segoe UI Emoji", 13), "width": 2})
            tk.Button(win, relief="flat", takefocus=False, **look,
                      command=lambda e=emoji: self._insert_emoji(e)).grid(
                row=n // EMOJI_COLUMNS, column=n % EMOJI_COLUMNS)
        # Above the input box, so it covers the chat rather than what is being typed.
        win.update_idletasks()
        win.geometry(f"+{self.input.winfo_rootx()}"
                     f"+{max(0, self.input.winfo_rooty() - win.winfo_reqheight() - 40)}")

    def _close_emoji(self):
        self.emoji_win.destroy()
        self.emoji_win = None
        self.input.focus_set()

    def _insert_emoji(self, emoji: str):
        self.input.insert("insert", emoji)
        self.input.see("insert")
        self.input.focus_set()
        self._render_counter()

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
        if item.get("status") != "failed":
            return
        if item["kind"] == "file":
            item["status"] = "sending"
            self._render_chat()
            self._transfer(conv, [item])
        else:
            self._dispatch(conv, item)

    def _dispatch(self, conv: Conversation, item: dict):
        item["status"] = "sending"
        # A retry goes only to the group members the message has not reached.
        targets = item.get("missed") or (conv.group and conv.group.members)
        self._render_chat()

        def work():
            error, reached, missed = None, True, []
            try:
                if conv.group:
                    missed = self._to_group(conv.group, item["text"], targets)
                    reached = len(missed) < len(targets)
                elif conv.key == EVERYONE:
                    self.node.send_broadcast(item["text"])
                else:
                    self.node.send_message(conv.key, item["text"])
            except (DeliveryError, OSError) as e:
                error, reached = str(e) or "error", False
            self.events.put({"type": "sent", "conv": conv, "item": item,
                             "error": error, "reached": reached, "missed": missed})

        threading.Thread(target=work, daemon=True).start()

    def _on_paste(self, event=None):
        """A picture on the clipboard is sent; anything else pastes as text."""
        try:
            from PIL import ImageGrab
            content = ImageGrab.grabclipboard()
        except Exception:         # no Pillow, or a clipboard it cannot read
            return None
        # Copied files win over text; text wins over a bitmap, which programs
        # such as Excel put beside the text they copy.
        if content is None or (not isinstance(content, list) and self._clipboard_text()):
            return None
        conv = self.convs[self.current]
        if conv.key == EVERYONE:
            self._set_status("Pictures can be sent in a one-to-one conversation or a group")
            return "break"
        sources, rejected = from_clipboard(content, time.time())
        if rejected:
            self._add_item(conv, kind="note",
                           text="Not sent, only .png and .jpg pictures can be pasted: "
                                + ", ".join(rejected))
        if conv.group:
            sources, too_big = split_by_size(sources, MAX_GROUP_PICTURE)
            if too_big:
                self._add_item(conv, kind="note",
                               text=f"Not sent, a picture in a group can be at most "
                                    f"{human_size(MAX_GROUP_PICTURE)}: " + ", ".join(too_big))
        if sources:
            self._send_jobs(conv, sources)
        else:
            self._render_chat()
        return "break"

    def _clipboard_text(self) -> bool:
        try:
            return bool(self.root.clipboard_get())
        except tk.TclError:
            return False

    def _send_files(self):
        conv = self.convs[self.current]
        if not conv.is_peer:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="Send file")
        if paths:
            self._send_jobs(conv, paths)

    def _send_jobs(self, conv: Conversation, sources):
        """Send each source to the peer or group: a path, read in place, or
        (name, bytes) for a picture that exists only in memory."""
        items = []
        for source in sources:
            if isinstance(source, tuple):
                (name, content), size = source, len(source[1])
            else:
                try:
                    size = os.path.getsize(source)
                except OSError:
                    continue
                name, content = os.path.basename(source), source
            # The source stays on the item so that `retry` can send it again.
            item = self._add_item(conv, kind="file", out=True, nick="me", name=name,
                                  size=size, status="sending", source=source)
            self._attach_picture(conv, item, content)
            items.append(item)
        self._render_chat()
        self._transfer(conv, items)

    def _transfer(self, conv: Conversation, items):
        """Send the outgoing file items one after another on a worker thread."""
        def send(addr, source, progress, group=None):
            if isinstance(source, tuple):
                self.node.send_bytes(addr, *source, progress, group)
            else:
                self.node.send_file(addr, source, progress, group)

        def work():
            for item in items:
                source = item["source"]
                def progress(done, size, name=item["name"]):
                    self.events.put({"type": "send_progress", "name": name,
                                     "done": done, "size": size})
                error, status, reached, missed = None, "ok", True, []
                if conv.group:
                    group = conv.group
                    # A retry goes only to the members the picture has not reached.
                    targets = item.get("missed") or group.members
                    done = dict.fromkeys(targets, 0)

                    def to_member(member):
                        def member_progress(sent, size):
                            done[member] = sent
                            progress(sum(done.values()), size * len(targets))
                        others = [m for m in group.members if m != member]
                        send(member, source, member_progress, (group.gid, group.name, others))

                    missed = self._to_members(targets, to_member)
                    reached = len(missed) < len(targets)
                    if missed:
                        status = "failed"
                else:
                    try:
                        send(conv.key, source, progress)
                    except Declined:
                        status, reached = "declined", False
                    except (DeliveryError, OSError) as e:
                        status, error, reached = "failed", str(e) or "error", False
                self.events.put({"type": "file_sent", "conv": conv, "item": item,
                                 "status": status, "error": error, "reached": reached,
                                 "missed": missed})

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
            # A group's title and mentions show its members' names.
            if conv.key == self.current or self.convs[self.current].group:
                self._render_chat()
        elif kind in ("message", "broadcast"):
            sender = self._conv(ev["addr"], ev["nick"])
            conv = sender if kind == "message" else self.convs[EVERYONE]
            self._add_item(conv, kind="msg", out=False, nick=ev["nick"], who=ev["addr"],
                           text=ev["text"])
            self.session.record(kind=kind, dir="in", peer=ev["addr"][0],
                                nick=ev["nick"], text=ev["text"])
            mine = kind == "broadcast" and mentions_me(ev["text"], self.cfg["nickname"])
            self._incoming(conv, ev["nick"] if mine else None)
        elif kind == "group":
            conv, new = self._join_group(ev["addr"], ev["nick"], ev["gid"], ev["name"],
                                         ev["members"])
            if conv is None:
                return
            if ev["text"]:
                self._add_item(conv, kind="msg", out=False, nick=ev["nick"],
                               who=ev["addr"], text=ev["text"])
                self.session.record(kind="group", dir="in", peer=ev["addr"][0],
                                    nick=ev["nick"], group=conv.group.name, text=ev["text"])
            if new or ev["text"]:
                mine = mentions_me(ev["text"], self.cfg["nickname"])
                self._incoming(conv, ev["nick"] if mine else None)
        elif kind == "invited":
            who = ", ".join(self._name(m) for m in ev["missed"])
            self._add_item(ev["conv"], kind="note",
                           text=f"Could not reach {who}. They join the group when "
                                "a message gets through to them.")
            if ev["conv"].key == self.current:
                self._render_chat()
        elif kind == "sent":
            conv, item = ev["conv"], ev["item"]
            item["missed"] = ev["missed"]
            if ev["missed"]:
                ev["error"] = "not delivered to " + ", ".join(map(self._name, ev["missed"]))
            item["status"], item["error"] = ("failed", ev["error"]) if ev["error"] else ("ok", None)
            # A group message that reached only some members was still sent.
            if ev["reached"] and not item.get("recorded"):
                item["recorded"] = True
                if conv.group:
                    self.session.record(kind="group", dir="out", group=conv.group.name,
                                        text=item["text"])
                else:
                    self.session.record(
                        kind="broadcast" if conv.key == EVERYONE else "message", dir="out",
                        peer=conv.key[0] if conv.is_peer else None, text=item["text"])
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
            # Accepting the offer put us in the group, so it is there.
            group = ev["gid"] and self.groups.get(ev["gid"])
            if group:
                conv = self._group_conv(group)
            incognito = Path(ev["path"]).parent == incognito_dir()
            item = self._add_item(conv, kind="file", out=False, nick=ev["nick"],
                                  who=ev["addr"], name=ev["name"], size=ev["size"],
                                  path=ev["path"], incognito=incognito)
            self._attach_picture(conv, item, ev["path"])
            self.session.record(kind="file", dir="in", peer=ev["addr"][0], nick=ev["nick"],
                                name=ev["name"], size=ev["size"], path=ev["path"],
                                **({"group": group.name} if group else {}))
            self._set_status(f"Received {ev['name']}")
            self._incoming(conv)
        elif kind == "file_failed":
            self._set_status(f"Receiving {ev['name']} failed: {ev['error']}")
        elif kind == "file_sent":
            conv, item = ev["conv"], ev["item"]
            item["missed"] = ev["missed"]
            if ev["missed"]:
                ev["error"] = "not delivered to " + ", ".join(map(self._name, ev["missed"]))
            item["status"], item["error"] = ev["status"], ev["error"]
            # A group picture that reached only some members was still sent.
            if ev["reached"] and not item.get("recorded"):
                item["recorded"] = True
                where = ({"group": conv.group.name} if conv.group
                         else {"peer": conv.key[0]})
                self.session.record(kind="file", dir="out", name=item["name"],
                                    size=item["size"], **where)
            self._set_status({"ok": "Sent ", "declined": "Declined: ",
                              "failed": "Failed: "}[ev["status"]] + item["name"])
            if conv.key == self.current:
                self._render_chat()
        elif kind == "picture":
            from PIL import ImageTk
            ev["item"]["thumb"] = ImageTk.PhotoImage(ev["image"])
            ev["item"]["picture"] = ev["source"]
            if ev["conv"].key == self.current:
                self._render_chat()
        elif kind == "copied":
            self._set_status(ev["text"])
        elif kind == "incompatible":
            self._set_status(f"{ev['ip']} runs an incompatible version of Whisper")

    def _handle_offer(self, ev: dict):
        offer = ev["offer"]
        if time.monotonic() > ev["expires"]:
            return
        if offer["group"]:
            # A group picture is taken unasked from a member; net.py has made
            # sure it can only be a picture.
            conv, new = self._join_group(offer["addr"], offer["nick"], *offer["group"])
            if new:
                self._render_list()
            try:
                ev["reply"].put_nowait(conv is not None)
            except queue.Full:
                pass
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

    # ── pictures ─────────────────────────────────────────────────────────────

    def _attach_picture(self, conv: Conversation, item: dict, source):
        """If `source` (a path or bytes) is a PNG or JPEG, draw it under the
        item and keep it for Copy image. The thumbnail lives in memory only.
        A large picture takes a while to decode, so that happens on a worker
        thread; the "picture" event brings the result back."""
        if not isinstance(source, bytes) and not is_image_file(source):
            return

        def work():
            image = thumbnail(source, PICTURE_SIDE)
            if image is not None:
                self.events.put({"type": "picture", "conv": conv, "item": item,
                                 "image": image, "source": source})

        threading.Thread(target=work, daemon=True).start()

    def _picture_menu(self, event, item: dict):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Copy image", command=lambda: self._copy_picture(item))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_picture(self, item: dict):
        """Decoding at full size takes a while, so it runs on a worker thread."""
        def work():
            data = dib(item["picture"])
            if data is None:
                text = f"Cannot read {item['name']} any more"
            elif clipboard_dib(data):
                text = f"Copied {item['name']}"
            else:
                text = "Could not put the picture on the clipboard"
            self.events.put({"type": "copied", "text": text})

        self._set_status(f"Copying {item['name']}…")
        threading.Thread(target=work, daemon=True).start()

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
