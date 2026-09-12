"""Deciding whether a file should enter the durable Angel library.

Three outcomes, and only one of them ever moves a file:

* ``IGNORE``  -- not for Angel. The file stays exactly where it is.
* ``REVIEW``  -- we cannot tell. The file stays where it is and appears in the
  review report.
* ``INGEST``  -- a candidate. It is still only transferred if the owner has
  switched `triage.auto_ingest` on *and* confidence clears the threshold;
  otherwise it too is reported and left alone.

The bias is deliberate. A false `REVIEW` costs the owner a glance at a report.
A false `INGEST` puts junk in a permanent library that restic then backs up
forever, and a false `IGNORE` on an auto-ingest run costs nothing at all
because nothing is deleted either way. So: when in doubt, `REVIEW`.

Every rule here is deterministic and reads only measured facts. A model may
later contribute semantic evidence to this decision; it will contribute a
`recommendation` and a `reason`, not an action.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.ingest.config import TriageConfig
from tools.ingest.ledger import RETENTION_IGNORE, RETENTION_INGEST, RETENTION_REVIEW
from tools.ingest.media.probe import (
    MEDIA_ARCHIVE,
    MEDIA_AUDIO,
    MEDIA_DOCUMENT,
    MEDIA_IMAGE,
    MEDIA_INSTALLER,
    MEDIA_TEXT,
    MEDIA_VIDEO,
)
from tools.ingest.naming.evidence import NamingEvidence
from tools.ingest.naming.quality import QUALITY_GOOD

TRIAGE_MODEL = "deterministic-triage"
TRIAGE_MODEL_VERSION = "1"

# Source classes whose files are, by their nature, worth keeping: a camera card
# exists to be emptied into a library. Downloads and screenshots are not.
_ARCHIVAL_SOURCE_CLASSES = frozenset({"sd"})


@dataclass(frozen=True)
class RetentionDecision:
    recommendation: str
    confidence: float
    reason: str
    model: str = TRIAGE_MODEL
    model_version: str = TRIAGE_MODEL_VERSION
    tags: tuple[str, ...] = field(default_factory=tuple)
    # True only for a decision a person made. Kept distinct from confidence so
    # a model can never reach this state by scoring itself 1.0.
    human_decided: bool = False

    @property
    def is_ingest(self) -> bool:
        return self.recommendation == RETENTION_INGEST


def triage(
    evidence: NamingEvidence,
    *,
    config: TriageConfig,
    duplicate_of_asset: str | None = None,
    duplicate_of_local: str | None = None,
    owner_named: bool = False,
) -> RetentionDecision:
    """Classify one file for retention. Reads facts; touches nothing.

    `owner_named` is whether the filename the file *arrived with* was already
    meaningful -- that is, a person chose it. It is deliberately separate from
    `evidence.name_quality`, which after a rename describes a name this
    pipeline generated. Treating the two alike would let the system argue its
    own output into the durable library.
    """
    extension = evidence.extension
    size = evidence.size

    # -- unambiguous ignores ------------------------------------------------

    if extension in config.ignore_extensions:
        return RetentionDecision(RETENTION_IGNORE, 0.97, f"{extension} is a transient/partial artifact", tags=("transient",))

    if extension in config.installer_extensions or evidence.media_kind == MEDIA_INSTALLER:
        return RetentionDecision(
            RETENTION_IGNORE, 0.95,
            "installer/disk image: re-downloadable, and not a record of anything",
            tags=("installer",),
        )

    if duplicate_of_asset is not None:
        return RetentionDecision(
            RETENTION_IGNORE, 0.96,
            f"identical bytes are already in Angel as {duplicate_of_asset}",
            tags=("duplicate", "already-in-angel"),
        )

    if size == 0:
        return RetentionDecision(RETENTION_IGNORE, 0.9, "zero-byte file", tags=("empty",))

    if size < config.min_interesting_bytes and evidence.media_kind in (MEDIA_IMAGE, MEDIA_VIDEO):
        return RetentionDecision(
            RETENTION_IGNORE, 0.8,
            f"{size} bytes: too small to be a real capture (icon, spacer, or fragment)",
            tags=("tiny",),
        )

    tags: list[str] = []
    if duplicate_of_local is not None:
        tags.append("duplicate-on-mac")

    # -- camera/removable media --------------------------------------------

    if evidence.source_class in _ARCHIVAL_SOURCE_CLASSES:
        if evidence.media_kind in (MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_AUDIO):
            return RetentionDecision(
                RETENTION_INGEST, 0.9,
                "capture from a registered removable source: this is what the library is for",
                tags=tuple(tags + ["capture"]),
            )
        return RetentionDecision(
            RETENTION_REVIEW, 0.6,
            "non-media file on a removable source",
            tags=tuple(tags),
        )

    # -- screenshots --------------------------------------------------------

    if evidence.is_screenshot or evidence.source_class == "screenshots":
        # Telling a disposable UI state from durable design reference needs to
        # read what is *in* the image. That is the worker layer, which does not
        # exist yet, so this reports rather than pretends.
        return RetentionDecision(
            RETENTION_REVIEW, 0.5,
            "screenshot: keeping it or not depends on what it shows, which needs OCR/vision",
            tags=tuple(tags + ["screenshot"]),
        )

    # -- documents ----------------------------------------------------------

    if extension in config.document_extensions or evidence.media_kind == MEDIA_DOCUMENT:
        # A meaningful filename is evidence of value *because a person typed
        # it*. A name this pipeline generated is not: counting it would let the
        # system promote its own output into the library by renaming it. So the
        # bar here is specifically the name the file arrived with.
        if owner_named:
            return RetentionDecision(
                RETENTION_INGEST, 0.78,
                "document with a meaningful, owner-recognisable filename",
                tags=tuple(tags + ["document"]),
            )
        if evidence.name_quality == QUALITY_GOOD:
            return RetentionDecision(
                RETENTION_REVIEW, 0.6,
                "document whose useful name this pipeline generated, not the owner: worth a look",
                tags=tuple(tags + ["document", "machine-named"]),
            )
        return RetentionDecision(
            RETENTION_REVIEW, 0.55,
            "document, but nothing establishes whether it matters",
            tags=tuple(tags + ["document"]),
        )

    # -- everything else ----------------------------------------------------

    if evidence.media_kind == MEDIA_ARCHIVE:
        return RetentionDecision(
            RETENTION_REVIEW, 0.5, "archive: contents decide this, and they are not unpacked here",
            tags=tuple(tags + ["archive"]),
        )

    if evidence.media_kind in (MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_AUDIO):
        return RetentionDecision(
            RETENTION_REVIEW, 0.5,
            f"{evidence.media_kind} in {evidence.source_class}: could be a keeper or could be junk",
            tags=tuple(tags + [evidence.media_kind]),
        )

    if evidence.media_kind == MEDIA_TEXT:
        return RetentionDecision(
            RETENTION_REVIEW, 0.5, "text/code file: often part of a project rather than an asset",
            tags=tuple(tags + ["text"]),
        )

    return RetentionDecision(
        RETENTION_REVIEW, 0.4, "unrecognised file type", tags=tuple(tags + ["unknown"])
    )


def may_auto_ingest(decision: RetentionDecision, config: TriageConfig) -> bool:
    """Whether policy permits acting on an INGEST recommendation.

    Two independent gates, because either one alone is too easy to trip: the
    owner must have switched auto-ingest on, *and* the decision must clear the
    confidence threshold. Off by default.

    A decision the owner made themselves (`human_decided`) passes both. That is
    not a loophole -- it is the point. `auto_ingest` governs how much the
    *system* may do unattended; it was never meant to stop a person acting on
    their own review queue, and a REVIEW the owner cannot act on is a dead end.
    """
    if decision.human_decided:
        return decision.recommendation == RETENTION_INGEST
    return (
        config.auto_ingest
        and decision.recommendation == RETENTION_INGEST
        and decision.confidence >= config.min_ingest_confidence
    )


def human_decision(recommendation: str, *, reason: str) -> RetentionDecision:
    """A retention decision the owner made. Outranks any model output."""
    if recommendation not in (RETENTION_IGNORE, RETENTION_REVIEW, RETENTION_INGEST):
        raise ValueError(f"unknown retention decision: {recommendation!r}")
    return RetentionDecision(
        recommendation=recommendation,
        confidence=1.0,
        reason=reason,
        model="human",
        model_version="-",
        tags=("human",),
        human_decided=True,
    )
