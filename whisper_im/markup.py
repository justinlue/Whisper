"""What in a message is drawn specially: @mentions and emoji. Pure functions,
so the UI only has to map the pieces to widget tags and images."""
import re

MENTION = "mention"
MENTION_ME = "mention-me"


def mentions(text: str, names, me: str) -> list[tuple[str, str | None]]:
    """`text` cut into (piece, kind): MENTION for "@name" of one of `names`,
    MENTION_ME when the name is `me`, None for everything else. Names match
    whole and without regard to case."""
    wanted = sorted({n for n in (*names, me) if n}, key=len, reverse=True)
    if not wanted:
        return [(text, None)]
    pattern = re.compile(r"(?<!\w)@(%s)(?!\w)" % "|".join(map(re.escape, wanted)),
                         re.IGNORECASE)
    pieces, end = [], 0
    for match in pattern.finditer(text):
        if match.start() > end:
            pieces.append((text[end:match.start()], None))
        mine = match.group(1).lower() == me.lower()
        pieces.append((match.group(), MENTION_ME if mine else MENTION))
        end = match.end()
    if end < len(text) or not pieces:
        pieces.append((text[end:], None))
    return pieces


def mentions_me(text: str, me: str) -> bool:
    return any(kind == MENTION_ME for _, kind in mentions(text, (), me))


_VS16, _ZWJ = "️", "‍"
# The BMP characters that are emoji without a variation selector, plus the
# heart, which people type bare.
_BMP_EMOJI = frozenset(
    "⌚⌛⏩⏪⏫⏬⏰⏳◽◾☔☕♈♉♊♋♌♍♎♏♐♑♒♓♿⚓⚡⚪⚫⚽⚾⛄⛅⛎⛔⛪⛲⛳⛵⛺⛽"
    "✅✊✋✨❌❎❓❔❕❗➕➖➗➰➿⬛⬜⭐⭕❤")


def _is_tone(ch: str) -> bool:
    return "\U0001f3fb" <= ch <= "\U0001f3ff"


def _is_emoji(text: str, i: int) -> bool:
    ch = text[i]
    if "\U0001f000" <= ch <= "\U0001faff":
        # Flags are pairs of regional indicators, which only a shaping engine
        # can combine; a skin tone on its own is a coloured square.
        return not ("\U0001f1e6" <= ch <= "\U0001f1ff" or _is_tone(ch))
    if ch in _BMP_EMOJI:
        return True
    return "‼" <= ch <= "㊙" and text[i + 1:i + 2] == _VS16


def emoji_pieces(text: str) -> list[tuple[str, str | None]]:
    """`text` cut into (piece, emoji): emoji is the single character to draw
    as a picture, None for ordinary text. A piece keeps the skin tone,
    variation selector or joiner that followed its emoji, so the pieces
    always join back into `text`."""
    pieces, plain, i = [], 0, 0
    while i < len(text):
        if not _is_emoji(text, i):
            i += 1
            continue
        if i > plain:
            pieces.append((text[plain:i], None))
        end = i + 1
        while end < len(text) and (text[end] == _VS16 or _is_tone(text[end])):
            end += 1
        if text[end:end + 1] == _ZWJ and end + 1 < len(text) and _is_emoji(text, end + 1):
            end += 1
        pieces.append((text[i:end], text[i]))
        i = plain = end
    if plain < len(text):
        pieces.append((text[plain:], None))
    return pieces
