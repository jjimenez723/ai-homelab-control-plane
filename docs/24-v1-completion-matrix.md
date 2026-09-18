# 24 — Angel V1 completion matrix

Built 2026-09-17 at the start of the V1 completion pass (branch
`feat/angel-v1-completion`, base `36261ea` on `feat/gate2-naming-lab`, which
contains `origin/main` `a8ddfd7` plus three unpushed commits D-054..D-056).
Every row was classified from code, tests, and the live machine, not from
prior prose. Status vocabulary: COMPLETE, IMPLEMENTED-NOT-INTEGRATED,
IMPLEMENTED-NOT-VALIDATED, PARTIAL, MISSING, BLOCKED-EXTERNALLY, POST-V1.
The "after" column is filled in as work closes; the final state is the
closeout record for this pass.

## Observed starting state (2026-09-17)

| Fact | Value |
|---|---|
| Branch / HEAD | `feat/gate2-naming-lab` @ `36261ea`, clean |
| vs `origin/main` | 3 commits ahead (`cd1b2a3`, `3fcb740`, `36261ea`), 0 behind |
| Other local branches | `feat/versioned-prompt-registry` @ `2e65f07` — fully contained in `main` (stale, mergeable-nothing) |
| Test suite | `python -m unittest discover -s tests`: **938 tests, OK, 27.7 s** |
| `/Volumes/Angel` | mounted, APFS, UUID `14145145-…` matches `config/ingest.yaml`; **1.2 MB used of 1.8 TiB** — bootstrap scaffold, three eval JSONL files, one restic drill marker. No models, no repos, no ingested assets on it. |
| Ollama | 0.33.3 installed, **not running** (no brew service, no launchd); models on the internal disk at `~/.ollama/models` (24 GB: `qwen2.5:0.5b`, `qwen3.5:4b`, `qwen3.5:9b`, `gpt-oss:20b`, `moondream`). Angel `40-ai/model-cache/ollama/` is empty. |
| Internal disk | 36 GB free of 460 GB (92 %) — Phase-0 ≥ 60 GB target still unmet |
| launchd (ai-homelab) | `com.ai-homelab.t7-backup` (daily 06:15, last exit 1: Angel was unplugged 09-16/09-17), `com.ai-homelab.t7-check` loaded; file-intelligence agent **not installed** |
| Backup | last successful snapshot 2026-09-15, check 09-13, restore drill 09-12 |
| Gate-2 campaign | `gate2-validation-001` ran 2026-09-15 (660 records): best routing `qwen3.5:4b` 0.684 (threshold 0.80), best naming 0.214 (0.75); `gpt-oss:20b` **truncated on 95–100 % of items** (`num_predict: 64` against a reasoning model) — an infrastructure defect, not a model measurement |
| Mac-side repos | 16 working copies under `~/Documents` and `~/OpenMontage`, many dirty, one with no remote and no commits (`ChatGPT/TLC …`), one stale duplicate of this repo (`~/Documents/ai-homelab-control-plane`, fully in `origin/main`). `~/Documents` is Google-Drive-synced. |
| Tools | ffmpeg 8.1.2, ffprobe, tesseract 5.5.3, restic 0.19.1, rclone 1.75.1, uv; **no** whisper CLI, no `video-to-skill` binary |

## Matrix

| # | Capability | Before | After | Evidence / blocker |
|---|---|---|---|---|
| 1 | Transactional ingestion (copy→verify→register→unlink), T7 UUID gate, exclusions | COMPLETE | COMPLETE | `tools/ingest/transaction.py`, `tests/ingest/test_transaction.py`, `test_mutation_safety.py` |
| 2 | Deterministic naming (quality, policy, safe_rename, human lock, collision) | COMPLETE | COMPLETE | `tools/ingest/naming/`, `tests/ingest/test_naming.py`, `test_hostile_proposer.py` |
| 3 | Retention triage (IGNORE/REVIEW/INGEST, evidence-blind) | COMPLETE | COMPLETE | `tools/ingest/triage/policy.py` |
| 4 | Local ledger + T7 catalog, additive migrations | COMPLETE | COMPLETE | `ledger.py`, `catalog.py`, `migrations.py` |
| 5 | Evidence extraction: native text (txt/md/code/PDF/DOCX), OCR (tesseract), vision (heuristic/ollama), content-keyed cache | COMPLETE | COMPLETE | `tools/ingest/evidence/`, `tests/ingest/test_evidence_*` |
| 6 | Evidence → production naming (proposer consumes perception) | IMPLEMENTED-NOT-INTEGRATED (only the Naming Lab bridges them; production `DeterministicProposer` never sees evidence) | **COMPLETE** | D-057: `tools/ingest/perception_bridge.py`, pipeline collects evidence before naming, `tests/ingest/test_naming_from_evidence.py`, drill 52/52; AST guard intact |
| 7 | Model-backed semantic naming in production (prompt registry → loopback model → policy) | MISSING (registry + render path exist, no production caller — D-051/D-052) | **COMPLETE (opt-in)** | `tools/ingest/naming/semantic.py`, `naming.proposer: semantic`; live run with `qwen3.5:4b`; hostile-answer matrix in `tests/ingest/test_semantic_proposer.py`; default stays deterministic |
| 8 | Naming audit record (which evidence / prompt / raw output produced a name) | PARTIAL (`naming_history` records model + confidence; no prompt id/hash, no evidence provenance, no raw output) | **COMPLETE** | ledger migration 4 / catalog 5: `prompt_id`, `prompt_version`, `prompt_sha256`, `evidence_sha256`, `evidence_sources`, `raw_output`; `organize naming-history PATH` |
| 9 | Screenshots named meaningfully without ingestion | PARTIAL (routed, OCR'd, but named `screen-capture-WxH` generically) | **COMPLETE** | OCR title line → `2026-09-18_screen-capture-github-pull-request.png` in the drill; real dry run 09-18 proposed a subject-bearing name; still never ingested unless the owner says `keep` |
| 10 | Image/EXIF evidence | COMPLETE | COMPLETE | `media/probe.py` |
| 11 | HEIC derivative | COMPLETE | COMPLETE | `media/heic.py` |
| 12 | PDF evidence (native + raster + OCR) | COMPLETE | COMPLETE | `evidence/text.py`, `raster.py` |
| 13 | Office docs: DOCX | COMPLETE; XLSX/PPTX/CSV | MISSING (no extractor; PPTX/XLSX are opaque zips) | **COMPLETE** | `tools/ingest/evidence/office.py`, rule `r18-office-native-text`, 14 tests incl. zip-bomb/billion-laughs/macro-never-opened |
| 14 | Archives (zip central directory) | COMPLETE | COMPLETE | `probe.py` |
| 15 | Video: probe + bounded frame sampling + OCR + vision | IMPLEMENTED-NOT-VALIDATED (D-055/D-056; evenly spaced frames, no dedupe, no scene-change, no tests on real corpus, no naming consumer) | **COMPLETE** | D-058: hard cap 12, average-hash de-duplication, duration-aware count, documented algorithm; consumed by naming via the bridge; drill covers speech / on-screen text / weak / injection / corrupt clips |
| 16 | Video/audio speech transcription as evidence | MISSING (only the social-video pipeline had whisper via an unbuilt `vts-extract` binary; no CLI on this Mac) | **COMPLETE** | `backends/transcription.py` (`whisper-cli` 1.9.4 over stdin, model `ggml-base.en.bin` sha256 `a03779c8…` in Angel `40-ai/model-cache/whisper`), Null backend default; real transcript captured in the drill |
| 17 | Fused, provenance-carrying video evidence for naming | MISSING | **COMPLETE** | `tools/ingest/evidence/summary.py::perception_summary` (container facts / transcript / OCR per frame / visual per frame, each attributed) |
| 18 | Audio files as evidence | MISSING (`MEDIA_AUDIO` opaque) | **COMPLETE** | rule `r16-audio-transcript`; drill `New Recording 12.m4a` transcript captured ("plumber", "invoice") |
| 19 | Content-addressed caching of heavy work (OCR/vision) | COMPLETE; transcription | MISSING | **COMPLETE** | cache kind `transcript` keyed by content sha + backend/model/pipeline version; second-call backend count 1 (test) |
| 20 | Prompt registry, versioning, deterministic render | COMPLETE | COMPLETE | `tools/prompts/`, 23 tests |
| 21 | Gate-2 suites + splits + holdout guard | COMPLETE (with known deferred defects, D-054) | — | `tools/bench/acb_v1/` |
| 22 | Campaign runner, auto-staging, memory scheduler, reports | COMPLETE | COMPLETE | `tools/campaign/`, `.state/campaigns/gate2-validation-001` |
| 23 | Naming/Gate-2 chain audit + failure analysis tooling | COMPLETE (a8ddfd7) | — | `tools/bench/gate2_audit.py`, `gate2_failure_analysis.py` |
| 24 | Reasoning-model handling in campaigns (gpt-oss truncation) | MISSING (defect) | — | report: truncation 0.947 / 1.000 |
| 25 | Controlled prompt experiments dev→validation with frozen candidates | PARTIAL (Lab exists; one validation campaign on v1.0.0 only) | — | — |
| 26 | Certification store + router consults certifications | COMPLETE (mechanism); certifications | none | — | `config/certifications.example.yaml` empty |
| 27 | Gate 2 | RED — partly for plumbing reasons (#24, #21 defects, no dev/validation iteration), partly model performance | — | — |
| 28 | Angel volume structure + READMEs/manifests | PARTIAL (scaffold only, no index files, no repo/knowledge/skill-candidate homes, `10-code` semantics undefined) | **COMPLETE** | D-061, `docs/25`, root + per-top-level READMEs, `90-system/angel-manifest.yaml`, idempotent `bootstrap-t7.sh --expect-uuid`, migration reports on the volume |
| 29 | Development-repo inventory and coherent home | MISSING (`unpushed-repos.md` PLAN item open; 16 clones scattered, 1 stale duplicate) | **COMPLETE (inventory + mirrors); relocation = owner decision** | `scripts/repos-inventory` → `repos-inventory.yaml` + `unpushed-repos.md`; 14 bare mirrors in `10-code/mirrors/`, stale duplicate archived to `10-code/archives/`; no owner repo moved (Drive-synced `~/Documents`, Kronos trust domain) |
| 30 | Skills trust boundary (trusted / candidate / knowledge / generated) | PARTIAL (D-013 states it; `skills/candidates/` only gitignored; no Angel home; no doc index) | **COMPLETE** | `docs/26`, `skills/README.md`, Angel `40-ai/skills/{trusted,candidates}`, `50-research/knowledge`, `scripts/export-angel-skills` (copy-only, manifest with sha256 + git SHA) |
| 31 | Firebase/backend baselines + capability profiles | MISSING | **COMPLETE** | `docs/27-backend-baselines.md` (sourced, 2026), `templates/backend-profiles/` (4 profiles + index + reference boilerplates), exported to Angel `50-research/knowledge/backend-baselines/`, 19 tests |
| 32 | Canonical application spec V1 (schema, validator, examples) | MISSING | **COMPLETE** | D-062: `templates/app-spec/schema-v1.json`, `tools/appspec/` (validate / new / agent-brief), 4 examples validate incl. cross-check against the backend-profile index, 43 tests |
| 33 | Battery / background-work audit | MISSING (never measured) | **COMPLETE** | `docs/29`: Angel schedules one ~25 s job/day + one ~35 s job/week, nothing resident; the drain is Google Drive over `~/Documents` repos, Amphetamine, and MacWhisper (owner actions) |
| 34 | Backup reconciled + restore drill on this pass | IMPLEMENTED-NOT-VALIDATED this pass (last drill 09-12) | **BLOCKED-EXTERNALLY** | preflight green through Keychain/source; backup, check, and restore drill all fail at rclone `invalid_grant` (Drive OAuth token expired). Owner: `rclone config reconnect gdrive-backup:`. Error now names the fix (`tools/backup.py::remote_failure_message`) |
| 35 | Security / mutation-authority guards | COMPLETE for existing surfaces; new surfaces (transcript, semantic proposer, office text) | — | `test_mutation_authority.py`, `test_evidence_security.py` |
| 36 | Real-world acceptance drill | PARTIAL (docs/15 hand drill; 1C dry-runs on real Downloads; never with video/audio/semantic naming) | **COMPLETE** | `tools/acceptance/drill.py` 52/52 with tesseract + whisper; real read-only dry runs over the owner's Downloads/Documents on 09-18 (44 + 1 files, 3 proposals, 3 defects fixed) |
| 37 | PLAN/DECISIONS truthfulness | PARTIAL (Gate-2 table says "no measured results" although `gate2-validation-001` ran; Phase-0 items stale) | — | — |
| 38 | Legacy dump / bulk inventory (Phase 1) | MISSING — deliberately **owner-scheduled** (one-time import of source disks that are not attached) | POST-V1 / owner | not a V1 daily-workflow capability; sources not present |
| 39 | Organized-view materialization, client/project inference (Phase 3) | POST-V1 | POST-V1 | requires certified classification model (Gate 2) |
| 40 | Linux workers, coordinator API, Dell/B2 backup (Phase 4) | BLOCKED-EXTERNALLY | BLOCKED-EXTERNALLY | hardware not provisioned |
