"""Pictures: what a paste sends, the small copy drawn in the chat, and the
bytes Windows' clipboard takes them as (ui.py puts them there). Only PNG and JPEG count as pictures.

Pillow is needed for bitmaps and thumbnails; without it a pasted bitmap
sends nothing and a picture is shown as a plain file.
"""
import io
import time
from pathlib import Path

# extension -> what the file's first bytes must say it is
EXTENSIONS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
# A received picture is a peer's file: one that would take this much memory
# to decode is shown as a plain file instead.
MAX_PIXELS = 40_000_000


def sniff(head: bytes) -> str | None:
    """"png" or "jpeg" from a file's first bytes, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def is_image_file(path) -> bool:
    """A .png, .jpg or .jpeg file whose contents are what its name says."""
    path = Path(path)
    kind = EXTENSIONS.get(path.suffix.lower())
    if kind is None:
        return False
    try:
        with open(path, "rb") as f:
            return sniff(f.read(8)) == kind
    except OSError:
        return False


def from_clipboard(content, now: float):
    """What pasting sends, given ImageGrab.grabclipboard()'s result:
    (sources, names of rejected files). A source is the Path of a copied
    file, which is read in place, or (name, PNG bytes) for a bitmap such as
    a screenshot, which never touches the disk."""
    if content is None:
        return [], []
    if not isinstance(content, list):
        out = io.BytesIO()
        content.save(out, "PNG")
        name = time.strftime("pasted-%Y%m%d-%H%M%S.png", time.localtime(now))
        return [(name, out.getvalue())], []
    sent, rejected = [], []
    for path in map(Path, content):
        (sent if is_image_file(path) else rejected).append(path)
    return sent, [p.name for p in rejected]


def thumbnail(source, side: int):
    """A Pillow image of `source` (a path or bytes) at most `side` pixels each
    way, or None if it is not a PNG or JPEG that decodes."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            if image.format not in ("PNG", "JPEG") or image.width * image.height > MAX_PIXELS:
                return None
            image.draft("RGB", (side, side))      # JPEG: decode at a smaller scale
            image = ImageOps.exif_transpose(image)
            image.thumbnail((side, side))
            return image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
    # Pillow raises many kinds of error on a malformed file.
    except Exception:
        return None


def dib(source) -> bytes | None:
    """`source` at full size as CF_DIB, the form Windows' clipboard holds a
    picture in: a BMP file without its 14-byte file header."""
    # No side of a picture we decode is longer than MAX_PIXELS.
    image = thumbnail(source, MAX_PIXELS)
    if image is None:
        return None
    if image.mode == "RGBA":
        from PIL import Image
        flat = Image.new("RGB", image.size, "white")
        flat.paste(image, mask=image)
        image = flat
    out = io.BytesIO()
    image.save(out, "BMP")
    return out.getvalue()[14:]
