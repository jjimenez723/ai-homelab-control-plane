"""Filename policy and the one rename primitive.

Everything a model suggests passes through `validate_proposal` before it can
become a filename, and every rename goes through `safe_rename`. Those two
functions are the entire filesystem authority of the naming pipeline.

Two rules do most of the work:

* **The extension comes from the file, never from the proposal.** A model that
  suggests `.jpeg` for a `.HEIC`, or drops the extension entirely, cannot
  change what the file is.
* **A rename never overwrites.** `os.rename` silently clobbers on POSIX, so it
  is not used. `os.link` + `unlink` is atomic and fails if the target exists;
  where hardlinks are unavailable the fallback reserves the name with
  `O_CREAT|O_EXCL` first. Either way the worst case of a crash mid-rename is a
  file reachable under two names -- never a file that is gone.
"""

from __future__ import annotations

import errno
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from tools.ingest.naming.quality import QUALITY_POOR, assess

# Forbidden on APFS/HFS+ via Finder, on exFAT, or in a POSIX path component.
# The union is used everywhere so a name stays valid wherever the file travels.
_FORBIDDEN_CHARS = set('/\\:*?"<>|')
_CONTROL_CHARS = {chr(code) for code in range(0x20)} | {chr(0x7F)}
# Zero-width and bidi-override characters: invisible in a filename, and a
# classic way to make one name look like another.
_INVISIBLE_CHARS = {"​", "‌", "‍", "⁠", "﻿", "‪", "‫", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}

# The two sets above name the characters this module has always called out by
# hand, and they are kept because they document the intent. They are not the
# test, though: enumerating an attack class by hand leaves the members nobody
# thought of, and a proposer is untrusted by construction (D-022), so the
# filter is by Unicode *category* and the hand-written sets are a subset of it.
#
# * `Cc` -- C0 controls *and* the C1 block (U+0080-U+009F). A C1 byte is what
#   makes an 8-bit ANSI/OSC escape sequence work in a terminal that still
#   honours one, and `organize` prints filenames to a terminal.
#   `evidence/types.py` already strips C1 from extracted text for exactly this
#   reason; a filename is the more durable place for it to land.
# * `Cf` -- every format character, not only the nine bidi/zero-width ones
#   listed above. U+200E/U+200F (the bidi *marks*), U+202C, U+00AD (soft
#   hyphen) and U+2062 (invisible times) were all invisible in a filename and
#   all survived.
# * `Cs` -- lone surrogates. These cannot be encoded to UTF-8 at all, so one
#   reaching the length check below raised `UnicodeEncodeError` instead of
#   `NamePolicyError`: a rejection that left no `naming_history` row, which is
#   the one outcome D-057 says an audit trail must never have.
# * `Co` -- private-use characters, which render as whatever the reader's font
#   decides and therefore cannot be part of a name we claim to have recorded.
_ILLEGAL_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})

# Characters that render as a path separator but are not one. A name carrying
# one looks like a directory in Finder and in any UI that shows it, and the
# module already blocks invisible and bidi characters for the same reason:
# a filename should not be able to misrepresent itself.
_SLASH_HOMOGLYPHS = {"\u2044", "\u2215", "\uff0f", "\u29f8", "\u2216", "\uff3c", "\u29f5"}

# Device names DOS reserved and Windows still refuses, with or without an
# extension. Harmless on APFS; not on the exFAT and SMB paths these names are
# meant to survive.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)

_SEPARATOR_RUN_RE = re.compile(r"[-_\s]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_MULTI_DASH_RE = re.compile(r"-{2,}")
# A leading ISO date plus one underscore, as every proposal is built.
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
_MULTI_UNDERSCORE_RE = re.compile(r"_{2,}")

# APFS allows 255 *bytes* per component; the configured cap is a readability
# limit well below that, and this is the hard ceiling underneath it.
MAX_COMPONENT_BYTES = 255


class NamePolicyError(ValueError):
    """A proposal cannot be turned into a legal, safe filename."""


@dataclass(frozen=True)
class ValidatedName:
    filename: str
    changed: bool
    reason: str


def slugify(text: str, *, max_length: int = 80) -> str:
    """Reduce free text to a lowercase, hyphenated, filename-safe slug.

    Accents are folded rather than stripped, so `café` becomes `cafe` instead
    of `caf`.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(character for character in decomposed if not unicodedata.combining(character))
    cleaned = "".join(
        character if (character.isalnum() or character in "-_ ") else " "
        for character in folded
    )
    slug = _SEPARATOR_RUN_RE.sub("-", cleaned.strip()).strip("-").lower()
    slug = _MULTI_DASH_RE.sub("-", slug)
    if len(slug) > max_length:
        # Cut on a word boundary so the slug stays readable.
        slug = slug[:max_length].rsplit("-", 1)[0] or slug[:max_length]
    return slug.strip("-")


# Compound suffixes where the second-to-last component carries meaning.
# `archive.tar.gz` renamed to `.gz` tells Finder it is a plain gzip stream, and
# unarchiving it then yields an extension-less tar -- so both parts are kept.
_COMPOUND_INNER_SUFFIXES = frozenset({".tar", ".pkg"})


def normalized_extension(original_filename: str) -> str:
    """The extension the renamed file must keep, lowercased.

    Taken from the file on disk, never from a proposal: the extension is what
    tells macOS and every other tool what this file *is*.
    """
    suffixes = [suffix.lower() for suffix in Path(original_filename).suffixes[-2:]]
    if len(suffixes) == 2 and suffixes[0] in _COMPOUND_INNER_SUFFIXES:
        return "".join(suffixes)
    return suffixes[-1] if suffixes else ""


def _truncate_to_bytes(text: str, *, max_bytes: int) -> str:
    """The longest prefix of `text` that fits in `max_bytes` UTF-8 bytes.

    Cuts on a character boundary, never inside one: `bytes[:n]` on a
    multi-byte string produces an invalid sequence, and a filename the
    filesystem would reject or alter is a filename the ledger would be lying
    about.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


def sanitize_stem(stem: str, *, max_length: int, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    stem = unicodedata.normalize("NFC", stem)
    stem = "".join(character for character in stem if character not in _CONTROL_CHARS)
    stem = "".join(character for character in stem if character not in _INVISIBLE_CHARS)
    # The categorical filter behind the two hand-written sets above; see
    # `_ILLEGAL_UNICODE_CATEGORIES` for what each category buys.
    stem = "".join(
        character
        for character in stem
        if unicodedata.category(character) not in _ILLEGAL_UNICODE_CATEGORIES
    )
    stem = "".join("-" if character in _SLASH_HOMOGLYPHS else character for character in stem)
    stem = "".join("-" if character in _FORBIDDEN_CHARS else character for character in stem)

    # `2026-09-12_kitchen-layout` reads better than an undifferentiated run of
    # hyphens, so the single underscore after a leading ISO date is preserved
    # and everything after it is normalized as usual. It is the only underscore
    # that survives, and only in this position.
    date_prefix = ""
    match = _DATE_PREFIX_RE.match(stem)
    if match:
        date_prefix = match.group(0)
        stem = stem[len(date_prefix):]
        max_length = max(1, max_length - len(date_prefix))

    stem = _SEPARATOR_RUN_RE.sub("-", stem.strip())
    stem = _MULTI_DASH_RE.sub("-", stem)
    # A leading dot would hide the file; trailing dots and spaces are stripped
    # by some filesystems, which would make the name we recorded a lie.
    stem = stem.strip(" .-")
    if len(stem) > max_length:
        stem = stem[:max_length].rsplit("-", 1)[0] or stem[:max_length]
    # `max_length` counts characters, which is the readability cap. APFS counts
    # *bytes*, and the two diverge by 3x for CJK and 4x for emoji -- so a
    # perfectly ordinary Japanese document title cleared the character cap and
    # then blew the 255-byte ceiling, where `validate_proposal` refused it
    # outright. The ceiling is a limit to keep a name under, not a reason to
    # leave a file unnamed, so it is enforced here by trimming.
    byte_budget = max(1, max_bytes - len(date_prefix.encode("utf-8")))
    if len(stem.encode("utf-8")) > byte_budget:
        stem = _truncate_to_bytes(stem, max_bytes=byte_budget)
        stem = stem.rsplit("-", 1)[0] or stem
    stem = stem.strip(" .-")
    return f"{date_prefix}{stem}" if stem else ""


def validate_proposal(
    proposed: str,
    *,
    original_filename: str,
    max_filename_length: int,
    vocabulary: frozenset[str] | tuple[str, ...] = (),
) -> ValidatedName:
    """Turn a proposal into a filename that is legal, safe, and an improvement.

    Raises `NamePolicyError` when the proposal cannot be salvaged. A rejection
    is a normal outcome, recorded in the ledger; the file is left alone.
    """
    # A proposer is the one place model output enters this system. Its type is
    # as untrusted as its content: bytes, an int, or an object with a hostile
    # `__str__` would otherwise escape as a raw TypeError/AttributeError rather
    # than the recorded `rejected_policy` this is supposed to produce.
    if not isinstance(proposed, str):
        raise NamePolicyError(f"proposal is not a string: {type(proposed).__name__}")
    if not proposed or not proposed.strip():
        raise NamePolicyError("empty proposal")

    # A proposal is a *name*, never a location. Anything path-shaped is a
    # rejection rather than something to be cleaned up and trusted.
    if "/" in proposed or "\\" in proposed or "\x00" in proposed:
        raise NamePolicyError("proposal contains a path separator")
    if proposed.strip() in {".", ".."}:
        raise NamePolicyError("proposal is a path component, not a filename")

    extension = normalized_extension(original_filename)
    candidate = Path(proposed).name
    # Drop whatever extension the proposal carried; the real one is re-attached.
    if extension and candidate.lower().endswith(extension):
        candidate = candidate[: -len(extension)]
    elif "." in candidate:
        head, _, tail = candidate.rpartition(".")
        # Only treat the tail as an extension if it looks like one -- otherwise
        # `Fine.Line.Group` would lose its last word.
        if head and 1 <= len(tail) <= 5 and tail.isalnum():
            candidate = head

    stem_budget = max(8, max_filename_length - len(extension))
    # `file-` is the longest prefix `_RESERVED_STEMS` can add below, so the
    # byte budget leaves room for it rather than letting a reserved name be
    # the one case that overflows the component ceiling.
    byte_budget = max(8, MAX_COMPONENT_BYTES - len(extension.encode("utf-8")) - len("file-"))
    stem = sanitize_stem(candidate, max_length=stem_budget, max_bytes=byte_budget)
    if not stem:
        raise NamePolicyError("proposal contains no usable characters")

    if stem.lower() in _RESERVED_STEMS:
        # Prefixing keeps the proposal's meaning instead of discarding it.
        stem = f"file-{stem}"

    filename = f"{stem}{extension}"
    # `sanitize_stem` now trims to the byte budget, so reaching this is a bug
    # in the budget arithmetic rather than a hostile proposal. It stays as a
    # backstop: a name too long for the filesystem must never be *applied*,
    # and a `NamePolicyError` is the recorded outcome for that.
    if len(filename.encode("utf-8")) > MAX_COMPONENT_BYTES:
        raise NamePolicyError("proposal exceeds the maximum filename length")

    quality, reason = assess(filename, vocabulary=vocabulary)
    if quality == QUALITY_POOR:
        # Applying this would leave the file eligible for a proposal again next
        # run -- a rename loop. Refuse rather than churn.
        raise NamePolicyError(f"proposed name is not an improvement: {reason}")

    return ValidatedName(
        filename=filename,
        changed=filename != original_filename,
        reason=reason,
    )


def _link_rename(source: Path, target: Path) -> bool:
    """Atomic, non-clobbering rename via hardlink. False if unsupported here.

    If this crashes between the link and the unlink, the file exists under both
    names. That is recoverable and visible; an overwritten file is neither.
    """
    try:
        # `follow_symlinks=True` is the default, and it is the wrong default
        # here: handed a symlink it hardlinks the *target*, so a link planted in
        # Downloads would pull a file from outside the source root into it and
        # then unlink the symlink the owner had. The scanner already refuses
        # symlinks; this is the same rule at the moment of mutation, where the
        # check cannot be raced.
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS):
            return False  # e.g. exFAT, which has no hardlinks
        raise

    try:
        os.unlink(source)
    except OSError:
        # The link landed but the old name will not go away (a locked file,
        # `chflags uchg`, a read-only directory). Leaving both names looks
        # harmless -- same inode, no data at risk -- but the next run sees the
        # original name again, proposes again, and links again: one extra name
        # per run, forever. Undo our half instead and report the failure, so
        # the file is exactly as it was before this attempt.
        try:
            os.unlink(target)
        except OSError:
            raise NamePolicyError(
                f"linked {source.name} to {target.name} but could not remove either name; "
                "the file now has two names and needs a human"
            ) from None
        raise
    return True


def _reserve_rename(source: Path, target: Path) -> None:
    """Fallback for filesystems without hardlinks.

    `O_CREAT|O_EXCL` claims the name atomically, so the `os.replace` that
    follows can only ever overwrite the empty placeholder this call created.
    """
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        os.replace(source, target)
    except OSError:
        # Undo the reservation so a failed rename leaves no stray empty file.
        try:
            if target.stat().st_size == 0:
                target.unlink()
        except OSError:
            pass
        raise


def collision_free_name(directory: Path, filename: str, *, disambiguator: str) -> str:
    """A name in `directory` that is free, derived deterministically.

    The first fallback is content-derived (`-<sha8>`) rather than a counter, so
    re-running over the same file proposes the same name instead of climbing
    `-2`, `-3`, `-4`. The numeric tail after that only exists for the case where
    two genuinely different files share a hash prefix and a stem.
    """
    if not (directory / filename).exists():
        return filename
    stem = Path(filename).stem
    extension = Path(filename).suffix
    candidate = f"{stem}-{disambiguator}{extension}"
    if not (directory / candidate).exists():
        return candidate
    for index in range(2, 100):
        candidate = f"{stem}-{disambiguator}-{index}{extension}"
        if not (directory / candidate).exists():
            return candidate
    raise NamePolicyError(f"could not find a free filename for {filename} in {directory}")


def _is_same_file(left: Path, right: Path) -> bool:
    """Whether two paths name the same inode on the same device.

    The question `left == right` cannot answer on a case-insensitive or
    normalization-insensitive filesystem, which is the default on macOS.
    """
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def safe_rename(source: Path, target_name: str, *, disambiguator: str) -> Path:
    """Rename `source` to `target_name` in its own directory. Never overwrites.

    The only function in the naming pipeline that mutates the filesystem. It
    takes a *name*, not a path, so a caller cannot use it to move a file into
    another directory -- renaming and relocating stay separate powers.
    """
    if "/" in target_name or "\\" in target_name or target_name in {".", ".."}:
        raise NamePolicyError(f"unsafe rename target: {target_name!r}")

    # Re-checked here rather than trusting the scan: discovery, settling,
    # hashing and probing all happen first, and anything can replace a file
    # with a symlink in that window. This is the last moment before mutation,
    # so it is the only place the answer cannot go stale.
    if source.is_symlink():
        raise NamePolicyError(f"refusing to rename a symlink: {source}")

    directory = source.parent

    # `exists()` on APFS is case- and Unicode-normalization-insensitive, so a
    # rename that changes only case (`Kitchen-Layout.JPG` -> `.jpg`) or only
    # NFD->NFC reports a collision against the very file being renamed, and the
    # file ends up with a pointless `-<hash>` suffix. Compare inodes rather than
    # paths: if the "collision" is this same file, the rename is a no-op that
    # macOS performs in place.
    naive_target = directory / target_name
    if naive_target != source and _is_same_file(naive_target, source):
        try:
            os.rename(source, naive_target)  # same inode: this cannot clobber
            return naive_target
        except OSError:
            return source

    resolved_name = collision_free_name(directory, target_name, disambiguator=disambiguator)
    target = directory / resolved_name
    if target == source:
        return source

    if not _link_rename(source, target):
        _reserve_rename(source, target)
    return target
