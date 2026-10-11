from whisper_im.markup import MENTION, MENTION_ME, emoji_pieces, mentions, mentions_me

NAMES = ["alice", "Bob Stone", "bob"]


def test_text_without_mentions_is_one_plain_piece():
    assert mentions("lunch at noon?", NAMES, "alice") == [("lunch at noon?", None)]


def test_mention_of_a_member_is_picked_out():
    assert mentions("ask @bob about it", NAMES, "alice") == [
        ("ask ", None), ("@bob", MENTION), (" about it", None)]


def test_mention_of_me_is_told_apart():
    assert mentions("@Alice, ping", NAMES, "alice") == [
        ("@Alice", MENTION_ME), (", ping", None)]
    assert mentions_me("@Alice, ping", "alice")
    assert not mentions_me("ask @bob", "alice")


def test_longest_name_wins_and_names_may_hold_spaces():
    assert mentions("hi @Bob Stone!", NAMES, "alice") == [
        ("hi ", None), ("@Bob Stone", MENTION), ("!", None)]


def test_not_a_mention():
    for text in ("@bobby is new", "mail bob@bob.org", "@ bob", "@nobody here"):
        assert mentions(text, NAMES, "alice") == [(text, None)], text
    assert not mentions_me("alice@example.org", "alice")


def test_name_with_punctuation_in_it_can_be_mentioned():
    assert mentions("@justi@PC-1 hello", ["justi@PC-1"], "x") == [
        ("@justi@PC-1", MENTION), (" hello", None)]


def test_emoji_are_picked_out_of_the_text_around_them():
    assert emoji_pieces("hi 😀!") == [("hi ", None), ("😀", "😀"), ("!", None)]
    assert emoji_pieces("😀😁") == [("😀", "😀"), ("😁", "😁")]


def test_plain_text_is_one_piece():
    for text in ("", "plain", "你好 ✓ © 2 #1", "🇨🇳"):
        assert emoji_pieces(text) == ([(text, None)] if text else []), text


def test_modifiers_stay_with_their_emoji_but_only_the_emoji_is_drawn():
    assert emoji_pieces("👍🏽") == [("👍🏽", "👍")]
    assert emoji_pieces("❤️") == [("❤️", "❤")]
    assert emoji_pieces("❤") == [("❤", "❤")]
    assert emoji_pieces("☺️") == [("☺️", "☺")]


def test_joined_emoji_are_drawn_as_their_parts():
    assert emoji_pieces("👨‍👩‍👧") == [("👨‍", "👨"), ("👩‍", "👩"), ("👧", "👧")]


def test_no_character_is_lost():
    text = "a👍🏽b👨‍👩‍👧 🇨🇳 ❤️‍ ✓ 1️⃣ 😀"
    assert "".join(piece for piece, _ in emoji_pieces(text)) == text
