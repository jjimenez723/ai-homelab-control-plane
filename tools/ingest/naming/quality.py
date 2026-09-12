"""Is this filename already useful?

Answered deterministically, from the name alone. This runs before any model is
consulted, and it is the gate that stops the system churning names the owner
already chose: `Fine-Line-Group-2026-SOW.pdf` is a better filename than
anything a model will invent for it, and invariant 7 says so.

It is also the first line of idempotency. A name this module calls GOOD is
never re-proposed, so a file the pipeline has already renamed is left alone on
every subsequent run -- without needing to consult the ledger. (The ledger is
still the authoritative check; see `ledger.known_names`.)

Two answers, because there are only two consequences:

* GOOD -- leave it alone.
* POOR -- eligible for a proposal. Not "will be renamed": a proposal still has
  to clear confidence and policy.
"""

from __future__ import annotations

import re
import unicodedata

QUALITY_GOOD = "good"
QUALITY_POOR = "poor"

# Names produced by a device or an app rather than by a person. Matched against
# the stem, case-insensitively, anchored: a *whole* stem that is one of these
# carries no information beyond a counter.
_MACHINE_STEM_PATTERNS = (
    # Cameras and phones
    r"img[_\-\s]?\d+",
    r"_?mg[_\-\s]?\d+",
    r"dsc[nf]?[_\-\s]?\d+",
    r"dscn\d+",
    r"p\d{7}",
    r"pxl[_\-]\d{8}[_\-]\d+.*",
    r"gopr\d+",
    r"g[hpx]\d{6}",
    r"dji[_\-]\d+",
    r"vid[_\-]?\d+",
    r"mvimg[_\-]\d+",
    r"mov[_\-]?\d+",
    r"video[_\-\s]?\d*",
    r"movie[_\-\s]?\d*",
    r"clip[_\-\s]?\d*",
    r"photo[_\-\s]?\d*",
    r"picture[_\-\s]?\d*",
    r"image[_\-\s]?\d*",
    r"img",
    r"scan[_\-\s]?\d*",
    r"scanned document[_\-\s]?\d*",
    # Screenshots, including the common localized and third-party spellings
    r"screen[_\-\s]?shot.*",
    r"screenshot.*",
    r"cleanshot.*",
    r"bildschirmfoto.*",
    r"capture d.?.?cran.*",
    r"captura de pantalla.*",
    r"snimok ekrana.*",
    # Generic app/browser output
    r"untitled[_\-\s]?\d*",
    r"unnamed[_\-\s]?\d*",
    r"no ?name[_\-\s]?\d*",
    r"document[_\-\s]?\d*",
    r"doc[_\-\s]?\d*",
    r"file[_\-\s]?\d*",
    r"download[s]?[_\-\s]?\d*",
    r"new[_\-\s]?\d*",
    r"copy[_\-\s]?\d*",
    r"output[_\-\s]?\d*",
    r"export[_\-\s]?\d*",
    r"final[_\-\s]?\d*",
    r"draft[_\-\s]?\d*",
    r"temp[_\-\s]?\d*",
    r"tmp[_\-\s]?\d*",
    r"test[_\-\s]?\d*",
    r"asdf+",
    r"aaa+",
)

_MACHINE_STEM_RE = re.compile(r"^(?:%s)$" % "|".join(_MACHINE_STEM_PATTERNS), re.IGNORECASE)

# `download (7)`, `Untitled 3`, `file copy 2` -- a generic stem plus a counter.
_COUNTER_SUFFIX_RE = re.compile(r"[\s_\-]*(?:\(\d+\)|copy(?:\s*\d+)?|\d+)$", re.IGNORECASE)

_UUID_RE = re.compile(r"^\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?$", re.IGNORECASE)
_HEX_BLOB_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_RANDOM_BLOB_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")
_DIGITS_ONLY_RE = re.compile(r"^[\d\W_]+$")

# Tokens that carry no subject matter. A stem made only of these is poor,
# however many of them there are: `new-file-copy-final` says nothing.
_EMPTY_TOKENS = frozenset(
    {
        "a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "by", "with", "from",
        "img", "image", "images", "photo", "photos", "picture", "pictures", "pic", "pics",
        "video", "videos", "movie", "clip", "scan", "scanned",
        "file", "files", "doc", "docs", "document", "documents", "pdf",
        "download", "downloads", "downloaded", "export", "exported", "output",
        "untitled", "unnamed", "noname", "new", "copy", "duplicate", "temp", "tmp",
        "final", "draft", "version", "ver", "rev", "test", "misc", "stuff", "thing", "things",
        "asset", "assets", "item", "items", "data", "sample", "example", "default",
        "am", "pm", "at",
    }
)

# camelCase / PascalCase boundaries, so `GoogleChrome` reads as two words.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _normalize(stem: str) -> str:
    return unicodedata.normalize("NFC", stem).strip()


def tokenize(stem: str) -> list[str]:
    """Split a filename stem into lowercase word tokens."""
    spaced = _CAMEL_RE.sub(" ", _normalize(stem))
    return [token.lower() for token in _SPLIT_RE.split(spaced) if token]


def meaningful_tokens(stem: str, vocabulary: frozenset[str] | tuple[str, ...] = ()) -> list[str]:
    """Tokens that actually say something about the file's subject.

    A vocabulary word (a client, a project, a course code the owner uses) always
    counts, even if it would otherwise look too short or too generic.
    """
    known = {word.lower() for word in vocabulary}
    out: list[str] = []
    for token in tokenize(stem):
        if token in known:
            out.append(token)
            continue
        if token in _EMPTY_TOKENS:
            continue
        if token.isdigit():
            continue
        if len(token) < 3:
            continue
        if not any(character.isalpha() for character in token):
            continue
        out.append(token)
    return out


def assess(filename: str, *, vocabulary: frozenset[str] | tuple[str, ...] = ()) -> tuple[str, str]:
    """Classify a filename. Returns (quality, reason).

    The reason is recorded in the naming ledger, so a decision the owner
    disagrees with can be traced to the rule that made it.
    """
    stem = _normalize(filename).rsplit(".", 1)[0] if "." in filename else _normalize(filename)
    stem = stem.strip()

    if not stem:
        return QUALITY_POOR, "empty filename stem"

    # Machine-generated patterns are checked before vocabulary. Otherwise a
    # short client code that happens to appear inside a camera filename
    # (`img` in `IMG_6909`) would certify it as meaningful.
    if _UUID_RE.match(stem):
        return QUALITY_POOR, "filename is a bare UUID"
    if _HEX_BLOB_RE.match(stem):
        return QUALITY_POOR, "filename is a bare hex/hash string"
    if _DIGITS_ONLY_RE.match(stem):
        return QUALITY_POOR, "filename contains no words"
    if _MACHINE_STEM_RE.match(stem):
        return QUALITY_POOR, "machine-generated filename pattern"

    trimmed = _COUNTER_SUFFIX_RE.sub("", stem).strip()
    if trimmed and trimmed != stem and _MACHINE_STEM_RE.match(trimmed):
        return QUALITY_POOR, "machine-generated filename with a counter suffix"

    # Vocabulary is matched on whole tokens, never as a substring: the owner's
    # project codes are words in a name, not letters hidden inside one.
    tokens = meaningful_tokens(stem, vocabulary)
    known = {word.lower() for word in vocabulary}
    if known and known.intersection(tokenize(stem)):
        return QUALITY_GOOD, "contains known project/client vocabulary"
    if not tokens:
        return QUALITY_POOR, "no meaningful words in the filename"

    if _RANDOM_BLOB_RE.match(stem) and len(tokens) <= 1 and not _looks_pronounceable(stem):
        return QUALITY_POOR, "filename looks like a random identifier"

    return QUALITY_GOOD, f"{len(tokens)} meaningful word(s) in the filename"


def _looks_pronounceable(stem: str) -> bool:
    """Cheap guard against calling a long real word a random identifier.

    A random token tends to have a vowel ratio far from that of ordinary text;
    this only has to separate `a1b2c3d4e5f6g7h8i9` from `architecturereview`.
    """
    letters = [character for character in stem.lower() if character.isalpha()]
    if len(letters) < 6:
        return False
    vowels = sum(1 for character in letters if character in "aeiou")
    return 0.2 <= vowels / len(letters) <= 0.6


def is_poor(filename: str, *, vocabulary: frozenset[str] | tuple[str, ...] = ()) -> bool:
    return assess(filename, vocabulary=vocabulary)[0] == QUALITY_POOR
