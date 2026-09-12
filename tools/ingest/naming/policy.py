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


def sanitize_stem(stem: str, *, max_length: int) -> str:
    stem = unicodedata.normalize("NFC", stem)
    stem = "".join(character for character in stem if character not in _CONTROL_CHARS)
    stem = "".join(character for character in stem if character not in _INVISIBLE_CHARS)
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
    stem = sanitize_stem(candidate, max_length=stem_budget)
    if not stem:
        raise NamePolicyError("proposal contains no usable characters")

    filename = f"{stem}{extension}"
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
    os.unlink(source)
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
    resolved_name = collision_free_name(directory, target_name, disambiguator=disambiguator)
    target = directory / resolved_name
    if target == source:
        return source

    if not _link_rename(source, target):
        _reserve_rename(source, target)
    return target
