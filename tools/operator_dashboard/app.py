"""Local Streamlit operator workflow; launch only through dashboard_cli."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

from tools.naming_lab.preview import image_preview_png, pdf_first_page_png, text_excerpt, video_frames_preview_png
from tools.operator_dashboard.canary import (
    CanaryError, CorpusStore, FrozenCanaryStore, HumanTruth, TruthStore,
    blind_review_payload, deterministic_select,
)
from tools.operator_dashboard.taxonomy import DOCUMENT_MEDIA_TYPES, REASON_CODES


def _arg(name: str) -> str:
    args = sys.argv[1:]
    if name not in args:
        st.error(f"Launch through angel-operator-dashboard with {name}."); st.stop()
    return args[args.index(name) + 1]


def _show_preview(store: CorpusStore, asset) -> None:
    path = store.local_path_for(asset.content_sha256)
    if path is None or not path.exists():
        st.info("No currently reachable local path is indexed. The asset identity and label remain valid.")
        return
    if asset.media_type == "image":
        preview = image_preview_png(path)
        if preview: st.image(preview)
    elif asset.media_type == "pdf":
        preview = pdf_first_page_png(path, sha256=asset.content_sha256, size=path.stat().st_size)
        if preview: st.image(preview)
    elif asset.media_type == "video":
        previews = video_frames_preview_png(path, sha256=asset.content_sha256, duration_seconds=None, count=4)
        if previews: st.image(previews, caption=[f"sample {index + 1}" for index in range(len(previews))])
    elif asset.media_type in {"text", "code_config"}:
        try: st.code(text_excerpt(path.read_text(encoding="utf-8", errors="replace"), max_chars=4000))
        except OSError: st.info("The local file could not be read.")
    else:
        st.caption("This type is reviewed from safe metadata/evidence summaries; documents and archives are never executed.")


def main() -> None:
    st.set_page_config(page_title="Angel Operator", layout="wide")
    state_root = Path(_arg("--state")); corpus_id = _arg("--corpus-id")
    corpus = CorpusStore(state_root / "corpora", corpus_id)
    try: metadata = corpus.metadata()
    except CanaryError as exc: st.error(str(exc)); return
    truth_store = TruthStore(state_root / "truth" / corpus_id)
    canaries = FrozenCanaryStore(state_root / "canaries")
    assets = corpus.all_assets(); locked = [asset for asset in assets if truth_store.locked(asset.content_sha256)]
    st.title("Angel operator review")
    st.caption(f"{metadata['corpus_kind']} · {metadata['purpose']} · {len(assets)} indexed assets")
    first, second, third = st.columns(3)
    first.metric("Reviewed / locked", f"{len(locked)} / {len(assets)}")
    second.metric("Remaining", len(assets) - len(locked))
    third.metric("Uncertain", sum(bool(truth_store.locked(a.content_sha256) and truth_store.locked(a.content_sha256).review_required) for a in assets))
    tab_overview, tab_freeze, tab_review = st.tabs(["Corpus", "Freeze 100-asset canary", "Blind review queue"])
    with tab_overview:
        st.dataframe([{"asset_id": asset.asset_id, "type": asset.media_type, "split": asset.split, "stratum": asset.stratum} for asset in assets], use_container_width=True)
        st.caption("Corpus metadata contains no source bytes. Local paths are a separate, non-portable index.")
    with tab_freeze:
        st.write("Freeze uses deterministic round-robin stratification (type, orientation, evidence strength and evidence flags), records a manifest digest, and selects exactly 100 identities.")
        seed = st.text_input("Selection seed", value="gate3-real-shoot-v1")
        canary_id = st.text_input("Canary ID", value=f"{corpus_id}-gate3")
        if st.button("Preview deterministic selection", disabled=len(assets) < 100):
            try:
                sample = deterministic_select(assets, seed=seed)
                st.dataframe([{"asset_id": item.asset_id, "stratum": item.stratum} for item in sample], use_container_width=True)
            except CanaryError as exc: st.error(str(exc))
        if st.button("Freeze this 100-asset canary", disabled=len(assets) < 100):
            try:
                frozen = corpus.freeze_gate3(canaries, canary_id=canary_id, seed=seed)
                st.success(f"Frozen {len(frozen.entries)} content identities. Manifest digest: {frozen.manifest_sha256}")
            except CanaryError as exc: st.error(str(exc))
    with tab_review:
        remaining = [asset for asset in assets if truth_store.locked(asset.content_sha256) is None]
        if not remaining: st.success("All indexed assets are ground-truth locked.")
        show_locked = st.checkbox("Show already locked items", value=not remaining)
        queue = assets if show_locked else remaining
        if not queue: return
        asset = st.selectbox("Asset", queue, format_func=lambda item: f"{item.original_filename} · {item.content_sha256[:12]}")
        existing_truth = truth_store.locked(asset.content_sha256)
        left, right = st.columns([1, 1])
        with left:
            st.subheader("Preview and trustworthy evidence")
            _show_preview(corpus, asset)
            st.json({"content_sha256": asset.content_sha256, "media_type": asset.media_type, "evidence_flags": asset.evidence_flags, "orientation": asset.orientation})
        with right:
            st.subheader("Human truth — blind first")
            if existing_truth is not None:
                payload = blind_review_payload(asset, truth=existing_truth, model_result=corpus.model_result_for(asset.content_sha256), evidence={})
                st.success("Ground truth is locked. This is the post-lock comparison view.")
                st.json({"truth": payload["truth"], "model_comparison": payload["model_comparison"] or "No indexed model result."})
                return
            st.caption("No proposal or model comparison is shown until this lock is committed.")
            with st.form("human-truth"):
                naming_action = st.selectbox("Naming action", ["KEEP", "RENAME", "UNSURE"])
                preferred = st.text_input("Preferred filename (if rename)")
                accepted = st.text_input("Other acceptable filenames (comma-separated)")
                client_id = st.text_input("Canonical client ID (optional/UNKNOWN)") or None
                project_id = st.text_input("Canonical project ID (optional/UNKNOWN)") or None
                subject = st.text_input("Subject / description") or None
                tags = st.text_input("Normalized tags (comma-separated)")
                confidence = st.slider("Human confidence", 0.0, 1.0, .8)
                review_required = st.checkbox("Ambiguous or review required")
                reasons = st.multiselect("Reason codes", sorted(REASON_CODES))
                notes = st.text_area("Optional reviewer notes") or None
                submitted = st.form_submit_button("Lock human ground truth")
            if submitted:
                try:
                    truth_store.lock(HumanTruth(
                        content_sha256=asset.content_sha256, corpus_id=corpus_id, split=asset.split,
                        naming_action=naming_action, preferred_filename=preferred or None,
                        accepted_filenames=tuple(part.strip() for part in accepted.split(",") if part.strip()),
                        required_semantics=(), forbidden_semantics=(), semantic_date=None,
                        client_id=client_id, project_id=project_id, subject=subject,
                        tags=tuple(part.strip() for part in tags.split(",") if part.strip()), media_type=asset.media_type,
                        review_required=review_required, human_confidence=confidence, additional_context_required=False,
                        reason_codes=tuple(reasons), reviewer_notes=notes, annotator_id="local-operator", created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                    ))
                    st.success("Ground truth locked; model comparison can now be safely revealed when a local result is available.")
                except CanaryError as exc: st.error(str(exc))


if __name__ == "__main__":
    main()
