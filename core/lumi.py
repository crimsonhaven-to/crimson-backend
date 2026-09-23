"""Lumi, the mascot's voice for the ``X-Lumi`` header, voiced errors and ``/lumi``."""

from __future__ import annotations

import random

EMPRESS = "Luminas Crimsonveil"
TITLE = "Eternal Empress of the Crimson Archives"

# ASCII only: an HTTP header value is latin-1 at best, so an emoji would raise
# on encode.
_HEADER_QUIPS = (
    "Snooping in the headers again, mortal? How quaint.",
    "Every byte here bends the knee to me.",
    "You found my whisper. Most never look this closely.",
    "The Archives breathe because I allow it.",
    "Served fresh from the crypt, just for you.",
    "Mind the dark. I do not.",
    "Yes, I see your requests. All of them.",
    "Bow, refresh, repeat. Good little mortal.",
    "An empress never sleeps; she merely caches.",
    "Curiosity is a delightful little sin.",
)

_BLESSINGS = (
    "I bless your bandwidth, mortal. Buffer not. ✨",
    "May your streams run swift and your subtitles never lie.",
    "Watch well. The night is long and the Archives are deep. 🦇",
    "You amuse me. Stay a while; the crypt is warm.",
    "Your devotion is noted in the eternal ledger. Carry on.",
    "Even an empress rewinds the good parts. No shame in it.",
    "Tonight's binge is sanctioned by royal decree. 👑",
    "Drink deep of the picture quality. I insist.",
    "Lost? Good. The best stories are found in the dark.",
    "I have watched a thousand seasons. Yours is adorable.",
)

# The handlers keep the real error in their structured fields; this is only the
# flavour shown to the viewer.
_ERROR_LINES = {
    400: "That request was malformed, mortal. Even I cannot read such gibberish.",
    401: "Halt. The Archives do not open for the uninvited. Show me your sigil.",
    403: "This chamber is sealed to you. Do not test an empress's patience.",
    404: "I searched the eternal Archives and found nothing. Even the void shrugged.",
    409: "You ask twice for the same boon. Once is quite enough.",
    413: "You bring me too much, mortal. Restraint is a virtue you lack.",
    429: "Patience. Even an empress tires of your incessant clamouring; wait, then beg again.",
    500: "Something in the crypt has broken, and it is decidedly not my fault. Probably.",
    502: "A lesser realm I lean upon has failed me. They will be dealt with.",
    503: "The Archives rest for a heartbeat. Return shortly, and mind your manners.",
    504: "I waited. It did not answer. How terribly rude of it.",
}

_ERROR_FALLBACK = {
    4: "You have erred, mortal. Try again, and try better.",
    5: "The crypt convulses. Lumi is displeased, but not at you. This time.",
}


def header_quip() -> str:
    return random.choice(_HEADER_QUIPS)


def blessing() -> str:
    return random.choice(_BLESSINGS)


def voiced_error(status_code: int) -> str:
    line = _ERROR_LINES.get(status_code)
    if line:
        return line
    return _ERROR_FALLBACK.get(status_code // 100, "The Archives stir uneasily.")
