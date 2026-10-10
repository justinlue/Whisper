from whisper_im.ui import ME_COLOR, PEER_COLORS, speaker_color, whole_characters


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
