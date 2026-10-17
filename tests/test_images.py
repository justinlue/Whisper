"""Pasted and received pictures."""
import io
import struct
import time

from PIL import Image

from whisper_im.images import MAX_PIXELS, dib, from_clipboard, thumbnail


def png_bytes(size=(4, 3), color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def jpeg_bytes(size=(4, 3)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "blue").save(out, "JPEG")
    return out.getvalue()


def test_copied_png_and_jpeg_files_are_sent_from_where_they_are(tmp_path):
    png, jpg, jpeg = tmp_path / "a.png", tmp_path / "b.JPG", tmp_path / "c.jpeg"
    png.write_bytes(png_bytes())
    jpg.write_bytes(jpeg_bytes())
    jpeg.write_bytes(jpeg_bytes())
    assert from_clipboard([str(png), str(jpg), str(jpeg)], 0) == ([png, jpg, jpeg], [])


def test_other_files_are_turned_down_by_name(tmp_path):
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a....")
    fake = tmp_path / "fake.png"            # right name, wrong contents
    fake.write_bytes(b"MZ\x90\x00 not a picture")
    swapped = tmp_path / "swapped.jpg"      # a PNG called .jpg
    swapped.write_bytes(png_bytes())
    folder = tmp_path / "pics.png"
    folder.mkdir()
    missing = tmp_path / "gone.png"
    content = [str(p) for p in (gif, fake, swapped, folder, missing)]
    assert from_clipboard(content, 0) == (
        [], ["anim.gif", "fake.png", "swapped.jpg", "pics.png", "gone.png"])


def test_a_bitmap_is_sent_as_a_png_made_in_memory():
    when = time.mktime((2026, 10, 17, 15, 30, 12, 0, 0, -1))
    (source,), rejected = from_clipboard(Image.new("RGB", (5, 2), "green"), when)
    name, data = source
    assert name == "pasted-20261017-153012.png" and rejected == []
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == (5, 2)
        assert image.convert("RGB").getpixel((0, 0)) == (0, 128, 0)


def test_an_empty_clipboard_sends_nothing():
    assert from_clipboard(None, 0) == ([], [])


def test_thumbnail_fits_the_square_and_keeps_the_shape(tmp_path):
    path = tmp_path / "wide.png"
    path.write_bytes(png_bytes((400, 100)))
    assert thumbnail(path, 200).size == (200, 50)
    assert thumbnail(jpeg_bytes((90, 300)), 150).size == (45, 150)


def test_a_small_picture_is_not_blown_up():
    assert thumbnail(png_bytes((20, 10)), 200).size == (20, 10)


def test_thumbnail_of_something_that_is_not_a_png_or_jpeg_is_none(tmp_path):
    gif = io.BytesIO()
    Image.new("RGB", (4, 4)).save(gif, "GIF")
    assert thumbnail(gif.getvalue(), 100) is None
    assert thumbnail(b"\x89PNG\r\n\x1a\n then garbage", 100) is None
    assert thumbnail(tmp_path / "missing.png", 100) is None


def test_a_picture_with_too_many_pixels_is_not_decoded():
    side = int(MAX_PIXELS ** 0.5) + 10
    bomb = io.BytesIO()
    Image.new("1", (side, side)).save(bomb, "PNG")
    assert thumbnail(bomb.getvalue(), 100) is None


def test_copying_out_gives_the_full_picture_as_a_windows_bitmap(tmp_path):
    path = tmp_path / "big.png"
    path.write_bytes(png_bytes((640, 480), "white"))
    data = dib(path)
    header_size, width, height, planes, bits = struct.unpack("<IiiHH", data[:16])
    assert (header_size, width, height, planes, bits) == (40, 640, 480, 1, 24)
    assert data[40:43] == b"\xff\xff\xff"      # first pixel, after the header


def test_copying_out_something_that_is_not_a_picture_gives_none():
    assert dib(b"not a picture") is None


def test_transparency_is_copied_out_as_white():
    clear = io.BytesIO()
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(clear, "PNG")
    assert dib(clear.getvalue())[40:43] == b"\xff\xff\xff"
