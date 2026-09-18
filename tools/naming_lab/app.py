"""Angel Naming Lab: the local Streamlit UI. See `docs/23-naming-lab.md`.

Launched only via `tools.naming_lab.cli` (or
`./scripts/angel-eval naming-lab --corpus ...`), which hard-binds this
process to `127.0.0.1`. Running `streamlit run tools/naming_lab/app.py`
directly bypasses that binding and must never be documented as a supported
way to start the Lab.

Every model run and every write to a seed happens only in response to an
explicit button click in this file -- nothing here runs automatically on
page load beyond measuring the currently displayed file's evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

from tools.campaign.manifest import load_manifest
from tools.campaign.profiles import manifest_path_for_profile
from tools.ingest.config import MAX_VIDEO_FRAME_COUNT, EvidenceConfig, OCRConfig, VisionConfig
from tools.naming_lab.evidence_bridge import LabEvidence, gather_lab_evidence
from tools.naming_lab.matrix import build_matrix
from tools.naming_lab.preview import (
    can_reveal_in_finder,
    image_preview_png,
    pdf_first_page_png,
    reveal_in_finder,
    text_excerpt,
    video_frames_preview_png,
)
from tools.naming_lab.prompts_lab import PromptDraftStore, load_lab_registry, save_draft, starter_vnext_draft
from tools.naming_lab.runner import run_candidate
from tools.naming_lab.seeds import SeedRecord, SeedStore, corpus_id_for
from tools.naming_lab.store import DecisionStore, ResultStore, record_human_decision

STATE_ROOT = Path(".state/naming-lab")
NAMING_CAPABILITY = "naming.semantic"
DEFAULT_MODELS = ("qwen3.5:4b", "qwen3.5:9b")


def _corpus_arg() -> Path:
    argv = sys.argv[1:]
    if "--corpus" in argv:
        return Path(argv[argv.index("--corpus") + 1]).expanduser().resolve()
    st.error("Launch via `./scripts/angel-eval naming-lab --corpus <dir>` -- no --corpus was given.")
    st.stop()
    raise SystemExit(2)  # unreachable; st.stop() halts the script


def _scan_corpus(corpus: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(corpus).parts):
            continue
        files.append(path)
    return files


@st.cache_data(show_spinner="Measuring file...")
def _cached_lab_evidence(
    path_str: str, _mtime: float, ocr_backend: str, vision_backend: str, video_frame_count: int,
) -> LabEvidence:
    evidence_config = EvidenceConfig(
        ocr=OCRConfig(backend=ocr_backend),
        vision=VisionConfig(backend=vision_backend, video_frame_count=video_frame_count),
    )
    return gather_lab_evidence(Path(path_str), evidence_config=evidence_config)


def _lab_evidence_for(path: Path, ocr_backend: str, vision_backend: str, video_frame_count: int) -> LabEvidence:
    return _cached_lab_evidence(str(path), path.stat().st_mtime, ocr_backend, vision_backend, video_frame_count)


def _seed_for(seed_store: SeedStore, path: Path, lab_evidence: LabEvidence) -> SeedRecord:
    seed_store.record_local_path(lab_evidence.sha256, path)
    return seed_store.get_or_create(
        sha256=lab_evidence.sha256,
        original_filename=path.name,
        media_kind=lab_evidence.naming_evidence.media_kind,
        extension=lab_evidence.naming_evidence.extension,
        semantic_date=lab_evidence.semantic_date,
    )


def _gate2_candidate_models() -> list[str]:
    """The local Ollama candidates already configured for Gate 2 (spec section 11)."""
    try:
        manifest = load_manifest(manifest_path_for_profile("gate2"))
    except Exception:  # noqa: BLE001 -- a missing/invalid manifest just means "no default list"
        return []
    return [candidate.model for candidate in manifest.candidates if candidate.runtime == "ollama"]


def main() -> None:
    st.set_page_config(page_title="Angel Naming Lab", layout="wide")
    corpus = _corpus_arg()
    corpus_id = corpus_id_for(corpus)
    corpus_root = STATE_ROOT / corpus_id
    seed_store = SeedStore(corpus_root)
    result_store = ResultStore(corpus_root)
    decision_store = DecisionStore(corpus_root)
    prompt_store = PromptDraftStore(corpus_root / "prompts")

    files = _scan_corpus(corpus)

    st.sidebar.title("Angel Naming Lab")
    st.sidebar.caption(f"Corpus: `{corpus}` ({len(files)} files)")
    st.sidebar.caption(
        "Every file reviewed here becomes a **development_seed** -- prompt-development "
        "supervision, never clean Gate-2 validation or frozen-holdout evidence."
    )
    view = st.sidebar.radio("View", ["Review", "Matrix"])

    st.sidebar.subheader("Evidence backends")
    ocr_backend = st.sidebar.selectbox("OCR backend", ["null", "tesseract"], index=0)
    vision_backend = st.sidebar.selectbox("Vision backend", ["null", "heuristic", "ollama"], index=1)
    video_frame_count = st.sidebar.number_input(
        "Video frames to sample", min_value=1, max_value=MAX_VIDEO_FRAME_COUNT,
        value=EvidenceConfig().vision.video_frame_count, step=1,
        help=(
            "How many evenly-spaced frames a video gets split into for vision "
            "and OCR. More frames catch a subject that only appears partway "
            "through a clip, at the cost of one backend call (and one OCR pass, "
            "if enabled) per frame -- tune this for your corpus's ROI. "
            "Near-identical frames are collapsed to one representative before "
            "the backends run, so a static clip costs one call however many "
            "frames are sampled."
        ),
    )

    registry = load_lab_registry(prompt_store)
    naming_prompts = [p for p in registry.all() if p.capability == NAMING_CAPABILITY]
    prompt_options = {f"{p.prompt_id}@{p.version}": p for p in (naming_prompts or registry.all())}
    st.sidebar.subheader("Prompt / model selection")
    selected_prompt_keys = st.sidebar.multiselect(
        "Prompt version(s) to run", list(prompt_options), default=list(prompt_options)[:1],
    )
    model_options = _gate2_candidate_models() or list(DEFAULT_MODELS)
    selected_models = st.sidebar.multiselect("Model(s) to run", model_options, default=model_options[:1])

    if not files:
        st.warning("No files found in the corpus directory.")
        return

    if view == "Review":
        _render_review(
            files=files, corpus=corpus, seed_store=seed_store, result_store=result_store,
            decision_store=decision_store, prompt_store=prompt_store, prompt_options=prompt_options,
            selected_prompt_keys=selected_prompt_keys, selected_models=selected_models,
            ocr_backend=ocr_backend, vision_backend=vision_backend, video_frame_count=int(video_frame_count),
        )
    else:
        _render_matrix(seed_store=seed_store, result_store=result_store)


def _render_review(
    *, files, corpus, seed_store, result_store, decision_store, prompt_store,
    prompt_options, selected_prompt_keys, selected_models, ocr_backend, vision_backend, video_frame_count,
) -> None:
    if "file_index" not in st.session_state:
        st.session_state.file_index = 0
    st.session_state.file_index = max(0, min(st.session_state.file_index, len(files) - 1))

    nav_prev, nav_pos, nav_next = st.columns([1, 4, 1])
    with nav_prev:
        if st.button("< Prev", disabled=st.session_state.file_index == 0):
            st.session_state.file_index -= 1
            st.rerun()
    with nav_next:
        if st.button("Next >", disabled=st.session_state.file_index >= len(files) - 1):
            st.session_state.file_index += 1
            st.rerun()
    path = files[st.session_state.file_index]
    with nav_pos:
        st.write(f"File {st.session_state.file_index + 1} / {len(files)}: `{path.relative_to(corpus)}`")

    lab_evidence = _lab_evidence_for(path, ocr_backend, vision_backend, video_frame_count)
    seed = _seed_for(seed_store, path, lab_evidence)

    left, center, right = st.columns([2, 3, 2])

    with left:
        st.subheader("Preview")
        naming_evidence = lab_evidence.naming_evidence
        if naming_evidence.media_kind == "image":
            png = image_preview_png(path)
            if png:
                st.image(png)
        elif path.suffix.lower() == ".pdf":
            png = pdf_first_page_png(path, sha256=lab_evidence.sha256, size=naming_evidence.size)
            if png:
                st.image(png)
        elif naming_evidence.media_kind == "video":
            frame_pngs = video_frames_preview_png(
                path, sha256=lab_evidence.sha256, duration_seconds=naming_evidence.fact("duration_seconds"),
                count=video_frame_count,
            )
            if frame_pngs:
                st.image(frame_pngs, caption=[f"frame {i + 1}" for i in range(len(frame_pngs))])
                st.caption(f"{len(frame_pngs)} sampled frame(s) -- the same ones vision/OCR were shown")
            else:
                st.caption("Could not sample any frames (ffmpeg not installed, or the video could not be read).")
        elif lab_evidence.combined_text:
            st.text(text_excerpt(lab_evidence.combined_text, max_chars=800))
        else:
            st.caption("No in-Lab preview for this file type.")
        if can_reveal_in_finder() and st.button("Reveal in Finder"):
            reveal_in_finder(path)
        st.caption(f"Original filename: `{path.name}`")
        st.caption(f"Type: {naming_evidence.media_kind}  |  SHA-256: `{lab_evidence.sha256[:16]}...`")

    with center:
        st.subheader("Evidence")
        st.json({
            "name_quality": naming_evidence.name_quality,
            "name_quality_reason": naming_evidence.name_quality_reason,
            "capture_date": naming_evidence.capture_date,
            "modified_date (filesystem, not semantic)": naming_evidence.modified_date,
        })
        if lab_evidence.semantic_date:
            candidate = lab_evidence.semantic_date
            st.success(
                f"semantic_date: **{candidate.value}** "
                f"(kind={candidate.kind}, source={candidate.source}, confidence={candidate.confidence})"
            )
        else:
            st.info("No trustworthy semantic date found -- no date prefix will be added.")
        if lab_evidence.document_date_candidates:
            st.caption("All document-date candidates found (best first):")
            for candidate in lab_evidence.document_date_candidates:
                st.caption(f"- {candidate.kind}: {candidate.value} (confidence={candidate.confidence})")
        if lab_evidence.visual_frames:
            frames = lab_evidence.visual_frames
            first = frames[0]
            st.caption(f"Vision ({first.backend}/{first.model}): {len(frames)} frame(s) described")
            if len(frames) == 1:
                st.write(first.description or "*(no description)*")
                if first.entities:
                    st.caption("Entities: " + ", ".join(first.entities))
            else:
                for index, visual in enumerate(frames, start=1):
                    st.write(f"**Frame {index}** ({visual.image_kind}): {visual.description or '*(no description)*'}")
                    if visual.entities:
                        st.caption(f"Frame {index} entities: " + ", ".join(visual.entities))
        if lab_evidence.combined_text:
            with st.expander("Extracted text (native + OCR)"):
                st.text(text_excerpt(lab_evidence.combined_text, max_chars=4000))

        st.subheader("Model runs")
        scope = st.radio("Run scope", ["This file", "Selected files", "All development seeds"], horizontal=True)
        target_paths: list[Path] = []
        if scope == "This file":
            target_paths = [path]
        elif scope == "Selected files":
            relative_options = [str(f.relative_to(corpus)) for f in files]
            chosen = st.multiselect(
                "Files to run", relative_options, default=[str(path.relative_to(corpus))],
            )
            target_paths = [corpus / choice for choice in chosen]
        else:
            for existing in seed_store.all():
                local = seed_store.local_path_for(existing.sha256)
                if local is not None and local.exists():
                    target_paths.append(local)
            if not target_paths:
                st.caption("No development seeds recorded yet -- visit files in Review to create seeds.")

        run_label = f"Run {len(selected_prompt_keys)} prompt(s) x {len(selected_models)} model(s) over {len(target_paths)} file(s)"
        if st.button(run_label, disabled=not (selected_prompt_keys and selected_models and target_paths)):
            prompts = [prompt_options[key] for key in selected_prompt_keys]
            new_results = []
            for target_path in target_paths:
                target_evidence = (
                    lab_evidence if target_path == path
                    else _lab_evidence_for(target_path, ocr_backend, vision_backend, video_frame_count)
                )
                target_seed = _seed_for(seed_store, target_path, target_evidence)
                for prompt in prompts:
                    for model in selected_models:
                        new_results.append(run_candidate(target_seed, target_evidence, prompt=prompt, model_name=model))
            result_store.append_all(new_results)
            st.success(f"Ran {len(new_results)} combination(s) across {len(target_paths)} file(s).")
            st.rerun()

        existing_results = result_store.for_seed(seed.sha256)
        st.caption(f"{len(existing_results)} run(s) recorded for this file.")
        for result in existing_results:
            label = f"{result.prompt_id}@{result.prompt_version} / {result.model_name} -- {result.created_at}"
            with st.expander(label):
                if result.error:
                    st.error(result.error)
                    continue
                st.code(result.raw_output or "", language=None)
                st.write("Deterministic filename:", f"`{result.deterministic_filename}`")
                raw_ok = result.raw_compliance.passed if result.raw_compliance else False
                raw_reasons = "; ".join(result.raw_compliance.reasons) if result.raw_compliance else ""
                st.write("Raw compliance:", "PASS" if raw_ok else f"FAIL -- {raw_reasons}")
                e2e_ok = bool(result.end_to_end and result.end_to_end.passed)
                e2e_detail = result.end_to_end.error if (result.end_to_end and result.end_to_end.error) else ""
                st.write("End-to-end (policy) result:", "PASS" if e2e_ok else f"FAIL -- {e2e_detail}")
                semantic_ok = bool(result.semantic and result.semantic.passed)
                st.write("Semantic/evidence result:", "PASS" if semantic_ok else "FAIL")
                if result.hallucination and (result.hallucination.unsupported_date or result.hallucination.unsupported_entities):
                    st.warning(
                        "Heuristic hallucination flags (for review, not a gate): "
                        f"unsupported_date={result.hallucination.unsupported_date}, "
                        f"unsupported_entities={result.hallucination.unsupported_entities}"
                    )
                if result.deterministic_filename:
                    button_key = f"accept-{result.prompt_id}-{result.prompt_version}-{result.model_name}-{result.created_at}"
                    if st.button("Accept this as an alternate", key=button_key):
                        record_human_decision(
                            seed_store, decision_store, seed, decision="accept_alternate",
                            filename=result.deterministic_filename,
                        )
                        st.rerun()

    with right:
        st.subheader("Human review")
        st.write("Accepted filenames so far:")
        if not seed.accepted_filenames:
            st.caption("(none yet)")
        for name in seed.accepted_filenames:
            st.code(name, language=None)
        manual_name = st.text_input("Add a filename you would personally accept")
        if st.button("Accept") and manual_name.strip():
            record_human_decision(seed_store, decision_store, seed, decision="accept", filename=manual_name.strip())
            st.rerun()
        notes = st.text_area("Rejection / review notes", value=seed.human_notes or "")
        if st.button("Reject / save notes"):
            record_human_decision(seed_store, decision_store, seed, decision="reject", filename=None, notes=notes)
            st.rerun()

        st.subheader("Prompt draft")
        base_key = st.selectbox("Base prompt", list(prompt_options), key="draft-base")
        base_prompt = prompt_options[base_key]
        starter_schema = starter_vnext_draft(base_prompt).output_schema
        edited_task = st.text_area("task_template", value=base_prompt.task_template, height=120, key="draft-task")
        edited_schema = st.text_area("output_schema", value=starter_schema, height=100, key="draft-schema")
        st.caption(
            "Saving never edits the base prompt -- it always allocates a new "
            "`naming-lab-draft-*` version (spec section 10)."
        )
        if st.button("Save as new draft version"):
            saved = save_draft(prompt_store, base_prompt, task_template=edited_task, output_schema=edited_schema)
            st.success(f"Saved {saved.prompt_id}@{saved.version}")
            st.rerun()


def _render_matrix(*, seed_store: SeedStore, result_store: ResultStore) -> None:
    st.subheader("Prompt x Model x File matrix")
    seeds = seed_store.all()
    results = result_store.all()
    if not results:
        st.info("No runs recorded yet. Run prompt/model combinations from the Review screen first.")
        return
    matrix = build_matrix(seeds, results)

    st.write("### Aggregate metrics (numerator / denominator)")
    for rate in matrix.rates.values():
        value = f"{rate.value:.1%}" if rate.value is not None else "n/a"
        st.write(f"- **{rate.label}**: {value}  ({rate.numerator}/{rate.denominator})")

    st.write("### Cells")
    rows = [
        {
            "seed": cell.seed_sha256[:10],
            "prompt": f"{cell.prompt_id}@{cell.prompt_version}",
            "model": cell.model_name,
            "exact_accepted": cell.exact_accepted_match,
            "human_alt": cell.human_approved_alternate,
            "semantic_pass": cell.deterministic_semantic_pass,
            "raw_compliance": cell.raw_compliance_pass,
            "end_to_end": cell.end_to_end_pass,
            "error": cell.error or "",
        }
        for cell in matrix.cells
    ]
    st.dataframe(rows, use_container_width=True)


if __name__ == "__main__":
    main()
