"""Local Streamlit operator workflow; launch only through dashboard_cli."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import streamlit as st

from tools.naming_lab.preview import image_preview_png, pdf_first_page_png, text_excerpt, video_frames_preview_png
from tools.operator_dashboard.canary import (
    CanaryError, CorpusStore, FROZEN_SPLITS, FrozenCanaryStore, HumanTruth, TruthStore,
    blind_review_payload, deterministic_select,
)
from tools.operator_dashboard.taxonomy import REASON_CODES
from tools.semantic.manifests import ManifestRegistry


def _arg(name: str) -> str:
    args = sys.argv[1:]
    if name not in args or args.index(name) + 1 >= len(args):
        st.error(f"Launch through angel-operator-dashboard with {name}.")
        st.stop()
    return args[args.index(name) + 1]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _show_preview(store: CorpusStore, asset: Any) -> None:
    path = store.local_path_for(asset.content_sha256)
    if path is None or not path.exists():
        st.info("No currently reachable verified local path is indexed. The content identity and label remain valid.")
        return
    if asset.media_type == "image":
        preview = image_preview_png(path)
        if preview:
            st.image(preview)
    elif asset.media_type == "pdf":
        preview = pdf_first_page_png(path, sha256=asset.content_sha256, size=path.stat().st_size)
        if preview:
            st.image(preview)
    elif asset.media_type == "video":
        previews = video_frames_preview_png(
            path, sha256=asset.content_sha256, duration_seconds=None, count=4
        )
        if previews:
            st.image(previews, caption=[f"sample {index + 1}" for index in range(len(previews))])
    elif asset.media_type in {"text", "code_config"}:
        try:
            st.code(text_excerpt(path.read_text(encoding="utf-8", errors="replace"), max_chars=4000))
        except OSError:
            st.info("The local file could not be read.")
    else:
        st.caption("This type is reviewed from safe metadata/evidence summaries; documents and archives are never executed.")


def _identity_label(value: str | None, registry: ManifestRegistry, *, project: bool = False) -> str:
    if value is None:
        return "UNKNOWN / not established"
    item = registry.project(value) if project else registry.client(value)
    return f"{value} — {item.display_name}" if item is not None else value


def _truth_form(
    *,
    prefix: str,
    asset: Any,
    corpus_id: str,
    effective_split: str,
    registry: ManifestRegistry,
    annotator_id: str,
    existing: HumanTruth | None = None,
    correction: bool = False,
) -> tuple[bool, HumanTruth | None, str | None]:
    current = existing
    client_options = [None, *[item.client_id for item in registry.clients]]
    project_options = [None, *[item.project_id for item in registry.projects]]
    client_default = client_options.index(current.client_id) if current and current.client_id in client_options else 0
    project_default = project_options.index(current.project_id) if current and current.project_id in project_options else 0
    action_options = ["KEEP", "RENAME", "UNSURE"]
    action_default = action_options.index(current.naming_action) if current else 0
    current_date = (current.semantic_date or {}) if current else {}
    semantic_date_value = str(current_date.get("value", "")) if current_date else ""
    semantic_date_provenance = str(current_date.get("provenance", "unknown")) if current_date else "unknown"
    provenance_options = ["unknown", "metadata", "filename", "native_text", "ocr", "transcript", "operator_context"]
    if semantic_date_provenance not in provenance_options:
        provenance_options.append(semantic_date_provenance)

    with st.form(f"{prefix}-truth-form"):
        naming_action = st.selectbox("Naming action", action_options, index=action_default, key=f"{prefix}-action")
        preferred = st.text_input(
            "Preferred filename (required for RENAME)",
            value=(current.preferred_filename or "") if current else "",
            key=f"{prefix}-preferred",
        )
        accepted = st.text_input(
            "Other acceptable filenames (comma-separated)",
            value=", ".join(current.accepted_filenames) if current else "",
            key=f"{prefix}-accepted",
        )
        required = st.text_input(
            "Required semantics (comma-separated)",
            value=", ".join(current.required_semantics) if current else "",
            key=f"{prefix}-required",
        )
        forbidden = st.text_input(
            "Forbidden semantics (comma-separated)",
            value=", ".join(current.forbidden_semantics) if current else "",
            key=f"{prefix}-forbidden",
        )
        client_id = st.selectbox(
            "Canonical client",
            client_options,
            index=client_default,
            format_func=lambda value: _identity_label(value, registry),
            key=f"{prefix}-client",
        )
        project_id = st.selectbox(
            "Canonical project",
            project_options,
            index=project_default,
            format_func=lambda value: _identity_label(value, registry, project=True),
            key=f"{prefix}-project",
        )
        subject = st.text_input(
            "Subject / description",
            value=(current.subject or "") if current else "",
            key=f"{prefix}-subject",
        )
        tags = st.text_input(
            "Normalized tags (comma-separated)",
            value=", ".join(current.tags) if current else "",
            key=f"{prefix}-tags",
        )
        semantic_date_text = st.text_input(
            "Semantic date (optional)",
            value=semantic_date_value,
            key=f"{prefix}-date",
        )
        provenance = st.selectbox(
            "Semantic-date provenance",
            provenance_options,
            index=provenance_options.index(semantic_date_provenance),
            key=f"{prefix}-date-provenance",
        )
        confidence = st.slider(
            "Human confidence",
            0.0,
            1.0,
            float(current.human_confidence) if current else 0.8,
            key=f"{prefix}-confidence",
        )
        review_required = st.checkbox(
            "Ambiguous or review required",
            value=bool(current.review_required) if current else False,
            key=f"{prefix}-review",
        )
        additional_context = st.checkbox(
            "Additional external/project context is required",
            value=bool(current.additional_context_required) if current else False,
            key=f"{prefix}-context",
        )
        reasons = st.multiselect(
            "Reason codes",
            sorted(REASON_CODES),
            default=list(current.reason_codes) if current else [],
            key=f"{prefix}-reasons",
        )
        notes = st.text_area(
            "Reviewer notes",
            value=(current.reviewer_notes or "") if current else "",
            key=f"{prefix}-notes",
        )
        correction_reason = None
        if correction:
            correction_reason = st.text_input("Correction reason (required)", key=f"{prefix}-correction-reason")
        submitted = st.form_submit_button(
            "Append audited correction" if correction else "Lock human ground truth"
        )

    if not submitted:
        return False, None, correction_reason
    if not annotator_id.strip():
        st.error("Set an annotator ID in the sidebar before locking or correcting truth.")
        return True, None, correction_reason

    semantic_date = (
        {"value": semantic_date_text.strip(), "provenance": provenance}
        if semantic_date_text.strip()
        else None
    )
    truth = HumanTruth(
        content_sha256=asset.content_sha256,
        corpus_id=corpus_id,
        split=effective_split,
        naming_action=naming_action,
        preferred_filename=preferred.strip() or None,
        accepted_filenames=_csv(accepted),
        required_semantics=_csv(required),
        forbidden_semantics=_csv(forbidden),
        semantic_date=semantic_date,
        client_id=client_id,
        project_id=project_id,
        subject=subject.strip() or None,
        tags=_csv(tags),
        media_type=asset.media_type,
        review_required=review_required,
        human_confidence=confidence,
        additional_context_required=additional_context,
        reason_codes=tuple(reasons),
        reviewer_notes=notes.strip() or None,
        annotator_id=annotator_id.strip(),
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    return True, truth, correction_reason


def main() -> None:
    st.set_page_config(page_title="Angel Operator", layout="wide")
    state_root = Path(_arg("--state"))
    corpus_id = _arg("--corpus-id")
    clients_root = Path(_arg("--clients-root"))
    corpus = CorpusStore(state_root / "corpora", corpus_id)
    try:
        metadata = corpus.metadata()
    except CanaryError as exc:
        st.error(str(exc))
        return

    registry = ManifestRegistry.load(clients_root)
    truth_store = TruthStore(state_root / "truth" / corpus_id)
    canaries = FrozenCanaryStore(state_root / "canaries")
    assets = corpus.all_assets()
    locked = [asset for asset in assets if truth_store.locked(asset.content_sha256)]

    st.title("Angel operator review")
    st.caption(f"{metadata['corpus_kind']} · {metadata['purpose']} · {len(assets)} indexed assets")
    annotator_id = st.sidebar.text_input(
        "Annotator ID",
        help="Stable local/operator identifier recorded with every truth event.",
    )
    st.sidebar.caption(f"Canonical manifest root: {clients_root}")
    st.sidebar.caption(f"{len(registry.clients)} clients · {len(registry.projects)} projects loaded")

    first, second, third = st.columns(3)
    first.metric("Reviewed / locked", f"{len(locked)} / {len(assets)}")
    second.metric("Remaining", len(assets) - len(locked))
    third.metric(
        "Uncertain",
        sum(
            bool(
                truth_store.locked(a.content_sha256)
                and truth_store.locked(a.content_sha256).review_required
            )
            for a in assets
        ),
    )

    tab_overview, tab_freeze, tab_review = st.tabs(
        ["Corpus", "Freeze 100-asset canary", "Blind review queue"]
    )
    with tab_overview:
        st.dataframe(
            [
                {
                    "asset_id": asset.asset_id,
                    "type": asset.media_type,
                    "declared_split": asset.split,
                    "frozen_membership": canaries.contains(asset.content_sha256),
                    "stratum": asset.stratum,
                }
                for asset in assets
            ],
            use_container_width=True,
        )
        if metadata.get("sealed_canary_id"):
            st.success(
                f"Corpus sealed by {metadata['sealed_canary_id']} · "
                f"pool {metadata.get('candidate_pool_count')} · "
                f"digest {metadata.get('candidate_pool_sha256')}"
            )
        st.caption("Portable corpus metadata contains no source bytes. Local paths are verified and stored separately.")

    with tab_freeze:
        st.write(
            "Freeze selects exactly 100 identities, records a digest of the entire candidate pool, "
            "and seals descriptor membership so the sampling frame cannot drift afterward."
        )
        seed = st.text_input("Selection seed", value="gate3-real-shoot-v1")
        canary_id = st.text_input("Canary ID", value=f"{corpus_id}-gate3")
        disabled = len(assets) < 100 or bool(metadata.get("sealed_canary_id"))
        if st.button("Preview deterministic selection", disabled=disabled):
            try:
                sample = deterministic_select(assets, seed=seed)
                st.dataframe(
                    [{"asset_id": item.asset_id, "stratum": item.stratum} for item in sample],
                    use_container_width=True,
                )
            except CanaryError as exc:
                st.error(str(exc))
        if st.button("Freeze and seal this 100-asset canary", disabled=disabled):
            try:
                frozen = corpus.freeze_gate3(canaries, canary_id=canary_id, seed=seed)
                st.success(
                    f"Frozen {len(frozen.entries)} identities. Manifest {frozen.manifest_sha256}; "
                    f"candidate pool {frozen.candidate_pool_count} / {frozen.candidate_pool_sha256}."
                )
            except CanaryError as exc:
                st.error(str(exc))

    with tab_review:
        remaining = [asset for asset in assets if truth_store.locked(asset.content_sha256) is None]
        if not remaining:
            st.success("All indexed assets have a ground-truth lock.")
        show_locked = st.checkbox("Show already locked items", value=not remaining)
        queue = assets if show_locked else remaining
        if not queue:
            return

        asset = st.selectbox(
            "Asset",
            queue,
            format_func=lambda item: f"{item.original_filename} · {item.content_sha256[:12]}",
        )
        existing_truth = truth_store.locked(asset.content_sha256)
        is_frozen = canaries.contains(asset.content_sha256)
        effective_split = "frozen_canary" if is_frozen else asset.split
        is_holdout = is_frozen or asset.split in FROZEN_SPLITS

        left, right = st.columns([1, 1])
        with left:
            st.subheader("Preview and trustworthy evidence")
            _show_preview(corpus, asset)
            st.json(
                {
                    "content_sha256": asset.content_sha256,
                    "media_type": asset.media_type,
                    "evidence_flags": asset.evidence_flags,
                    "orientation": asset.orientation,
                    "effective_split": effective_split,
                }
            )

        with right:
            st.subheader("Human truth — blind first")
            if existing_truth is not None:
                payload = blind_review_payload(
                    asset,
                    truth=existing_truth,
                    model_result=corpus.model_result_for(asset.content_sha256),
                    evidence={},
                    reveal_model_comparison=not is_holdout,
                )
                st.success("Ground truth is locked.")
                st.json(payload["truth"])
                if is_holdout:
                    st.warning(
                        "Model comparison is withheld for frozen/general holdout content. "
                        "Use the explicit manifest-bound Gate 3 evaluation only after the candidate run is frozen."
                    )
                else:
                    st.subheader("Post-lock development comparison")
                    st.json(payload.get("model_comparison") or "No indexed model result.")

                with st.expander("Append an audited correction"):
                    submitted, replacement, correction_reason = _truth_form(
                        prefix=f"correct-{asset.content_sha256[:12]}",
                        asset=asset,
                        corpus_id=corpus_id,
                        effective_split=effective_split,
                        registry=registry,
                        annotator_id=annotator_id,
                        existing=existing_truth,
                        correction=True,
                    )
                    if submitted and replacement is not None:
                        try:
                            truth_store.correct(
                                replacement,
                                actor_id=annotator_id,
                                reason=correction_reason or "",
                                registry=registry,
                            )
                            st.success("Audited correction appended.")
                        except (CanaryError, ValueError) as exc:
                            st.error(str(exc))
                return

            st.caption("No proposal or model comparison is shown until this lock is committed.")
            submitted, truth, _ = _truth_form(
                prefix=f"lock-{asset.content_sha256[:12]}",
                asset=asset,
                corpus_id=corpus_id,
                effective_split=effective_split,
                registry=registry,
                annotator_id=annotator_id,
            )
            if submitted and truth is not None:
                try:
                    truth_store.lock(truth, registry=registry)
                    st.success("Ground truth locked. Holdout comparisons remain withheld.")
                except (CanaryError, ValueError) as exc:
                    st.error(str(exc))


if __name__ == "__main__":
    main()
