"""Text utilities: X weighted length (twitter-text v3 rules), links, hashtags, similarity, UTM."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# twitter-text v3 config: chars in these ranges weigh 1, everything else weighs 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
TRANSFORMED_URL_LENGTH = 23

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@\w.])(?:[a-z0-9-]+\.)+(?:com|ai|io|dev|co|app|so|xyz|org|net|me|gg|sh|tech|link|ly|to)"
    r"(?:/[^\s]*)?(?![\w.])",
    re.IGNORECASE,
)
_HASHTAG_RE = re.compile(r"(?<![\w&])#[A-Za-z_][\w]*")
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{1,15}")
_ZWJ = "‍"


def _is_emoji_base(cp: int) -> bool:
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x1F1E6 <= cp <= 0x1F1FF
    )


def _is_emoji_modifier(cp: int) -> bool:
    return cp in (0xFE0F, 0xFE0E, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def _char_weight(cp: int) -> int:
    for lo, hi in _LIGHT_RANGES:
        if lo <= cp <= hi:
            return 1
    return 2


def find_urls(text: str) -> list[str]:
    urls = [m.group(0) for m in _URL_RE.finditer(text)]
    stripped = _URL_RE.sub(" ", text)
    urls += [m.group(0) for m in _BARE_DOMAIN_RE.finditer(stripped)]
    return urls


def weighted_length(text: str) -> int:
    """Approximate X's weighted length. URLs count 23, emoji count 2, CJK counts 2."""
    text = unicodedata.normalize("NFC", text)
    total = 0
    # Replace URLs by a placeholder so their characters don't count.
    for url in find_urls(text):
        text = text.replace(url, "\x00", 1)
        total += TRANSFORMED_URL_LENGTH
    i = 0
    chars = [ord(c) for c in text]
    while i < len(chars):
        cp = chars[i]
        if cp == 0:
            i += 1
            continue
        if _is_emoji_base(cp):
            # Consume the whole emoji sequence (modifiers, ZWJ joins, flag pairs) as one glyph of weight 2.
            total += 2
            i += 1
            if 0x1F1E6 <= cp <= 0x1F1FF and i < len(chars) and 0x1F1E6 <= chars[i] <= 0x1F1FF:
                i += 1
            while i < len(chars):
                if _is_emoji_modifier(chars[i]):
                    i += 1
                elif chars[i] == ord(_ZWJ) and i + 1 < len(chars):
                    i += 2
                else:
                    break
            continue
        if _is_emoji_modifier(cp) or cp == ord(_ZWJ):
            i += 1
            continue
        total += _char_weight(cp)
        i += 1
    return total


def hashtags(text: str) -> list[str]:
    return _HASHTAG_RE.findall(text)


def mentions(text: str) -> list[str]:
    return _MENTION_RE.findall(text)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = _URL_RE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _shingles(t: str, k: int = 3) -> set[str]:
    words = t.split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def similarity(a: str, b: str) -> float:
    """Max of character-sequence ratio and word-trigram Jaccard. 1.0 == identical."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    sa, sb = _shingles(na), _shingles(nb)
    jac = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    return max(seq, jac)


def max_similarity(text: str, history: list[str]) -> float:
    return max((similarity(text, h) for h in history), default=0.0)


def with_utm(url: str, source: str, medium: str, campaign: str, content: str | None = None) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.setdefault("utm_source", source)
    q.setdefault("utm_medium", medium)
    q.setdefault("utm_campaign", campaign)
    if content:
        q.setdefault("utm_content", content)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def clean_post(text: str) -> str:
    """Strip wrapping quotes/whitespace models sometimes add; collapse >2 blank lines."""
    t = text.strip()
    quotes = "\"“”"
    if len(t) >= 2 and t[0] in quotes and t[-1] in quotes and sum(t.count(q) for q in quotes) == 2:
        t = t[1:-1].strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t
