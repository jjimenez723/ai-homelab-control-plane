"""The naming gate: the only code that decides a file gets a new name.

The order of the checks is the order of the invariants, and it matters:

1. A person's name wins over any model output, forever (invariant 12).
2. A name we already settled on is not reconsidered (idempotency).
3. A useful name is left alone (invariant 7).
4. Only then is a proposal asked for -- and what comes back is a string.
5. `policy.validate_proposal` makes it legal, keeps the real extension, and
   refuses anything that would not actually be an improvement.
6. Dry run stops here. Otherwise `policy.safe_rename` performs the one
   mutation, collision-safely, and it cannot overwrite anything.

Every branch -- including every decision *not* to rename -- is appended to the
naming ledger. That is what keeps repeat runs cheap and lets a human see why a
file is called what it is called.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from tools.ingest.identity import short_hash
from tools.ingest.ledger import (
    DECISION_ALREADY_NAMED,
    DECISION_HUMAN_LOCKED,
    DECISION_KEPT,
    DECISION_LOW_CONFIDENCE,
    DECISION_NO_PROPOSAL,
    DECISION_PROPOSED,
    DECISION_REJECTED_POLICY,
    DECISION_RENAME_FAILED,
    DECISION_RENAMED,
    NAMING_STATE_HUMAN,
    NAMING_STATE_KEPT,
    NAMING_STATE_NAMED,
    NAMING_STATE_REVIEW,
    LedgerTables,
)
from tools.ingest.naming import policy, quality
from tools.ingest.naming.evidence import NamingEvidence
from tools.ingest.naming.proposer import NameProposer


@dataclass(frozen=True)
class NamingOutcome:
    decision: str
    path: Path
    original_filename: str
    proposed_filename: str | None = None
    applied_filename: str | None = None
    name_quality: str = quality.QUALITY_GOOD
    confidence: float | None = None
    reason: str = ""
    model: str | None = None
    model_version: str | None = None

    @property
    def renamed(self) -> bool:
        return self.decision == DECISION_RENAMED

    @property
    def naming_state(self) -> str:
        return {
            DECISION_RENAMED: NAMING_STATE_NAMED,
            DECISION_KEPT: NAMING_STATE_KEPT,
            DECISION_ALREADY_NAMED: NAMING_STATE_NAMED,
            DECISION_HUMAN_LOCKED: NAMING_STATE_HUMAN,
        }.get(self.decision, NAMING_STATE_REVIEW)


def evaluate_and_apply(
    ledger: LedgerTables,
    *,
    local_file_id: str,
    path: Path,
    evidence: NamingEvidence,
    proposer: NameProposer,
    source_volume_id: str,
    source_class: str,
    sha256: str,
    now: str,
    apply: bool,
    min_confidence: float,
    max_filename_length: int = 120,
    protected_extensions: frozenset[str] = frozenset(),
    vocabulary: tuple[str, ...] = (),
) -> NamingOutcome:
    """Decide this file's name and, when `apply` is set, make it so."""
    original = path.name
    directory = path.parent

    def record(outcome: NamingOutcome, *, approved_by: str = "automatic", human_corrected: int = 0) -> NamingOutcome:
        ledger.append_naming(
            {
                "local_file_id": local_file_id,
                "asset_id": None,
                "source_volume_id": source_volume_id,
                "source_class": source_class,
                "directory": str(directory),
                "sha256": sha256,
                "original_filename": outcome.original_filename,
                "proposed_filename": outcome.proposed_filename,
                "applied_filename": outcome.applied_filename,
                "name_quality": outcome.name_quality,
                "decision": outcome.decision,
                "reason": outcome.reason,
                "model": outcome.model,
                "model_version": outcome.model_version,
                "confidence": outcome.confidence,
                "approved_by": approved_by,
                "human_corrected": human_corrected,
                "policy_outcome": outcome.decision,
                "created_at": now,
            }
        )
        return outcome

    # 1. A person has named this file. Nothing automatic touches it again.
    if ledger.has_human_naming(local_file_id):
        return record(
            NamingOutcome(
                decision=DECISION_HUMAN_LOCKED,
                path=path,
                original_filename=original,
                name_quality="human",
                reason="an owner-authored name outranks any later model output",
            ),
            approved_by="human",
            human_corrected=1,
        )

    # 2. A name this system already chose, or already chose to keep. Return
    #    without writing: re-recording an unchanged decision every scan would
    #    grow the ledger without adding information.
    if original in ledger.known_names(local_file_id):
        return NamingOutcome(
            decision=DECISION_ALREADY_NAMED,
            path=path,
            original_filename=original,
            applied_filename=original,
            reason="filename already settled by an earlier run",
        )

    # Some extensions are load-bearing: an app bundle or an installer is found
    # by name by something else on the system. Never rename those.
    if path.suffix.lower() in protected_extensions:
        return record(
            NamingOutcome(
                decision=DECISION_KEPT,
                path=path,
                original_filename=original,
                name_quality=evidence.name_quality,
                reason=f"{path.suffix.lower()} is a protected extension; renaming it could break something",
            )
        )

    # 3. The existing name is useful. Leave it alone.
    if evidence.name_quality == quality.QUALITY_GOOD:
        return record(
            NamingOutcome(
                decision=DECISION_KEPT,
                path=path,
                original_filename=original,
                name_quality=evidence.name_quality,
                reason=evidence.name_quality_reason,
                model=proposer.model,
                model_version=proposer.model_version,
            )
        )

    # A question this proposer version already answered is not re-asked. A
    # proposer *upgrade* deliberately reopens it.
    if ledger.evaluated_by(local_file_id, original, proposer.model_version):
        return NamingOutcome(
            decision=DECISION_ALREADY_NAMED,
            path=path,
            original_filename=original,
            name_quality=evidence.name_quality,
            reason=f"already evaluated by {proposer.model} {proposer.model_version}",
        )

    # 4. Ask for a proposal. The only model participation in the pipeline, and
    #    it receives `evidence`: plain data, no filesystem handle.
    proposal = proposer.propose(evidence)
    if proposal is None:
        return record(
            NamingOutcome(
                decision=DECISION_NO_PROPOSAL,
                path=path,
                original_filename=original,
                name_quality=evidence.name_quality,
                reason="the proposer had nothing factual to say about this file",
                model=proposer.model,
                model_version=proposer.model_version,
            )
        )

    base = NamingOutcome(
        decision=DECISION_NO_PROPOSAL,
        path=path,
        original_filename=original,
        proposed_filename=proposal.proposed_filename,
        name_quality=evidence.name_quality,
        confidence=proposal.confidence,
        reason=proposal.reason,
        model=proposal.model,
        model_version=proposal.model_version,
    )

    if proposal.confidence < min_confidence:
        return record(
            replace(
                base,
                decision=DECISION_LOW_CONFIDENCE,
                reason=f"confidence {proposal.confidence:.2f} below threshold {min_confidence:.2f}",
            )
        )

    # 5. Make the string legal, keep the real extension, and require that the
    #    result is actually better than what the file is called now.
    try:
        validated = policy.validate_proposal(
            proposal.proposed_filename,
            original_filename=original,
            max_filename_length=max_filename_length,
            vocabulary=vocabulary,
        )
    except policy.NamePolicyError as exc:
        return record(replace(base, decision=DECISION_REJECTED_POLICY, reason=f"filename policy rejected the proposal: {exc}"))

    if not validated.changed:
        return record(replace(base, decision=DECISION_KEPT, reason="proposal matches the existing filename"))

    if not apply:
        return record(replace(base, decision=DECISION_PROPOSED, proposed_filename=validated.filename, reason=validated.reason))

    # 6. The one mutation. Collision-safe; it cannot overwrite anything, and it
    #    takes a name rather than a path so it cannot relocate the file either.
    try:
        destination = policy.safe_rename(
            path, validated.filename, disambiguator=short_hash(sha256 or str(path), 6)
        )
    except (policy.NamePolicyError, OSError) as exc:
        return record(
            replace(
                base,
                decision=DECISION_RENAME_FAILED,
                proposed_filename=validated.filename,
                reason=f"rename failed, file left untouched: {exc}",
            )
        )

    return record(
        NamingOutcome(
            decision=DECISION_RENAMED,
            path=destination,
            original_filename=original,
            proposed_filename=validated.filename,
            applied_filename=destination.name,
            name_quality=evidence.name_quality,
            confidence=proposal.confidence,
            reason=proposal.reason,
            model=proposal.model,
            model_version=proposal.model_version,
        )
    )
