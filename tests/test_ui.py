from whisper_im.ui import (EMOJI, ME_COLOR, PEER_COLORS, split_by_size, speaker_color,
                           whole_characters)


def test_a_speaker_keeps_their_colour():
    assigned = {}
    first = speaker_color(assigned, ("10.0.0.2", 48765))
    speaker_color(assigned, ("10.0.0.3", 48765))
    assert speaker_color(assigned, ("10.0.0.2", 48765)) == first


def test_speakers_get_different_colours_until_the_palette_runs_out():
    assigned = {}
    colours = [speaker_color(assigned, ("10.0.0.%d" % n, 48765))
               for n in range(len(PEER_COLORS))]
    assert len(set(colours)) == len(PEER_COLORS)
    assert speaker_color(assigned, ("10.0.1.1", 48765)) in PEER_COLORS


def test_same_ip_on_another_port_is_another_speaker():
    assigned = {}
    assert (speaker_color(assigned, ("10.0.0.2", 48765))
            != speaker_color(assigned, ("10.0.0.2", 48766)))


def test_no_peer_is_mistaken_for_me():
    assert ME_COLOR not in PEER_COLORS


def test_whole_emoji_are_left_alone():
    text = "ok 😀 👍🏽 👨‍👩‍👧 🇨🇳 ❤️ 你好"
    assert whole_characters(text) == text


def test_emoji_typed_as_two_halves_is_joined():
    assert whole_characters("\ud83d" + "\ude00") == "😀"


def test_half_an_emoji_becomes_something_sendable():
    cleaned = whole_characters("a\ud83dX\ude00b")
    cleaned.encode()
    assert cleaned == "a\N{REPLACEMENT CHARACTER}X\N{REPLACEMENT CHARACTER}b"


def test_pictures_over_the_limit_are_split_off_by_name(tmp_path):
    small, big = tmp_path / "small.png", tmp_path / "big.jpg"
    small.write_bytes(bytes(10))
    big.write_bytes(bytes(11))
    sources = [small, big, ("pasted.png", bytes(10)), ("huge.png", bytes(11))]
    fit, too_big = split_by_size(sources, 10)
    assert fit == [small, ("pasted.png", bytes(10))]
    assert too_big == ["big.jpg", "huge.png"]


def test_a_file_that_cannot_be_measured_is_not_split_off(tmp_path):
    gone = tmp_path / "gone.png"
    assert split_by_size([gone], 10) == ([gone], [])


def test_picker_offers_each_emoji_once():
    assert len(set(EMOJI)) == len(EMOJI) > 0


def test_picker_emoji_are_ones_tk_draws_as_a_single_glyph():
    # Tk 8.6 shows a flag, family or skin-toned emoji as its separate parts.
    for emoji in EMOJI:
        assert len(emoji.removesuffix("\N{VARIATION SELECTOR-16}")) == 1, emoji
