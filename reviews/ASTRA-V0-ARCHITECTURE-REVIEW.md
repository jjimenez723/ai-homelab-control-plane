# Astra V0 Architecture Review

- Repository: `jjimenez723/ai-homelab-control-plane`
- Baseline: `main` @ `2b07c533c9a781db9791e9bfa8a1b1480e795133` (PR #1 squash, 2026-09-09)
- Review date: 2026-09-10
- Evidence used: every file in the repository; Git history including both merged feature branches and PR #1; the pinned `video-to-skill` source at `c527aa9`; the five real pipeline outputs in the owner's working clone; the coordinator Mac itself (`sysctl`, `diskutil`, `df`, installed tools, model caches); GitHub repository settings via `gh api`.

Numbered section references (§n) are used by `DECISIONS.md` and `PLAN.md`.

---

## 1. Executive verdict

**Viable with changes.** The documented principles are right and the repository already avoids the two classic mistakes (pooling RAM into one model; letting an LLM rename originals). But the design is currently a set of good intentions with three problems underneath:

1. **Nothing is measured.** No local model has ever run on the coordinator (no Ollama, MLX, llama.cpp, LM Studio, or model weights other than Kronos/whisper in the HF cache). Every local-model claim in the docs is unverified, and the Mac's internal disk has **12 GB free of 460 GB (98% full)**, which will break the first serious inference run and is already a data-safety problem for APFS snapshots.
2. **The security boundary that matters most is not enforced anywhere.** Kronos research artifacts sit in the same user account, same HF cache, same keychain as everything else on the coordinator. The trading boundary exists only as prose.
3. **The one implemented subsystem leaks untrusted content into the trusted tree.** Video-derived "candidate skills" were written to `skills/candidates/`, the same tree agent harnesses load from, and that path was not git-ignored. Four candidates already exist on disk. This is fixed on the review branch.

None of this is fundamentally flawed. The corrections are mostly deletions (of components, of ambiguity, of one node) plus a handful of concrete decisions the docs left open. The 90-day plan in `PLAN.md` does not require buying anything except a Backblaze bucket and, optionally, GitHub Pro.

## 2. What the existing design gets right

- **Jobs before tensors.** Correct and stays correct. Heterogeneous CPU/Metal parallelism over Wi-Fi is a research project, not infrastructure.
- **AI proposes, policy authorizes; raw is immutable; sidecars are durable; idempotent automation.** These four principles, if actually enforced in code, prevent the worst outcomes. They are the right invariants.
- **Dump-first.** Refusing to make the owner pre-sort is the correct product decision; the whole point of the catalog is to make sorting a derived view.
- **Consolidation ≠ backup** is stated repeatedly. Good; §12 makes it concrete.
- **The video pipeline never executes video-derived commands** and the pinned upstream (`video-to-skill`) is a rare example of a small project with a written threat model, a real kernel sandbox (seatbelt/bwrap), checksum-pinned downloads, and consent-gated install. Pinning to a reviewed commit and only using its `extract` subcommand was the right call.
- **CI is already hygienic**: SHA-pinned actions, `permissions: contents: read`, `persist-credentials: false`, timeouts, concurrency. Better than most production repositories.
- **PR #1's body is honest** about the branch-name mismatch and about squashing 19 noisy commits. That is the right instinct.
- **The UI policy** correctly refuses to iOS-ify Android/web while keeping HIG as a craft bar; the registry-with-reasons pattern is good.

## 3. Incorrect assumptions

| # | Assumption in the docs | Reality |
|---|---|---|
| A1 | "T7 may hold clean clones/workspaces" for active dev | Active dev on a USB volume is slower, loses work when the cable moves, and the internal disk (where it belongs) is full. Fix the disk first. |
| A2 | Linux workers "consume files through a network protocol rather than mounting APFS directly" — left as a maybe | It is not a maybe: Linux cannot mount *encrypted* APFS at all. Decide it (D-002/D-003). |
| A3 | The Dells are useful "CPU inference" nodes | A Precision 7520 is a 4-core Kaby Lake Xeon; a 7B Q4 model generates ~3–5 tok/s and prefills ~30 tok/s. That is useless for agents and marginal for batch classification. Dells are for ffmpeg, hashing, transcription batches, embeddings, builds, and sandboxed untrusted work — not LLMs. |
| A4 | `dell-lite` (8 GB, broken display) is a "monitoring/queue" node | A node costs a service identity, patching, Tailscale, and a failure mode. It buys nothing the Xeon can't host in a spare 500 MB. Retire it (D-005). |
| A5 | The video pipeline "deduplicates and never reprocesses" | Completion state is committed in `data/video-learning-queue.yaml` while artifacts are git-ignored. A fresh clone re-downloads all five reels. |
| A6 | `--force` exists | Only `SOCIAL_VIDEO_FORCE=1` existed. Fixed. |
| A7 | The `unsafe/untrusted` classification is a safety control | It is a regex. Two of five real reels were false positives; a real injection avoids the phrases trivially. The digest is written regardless. The actual control is "nothing derived from video is ever executed or loaded as instructions" — that must hold without the classifier. |
| A8 | OCR is adequate for on-screen text | Measured: tesseract on stylized reel overlays yields `MOnelrootitolder`, `aBraimfolder`. Adequate for GitHub screenshots, useless for overlays. |
| A9 | `ggml-base` is an adequate transcription model | It mis-hears names ("Kazakistan", "Alpami Makajan") and is weak on non-English. For a Brazilian-market app and Spanish/Portuguese clients, `large-v3-turbo` is the floor. |
| A10 | Kronos separation is a "future" concern | Kronos models are in `~/.cache/huggingface/hub` of the same user that will run coding agents. The boundary is already violated by default. |
| A11 | "Configure `main` to require PRs" is a settings toggle | Rulesets/branch protection on a **private** repo require GitHub Pro/Team. On a Free personal plan this cannot be done at all. |
| A12 | Secret scanning is a control the repo has | Secret scanning and push protection on private repos are paid (GitHub Secret Protection). The repo has neither. gitleaks added in CI. |
| A13 | The lock file pins the dependency | It pins the *source commit*; the binary that runs is a local `cargo build` whose hash was recorded nowhere, and the Python constant `VTS_COMMIT` duplicated the lock with no test tying them. Fixed (provenance in `source.json`, test added). |
| A14 | The `skills/` tree is safe because candidates "are review-only" | Review-only text in a directory that harnesses auto-load is not review-only. Fixed. |

## 4. Top architecture risks (severity × likelihood)

Scored 1–5 each; product shown.

| Rank | Risk | Sev | Lik | Score | Mitigation (section) |
|---|---|---|---|---|---|
| 1 | Coordinator internal disk at 98% → failed snapshots, failed model loads, kernel-panic-style hangs, and ingest jobs dying mid-write | 5 | 5 | 25 | Phase 0 in `PLAN.md`; nothing else starts before 60 GB free |
| 2 | T7 becomes the only copy of client media during "dump-first" and is lost/encrypted/dropped | 5 | 4 | 20 | §12 backup chain before any source is wiped; append-only restic |
| 3 | General coding/media agent and Kronos share one OS user, keychain, and HF cache → credential/trust bleed | 5 | 4 | 20 | §24: separate macOS user now, separate machine before live capital |
| 4 | Untrusted video-derived text lands in the trusted `skills/` tree and is auto-loaded by a harness | 4 | 4 | 16 | Fixed on branch; §21 trust model |
| 5 | Local-model economics assumed rather than measured; owner buys a GPU or wastes weeks on a 4-core Xeon | 4 | 4 | 16 | §18, §25, §27; Gate 2 before any hardware decision |
| 6 | `main` is unprotected (no ruleset, all merge methods, no auto-delete), so one bad `git push` or a compromised token rewrites history | 4 | 3 | 12 | §32; Pro plan or hook backstop |
| 7 | Agent worker holds a broad GitHub/API credential → poisoned repo or prompt exfiltrates it | 5 | 2 | 10 | §21: branch-scoped fine-grained tokens, router owns frontier keys |
| 8 | Catalog corruption / misclassification silently materialized into the organized view | 4 | 3 | 12 | §11, §19: clones not moves, corrections table, canary set, dry-run diff |
| 9 | Over-building the cluster (queue broker, k8s, metrics stack) before one worker is proven | 3 | 4 | 12 | §7, §28 |
| 10 | Single-maintainer upstream (`video-to-skill`, 5 stars) goes stale or hostile; no rebuild path documented per node | 3 | 3 | 9 | §20: vendor the build recipe, record binary sha256, treat as replaceable |

## 5. Data-loss risks

1. **Full internal disk** (measured). APFS behaves badly near 100%: Time Machine local snapshots fail, `cp -c` clones fail, SQLite WAL checkpoints fail, and macOS can hang. This is the most likely near-term data-loss event and has nothing to do with the cluster.
2. **Dump-first without a second copy.** The plan says "establish a second backup before any source deletion" but names no target, no tool, and no credential model. Until §12 is implemented the T7 is a single point of failure holding the only organized copy.
3. **Hardlink view.** `docs/13` lists "hardlink where filesystem semantics permit." A hardlink shares the inode: editing the organized view edits the original. Rejected; APFS clones (`cp -c`) replace it.
4. **Locked dump not specified.** Nothing prevents a script, Finder drag, or agent from writing into `00-inbox/legacy-dump/`. `chflags uchg` after hashing (Phase 1).
5. **Queue file mutation by the tool** (`update_queue_item` rewrites a committed YAML). A crash mid-write leaves a half-written queue; a `git checkout` discards state. Move completion state to the ignored knowledge index.
6. **Unpushed Git work in the dump.** The inventory must detect dirty/unpushed/stashed repos and produce the one manual list the owner must act on. Everything else is derivable; unpushed commits are not.
7. **Restic password loss** = loss of all backups. Print it; store it physically elsewhere.
8. **Backup credential on the coordinator.** If the backup identity can delete, ransomware on the Mac deletes the backups too. Append-only rest-server and a B2 key without `deleteFiles`.
9. **Catalog on the T7 with no copy.** The catalog is small; it goes into every restic snapshot. Sidecars remain the durable form so the catalog can be rebuilt.

## 6. Security risks

Threat-by-threat, current state → required state.

| Threat | Today | Required |
|---|---|---|
| Malicious prompt / prompt injection via video, repo, or web | Regex label; digests written verbatim; candidates in `skills/` | Data/instruction separation at every consumer (§21); candidates quarantined (done); no executable output from untrusted stages |
| Poisoned repository (hooks, `.vscode/tasks`, `npm postinstall`, `direnv`) in the legacy dump or a cloned target | Inventory would run `git status` in unknown repos — safe — but any later build runs hooks | Inventory uses `git --git-dir … --no-optional-locks` read-only; builds only in sandboxed units on Linux; `core.hooksPath=/dev/null` for inventory |
| Malicious model weights | Only pickle-based formats are dangerous; GGUF/safetensors are data | Allow only `.gguf`/`.safetensors`; pin sha256 in a model registry; never `trust_remote_code` |
| Dependency compromise | Pip unpinned hashes; single Rust dep pinned by commit but not by binary | `--require-hashes` when the requirements grow; Dependabot (added); record binary hashes (added) |
| GitHub credential theft | Owner's `gh` keyring token has full scope on this Mac | Agents get fine-grained, per-repo, non-`main` tokens; owner token never on a worker |
| API-key leakage | No router yet; keys would be in agent env | Router on the coordinator holds frontier keys; workers call the router, never providers |
| Client A reads client B | One catalog, one user | Per-client `data_classification` + per-job allowlisted `BindPaths=`; the context-pack generator refuses cross-client paths |
| Ransomware | Mac ↔ T7 ↔ (future) SMB share all writable by one user | Append-only backups; dump locked; SMB export read-only |
| Destructive coding agent | Would run as owner | Linux sandbox units, disposable worktrees, no `~/`, no sudo, branch-scoped tokens |
| Poisoned skill files | `skills/candidates/` inside trusted tree | Fixed; promotion by human PR only |
| Compromised worker | Would have Tailscale reach to everything | ACL tags; workers reach only the coordinator API and package registries |
| LAN compromise | Plain LAN, laptops on Wi-Fi | Nothing listens on LAN interfaces except SSH; services bind to Tailscale IPs |
| Remote-access compromise | None yet | Tailscale SSH with device approval; no port forwarding |
| Physical loss of T7 | Unformatted | APFS Encrypted with a real passphrase |
| Backup corruption | No backups | `restic check --read-data-subset`, monthly restore drill |
| Accidental public commit | Repo is private; no secret scanning | gitleaks in CI; `.gitignore` already broad; never make this repo public while it names clients |
| Trading credential compromise | Same user as everything | §24 |

## 7. Unnecessary complexity

Delete from V0 (and from the docs' "evaluate later" lists, which invite scope creep):

- **Kubernetes, Nomad, Ray, Temporal, Celery, RabbitMQ, NATS, Redis.** Three nodes and one human. A SQLite table with leases behind an HTTP API does everything these would, and its failure modes fit in one person's head.
- **Docker on the Mac.** Not needed for Ollama, whisper, SQLite, or restic. Rootless Podman on Linux only for job classes that need an image.
- **A metrics stack.** Job rows plus a `report` CLI. Grafana at day 60+ only if the CLI is insufficient.
- **`dell-lite`.** Retired.
- **Nine standard files per generated repo.** Six (§22).
- **The `unsafe/untrusted` classifier as a control.** Keep it as a hint; stop describing it as a safeguard.
- **Two duplicate `process_queue` implementations** (one dead and buggy). Removed.
- **A separate `PROGRESS.md`.** PR history + `PLAN.md` checkboxes.
- **Vector database as a service.** `sqlite-vec` beside the catalog until >5 M vectors.
- **SMB/NFS as the primary data path to workers.** Job payloads carry the files; a read-only SMB export is a convenience for bulk reads only.
- **Syncthing.** Two-way sync between a laptop and workers is how one bad rename propagates everywhere. Not used.

## 8. Missing components

- **A backup implementation** (tool, target, credential model, drill). Now specified (§12).
- **An ingest catalog schema** and the inventory tool. Specified (§11).
- **A coordinator/queue** even in minimal form. Specified (§15).
- **A model registry + benchmark harness** so routing is data-driven. Specified (§16, §25).
- **A router** that owns frontier keys and writes cost rows. Specified (§17).
- **A context-pack generator** — the single most important piece for making weak models useful. Specified (§22).
- **A secrets model** (where the APFS key, restic password, B2 key, GitHub tokens, provider keys live). Specified (§13, §21).
- **A trust label on every artifact** (`trust: untrusted-video-derived` added to `source.json`; the catalog schema carries `trust` per row).
- **Client isolation primitive** (per-job allowlisted paths).
- **An OS-level home for Kronos** (separate user now, separate machine later).
- **Repository settings** (squash-only, auto-delete, rulesets) and free substitutes for paid GitHub security features.
- **A restore drill.** A backup that has never been restored is a hypothesis.

## 9. Corrected V0 architecture

```text
                          owner (human)
                               |
                 +-------------+--------------+
                 |     mac-m4pro (coordinator)|
                 |  Ollama (LLM/VLM/embed)    |
                 |  whisper large-v3-turbo    |
                 |  router  (owns API keys)   |
                 |  coordinator API + jobs.db |
                 |  ingest daemon + catalog   |
                 |  T7 (APFS enc.) attached   |
                 |  restic client             |
                 +------+-------------+-------+
                        | Tailscale   | Tailscale
        tag:worker      |             |      tag:backup / tag:quant
   +--------------------+--+     +----+----------------------------+
   | dell-xeon (Linux)     |     | dell-xeon: restic rest-server   |
   | worker: systemd-run   |     |   --append-only  (copy 2)       |
   |  sandboxed job units  |     +---------------------------------+
   |  ffmpeg / whisper-cpu |            | nightly restic copy
   |  embeddings / builds  |            v
   |  coding-agent worktree|     Backblaze B2 (copy 3, no-delete key)
   +-----------------------+
   dell-i7: provisioned after Gate 4; reserved for a separate quant box
   dell-lite: retired
   Frontier APIs: reached only from the router on the coordinator
   Kronos: separate macOS user `quant` now; separate machine before live capital
```

Principles that make this small: one process owns each piece of state (catalog → ingest daemon; jobs → coordinator; keys → router); workers hold no state and no secrets; every untrusted input is processed on a Linux sandbox unit or, until one exists, under `sandbox-exec`; every stage writes rows so the day-90 hardware decision is arithmetic.

## 10. T7 filesystem / storage architecture

**Decision: APFS (Encrypted), case-insensitive, Mac-attached, never mounted by Linux.** (D-002, D-003)

Why each part:

- *APFS*: clonefile (zero-cost copy-on-write copies, which is the derived-view primitive), snapshots, native encryption, hardware-accelerated SHA on Apple Silicon, TRIM, proper permissions and symlinks. Git is fully happy on APFS.
- *Encrypted*: the T7 will hold client media and leaves the building. Use a real passphrase, not only a keychain-bound key, so another Mac can unlock it after a loss. Store the passphrase in the password manager and print a copy.
- *Case-insensitive*: matches the internal disk so repositories behave identically; case-sensitive volumes break Xcode/Adobe/Unity/Steam. The inventory flags any Linux-born repo with case-only collisions; those get fixed in Git.
- *Mac-attached*: the Mac is the media workstation and the only node that can read the volume. USB 3.2 Gen 2 gives ~900 MB/s read on a T7; the LAN gives ≤ 110 MB/s (1 GbE) or worse on Wi-Fi. Hashing 1 TB locally is ~20 minutes; over the LAN it is hours. So inventory/hash/catalog run on the Mac and only compute-heavy per-file jobs ship bytes to workers.
- *Linux interop*: `apfs-fuse` / `linux-apfs-rw` are read-only-ish and do not support encrypted volumes. Do not put client data behind an experimental driver. If a drive must roam between OSes, buy a second drive formatted ext4 for that purpose; do not compromise the T7.
- *exFAT rejected*: no permissions, no hard/symlinks (breaks `npm`, `pnpm`, `uv` venvs), no clones, no snapshots, no encryption, weak crash consistency.
- *Git on external storage*: fine on APFS. Keep active clones on the internal disk; the T7 holds `git clone --mirror` copies (bare, compact, complete history) and cold workspaces. Set `core.fsmonitor=false` for anything on the T7.

Physical: attach directly to the Mac (not through a hub that shares bandwidth with a display). Cable strain is the common failure; keep the drive on the desk, not dangling. Exclude the T7 from Spotlight (`mdutil -i off /Volumes/T7`) and from Time Machine (restic handles it).

Checksum strategy: **SHA-256, single pass, computed once at inventory, stored in the catalog and in the sidecar.** Apple Silicon SHA-256 runs ≥ 2 GB/s, faster than the drive, so it is never the bottleneck; BLAKE3 would buy nothing here. Re-verify by sampling (1% weekly via restic's own checks plus a catalog `verify --sample` job), not by full re-hash.

Duplicate strategy: exact duplicates by SHA-256 are recorded as one `blob` with many `paths`; nothing is deleted. Near-duplicates: perceptual hash (dHash 64-bit) for images and per-keyframe dHash for video, flagged as `near_dup_of` with distance; never auto-resolved. Deletion of exact duplicates is a separate, later, human-approved job with its own policy (§11 GC).

Generated-view strategy: the organized tree is **materialized from the catalog with APFS clones** (`cp -c` / `clonefile`). Zero extra space, separate inode, so an edit in the view never touches the dump. Regeneration is idempotent: compute desired view → diff against the current view → apply only the delta → refuse if the delta exceeds a threshold (default 20%) without `--yes`.

Database/catalog/index: **SQLite on the T7** (`90-system/catalog.sqlite`, WAL mode, one writer: the ingest daemon) plus `sqlite-vec` for embeddings. Per-asset JSON sidecars are the durable, human-readable, restic-friendly form; the catalog can be rebuilt from sidecars and vice versa. PostgreSQL + pgvector is the documented successor at >5 M assets or >3 volumes; the schema is written to survive that move (`volume_id` + relative path, UUID asset IDs, no absolute paths).

Failure recovery: the dump is immutable and locked; the catalog is in every backup; the view is regenerable; the sidecars are the tie-breaker. Losing the catalog costs a rebuild from sidecars (minutes to hours). Losing the T7 costs a restic restore. Losing the Mac costs nothing but time.

When volume gets large (≥ 4 TB, the T7's ceiling): a second/third volume registered in the catalog (`volumes` table), the same ingest daemon, and a NAS or DAS with ZFS/btrfs as the "warm" tier while the T7 becomes the "hot" portable tier. Embeddings move to pgvector. Nothing in the V0 schema has to change; only where rows point.

## 11. Dump-first ingestion architecture

The owner's pipeline (messy → protected ingest → inventory → hash → dedup → type → git → metadata → vision → transcript → client/project → tags → names → embeddings → view) is right in shape. Corrections:

1. **Copy, lock, then inventory.** `rsync -aHAX --info=progress2` per source into `00-inbox/legacy-dump/<source>/`, log kept; inventory verifies count and bytes against the log; then `chflags -R uchg`. Only after the first restic snapshot is the source disk considered copied.
2. **Everything after hashing is a job on the catalog, not a walk of the filesystem.** The filesystem is walked exactly once. All later stages (`type`, `git`, `metadata`, `vision`, `transcribe`, `infer_client`, `tag`, `name`, `embed`) are idempotent jobs keyed by `asset_id` + `stage` + `model_version`; re-running with the same versions is a no-op.
3. **Deterministic first.** Stages 1–6 (inventory, hash, dedup, type, git, metadata) are Tier 0 code. No model is involved until vision/transcript, and even client/project inference starts with deterministic signals (source folder, EXIF dates, filename tokens, sibling repo names) before a small model votes.
4. **Confidence is per field and per model version.** A canonical name is assembled from fields (date, client, project, subject, variant), each with its own confidence, so a wrong `subject` does not poison the `client`.
5. **Human corrections live in their own table and always win.** A later model pass never overwrites a corrected field; it writes a new `proposal` row that is ignored unless the correction is cleared.
6. **The view is proposals filtered by threshold, materialized as clones.** Below threshold → `20-clients/_review/<client-guess>/…` (still clones). Nothing is ever moved out of the dump.

Catalog schema (V0, SQLite):

```sql
volumes(volume_id TEXT PK, label, fs, serial, mounted_at)
blobs(sha256 TEXT PK, size INT, mime, first_seen, phash BLOB NULL)
assets(asset_id TEXT PK, sha256 → blobs, volume_id → volumes, rel_path, original_name,
       mtime, ctime, source_device, locked INT, trust TEXT DEFAULT 'owner-data')
repos(repo_id PK, asset_id, remote_url, head, branch, dirty INT, unpushed INT, stash INT, duplicate_of NULL)
media_meta(asset_id PK, width, height, duration, codec, captured_at, camera, gps NULL)
stages(asset_id, stage, model, model_version, prompt_sha, started, finished, status, cost_row → jobs)
proposals(id PK, asset_id, field, value, confidence, model, model_version, created)
corrections(asset_id, field, value, by, created, PRIMARY KEY(asset_id, field))
tags(asset_id, tag, source, confidence)
embeddings(asset_id, kind, model, vec) -- via sqlite-vec virtual table
view_state(view_path PK, asset_id, sha256, materialized_at)
jobs(job_id PK, class, node, model, tier, input_tokens, output_tokens, wall_ms, energy_wh,
     verification, retries, escalated INT, human_corrected INT, usd_est, created, finished)
```

Git repository handling: detect `.git` (dirs, files for worktrees/submodules, bare repos); read remotes/HEAD/branches/dirty/unpushed/stashes using `git --git-dir … -c core.hooksPath=/dev/null` (read-only, hooks disabled); group by remote URL; mark exact duplicate clones; **never merge, delete, or push automatically.** Output `unpushed-repos.md` for the owner. Fresh canonical mirrors are created later, on the internal disk for active work and as `--mirror` on the T7.

What happens when an AI misclassifies: nothing physical moves; the wrong proposal is one row; the view shows a clone in the wrong folder; the owner corrects one field (CLI or a later web UI); the correction row wins forever; the view regenerates. A **canary set** (100–200 human-labelled assets) is re-scored on every model/prompt change; a drop > 5 points blocks view regeneration.

Garbage collection stays exactly as `docs/13` states, plus one rule: GC is its own job class with `PrivateNetwork`, a dry-run report, and a per-run cap on bytes freed.

## 12. Backup / recovery architecture

```text
T7 (primary) --restic hourly when attached--> dell-xeon internal disk
                                              restic rest-server --append-only    (copy 2)
                                                     --restic copy nightly-->  Backblaze B2, versioned bucket,
                                                                               application key without deleteFiles  (copy 3)
```

- restic: deduplicating, encrypted, content-addressed, one binary, mature, cheap to drill. Alternatives considered: Time Machine (no offsite, no append-only), rclone alone (no versioned dedup), borg (no native object storage). Kopia is an acceptable substitute if restic proves slow on the Xeon.
- Append-only is the ransomware/destructive-agent control: the Mac's backup identity can add snapshots but cannot prune. Pruning is a separate credential run by hand.
- Workers never hold backup credentials and are never sources of record.
- Verification: `restic check` weekly with `--read-data-subset=5%`; monthly scripted restore of one random client folder with hash comparison; the result is a row in `90-system/migration-reports/`.
- Order of operations for every legacy source: copy → inventory → lock → snapshot to copy 2 → verify → (B2 copy) → only then wipe the source. No shortcuts for SD cards.
- RPO/RTO table is in `docs/09`. Irreplaceable = client raw media, corrections, unpushed Git work. Everything else is regenerable.
- Cost: B2 ≈ $6/TB/month (2 TB ≈ $12/month). GitHub remains the copy for committed code only.

## 13. Network topology

- **Tailscale** (personal plan; 3 users / 100 devices free) on every node; MagicDNS; Tailscale SSH; device approval on. No router port forwarding, no inbound LAN listeners except SSH.
- ACL tags: `tag:coordinator` (Mac), `tag:worker` (Dells), `tag:backup` (rest-server on the Xeon, exposed only to the coordinator), `tag:quant` (Kronos box/user). Rules: worker → coordinator API only (one port); coordinator → worker SSH and worker API; `tag:worker → tag:quant` denied; `tag:worker → tag:backup` denied; `tag:quant` reaches nothing except its own broker endpoints via its own firewall.
- Raw WireGuard: rejected. Same crypto, more key management, no ACLs, no SSH auth.
- SMB: a read-only export of selected T7 folders from the Mac to `tag:worker` for bulk reads; never the primary job data path. NFS from macOS is a worse experience; not used. Syncthing: not used (§7).
- Wired Ethernet for the Xeon (it is stationary); the Mac may be on Wi-Fi. Nothing in V0 needs >1 GbE because bulk data stays on the Mac.

## 14. Node responsibilities

| Node | Role | Explicitly not |
|---|---|---|
| `mac-m4pro` (M4 Pro 14-core, 48 GB, macOS 26.5) | Coordinator; all LLM/VLM/embedding inference (Ollama, Metal); whisper `large-v3-turbo`; router (holds provider keys); ingest daemon + catalog; T7 host; restic client; owner's dev workstation | Not an agent sandbox for untrusted code; not a 24/7 server (it sleeps, travels) |
| `dell-xeon` (Precision 7520-class, 32 GB) | First Linux worker: sandboxed job units for ffmpeg, hashing of shipped payloads, CPU whisper batches, embeddings, builds/tests, the social-video pipeline, coding-agent worktrees; restic rest-server (copy 2); optional read-only SMB consumer | Not an LLM inference node (4-core Kaby Lake); not a Kronos node |
| `dell-i7` (32 GB) | Provisioned after Gate 4 as worker #2, **or** rebuilt as the physically separate Kronos execution box (`tag:quant`) when a paper→live path exists. It does not do both. | Not provisioned in the first 60 days |
| `dell-lite` (8 GB, broken display) | **Retired.** Wipe it, keep it as a spare, or recycle it. | Everything |
| `t7` | Primary organized data volume; catalog; mirrors | Not a backup; not a secret store; not a dev volume |
| Future NVIDIA node | vLLM/llama.cpp server registered as another inference backend in the model registry; router prefers it by measured tok/s | Not a re-architecture: the router/registry make it a config change |

## 15. Queue / job architecture

**Decision (D-006): one coordinator process on the Mac, SQLite `jobs.sqlite`, HTTP API on the Tailscale interface, pull-based workers.**

API (all JSON, bearer token per node):

```text
POST /jobs                 submit {class, inputs, requirements, idempotency_key, priority}
POST /jobs/claim           {node, classes[], capabilities} → job + lease (ttl 300 s)
POST /jobs/{id}/heartbeat  extends lease
POST /jobs/{id}/complete   {result, artifacts[], metrics}
POST /jobs/{id}/fail       {reason, retryable}
GET  /jobs?status=…        for the report CLI
```

Semantics: at-least-once with idempotency keys; leases expire → job returns to `queued` with `attempts+1`; `max_attempts` per class; dead-letter status after that; results are JSON rows plus artifact paths on the T7 (written by the coordinator, not the worker, when the payload comes back). Job classes declare `network: none|registries|web`, `mounts: []`, `memory_max`, `cpu_quota`, `timeout`, `trust_of_inputs`.

Why not Postgres now: the Mac is the only always-relevant node, SQLite in WAL mode handles thousands of jobs/minute from one process, and restoring the queue is copying a file. Why not Redis/NATS: they add a daemon, a persistence story, and a second place state lives, for zero gain at this scale. Migration trigger to PostgreSQL on `dell-xeon`: >3 concurrent workers, >50k jobs/day, or the coordinator must survive the Mac's lid closing. The API does not change; the storage behind it does.

Worker: a Python service per Linux node, `systemd` unit, polls every 2 s with backoff, runs each job as `systemd-run --unit=job-<id> -p DynamicUser=yes -p ProtectHome=yes -p ProtectSystem=strict -p PrivateTmp=yes -p BindPaths=/srv/jobs/<id> -p MemoryMax=… -p CPUQuota=… -p RuntimeMaxSec=… [-p PrivateNetwork=yes]`, captures stdout/stderr, ships results. Rootless Podman only for classes that declare an image.

## 16. Local inference architecture

Measured facts about the coordinator: M4 Pro, 10P+4E cores, 48 GB unified, ~273 GB/s memory bandwidth, no local model runtime installed as of 2026-09-10.

Runtime decision (D-008): **Ollama** for V0 — OpenAI-compatible API, per-model fixed context (which is what keeps Metal memory bounded), broad model coverage, and a 2026 field report of `mlx-lm` server crashing from unbounded KV growth while Ollama ran the same MoE model stably at 100% GPU. MLX remains the likely throughput winner; benchmark it per model and adopt it only after a 24-hour soak. `llama.cpp` server is the fallback and the CPU runtime on Dells. vLLM only on a future NVIDIA node. LM Studio is fine for interactive testing, not as a service.

Model classes for the M4 Pro (verify every number in Phase 2; these are the classes and the public 2026 figures for this hardware):

| Tier | Class | Fit on 48 GB | Expected | Use |
|---|---|---|---|---|
| 1 small | ≤ 4B instruct (Qwen 3.6 small / Gemma 4 E4B class), Q4–Q8 | ~3–5 GB | 80–120 tok/s gen | classification, tag/filename proposals, extraction, log summaries, topic labels |
| 2 capable (MoE) | Qwen 3.6-35B-A3B class, Q4/MXFP8 | ~20–37 GB depending on quant | 35–55 tok/s gen; prefill fast (3B active) | bounded code edits, test generation, repo summaries, structured JSON, most agent steps |
| 2 capable (dense) | Qwen 3.6-27B class, Q4_K_M | ~17 GB | 18–28 tok/s; prefill ~100–150 tok/s | higher-quality bounded coding when latency is acceptable |
| 2 alt | Gemma 4 26B-A4B, gpt-oss-20b class | ~13–16 GB | 30–45 tok/s | second opinion / diversity for review |
| VLM | Qwen3-VL-8B class (Q4 ~6 GB); Qwen3-VL-32B class (~20 GB) for hard frames | yes | ~1–3 s per frame (8B) | on-screen text, UI screenshots, media tagging, frame reasoning |
| ASR | whisper.cpp `large-v3-turbo` (Metal); `faster-whisper` equivalent | ~1.6 GB | ≥ 10× realtime | all transcription |
| Embeddings | bge-m3 or nomic-embed-text v1.5 (text, multilingual); SigLIP 2 for images | < 2 GB | thousands/min | search index |

Rules that follow from the hardware:

- **Prefill is the agent killer, not generation.** On this bandwidth a dense 27B prefills a 30k-token context in tens of seconds to minutes per turn; 3B-active MoE is several times faster. Local coding agents therefore get **context packs ≤ 12k tokens**, not repo dumps. This is why §22 exists.
- **One large model resident at a time.** 48 GB minus macOS/apps (~10 GB) leaves room for one Tier-2 model + its KV cache + one small model or the VLM. The router serializes Tier-2 calls and unloads on idle (Ollama `keep_alive`).
- **Quantization**: Q4_K_M / MXFP4-8 for MoE; avoid Q2/Q3 (measurable quality loss); prefer 8-bit for the small tier where memory allows because it is nearly free there.
- **Thermals/power**: sustained inference on a MacBook Pro throttles after minutes and draws ~50–70 W. Batch jobs run on mains power with the lid open or clamshell with cooling. Record thermal throttling in the benchmark (tok/s at minute 1 vs minute 20).
- **Model files**: only `.gguf`/`.safetensors`, sha256 pinned in `data/model-registry.yaml` (create in Phase 2), stored in `40-ai/model-cache/`, never in Git.

CPU-only Dells (4–8 cores, no usable GPU): **yes** — whisper/parakeet batch transcription (overnight, 0.3–1× realtime), text embeddings, ≤ 4B classifiers for bulk tagging where latency is irrelevant, OCR, ffmpeg, hashing, builds. **No** — any interactive or agentic LLM use, VLMs above 4B, anything with a human waiting. The Precision's Quadro M2200 (if present, 4 GB Maxwell) is not worth configuring.

## 17. Model-routing policy

```text
Tier 0  deterministic code      hashing, parsing, schema validation, git ops, policy gates, exact transforms
   ↓ (only if a language judgement is required)
Tier 1  small local (≤4B)       classification, extraction to schema, tag/name proposals, summaries ≤ 2k tokens
   ↓ (if Tier-1 confidence < threshold, or task class demands code/reasoning)
Tier 2  capable local           bounded code edits with tests, test generation, refactors, repo summaries,
                                structured vision, first-pass review — context pack ≤ 12k tokens
   ↓ (if verification fails twice, context > 12k, risk ≥ high, or task class is Tier-3-only)
Tier 3  frontier                architecture, ambiguous specs, security review, hard debugging, final review
                                of consequential diffs, anything cross-repository
```

Routing inputs and how each is used:

| Input | Effect |
|---|---|
| complexity / required intelligence | task class → minimum tier; classes are declared in the job contract, never guessed from the prompt |
| context length | > 12k tokens → skip Tier 2 (prefill cost); > 100k → Tier 3 only |
| latency | human-waiting jobs bypass the queue and may go straight to the fastest adequate tier |
| privacy | `restricted` never leaves the machine; `confidential` may go to frontier only with the provider's zero-retention terms recorded in the registry; client media never leaves by default |
| hardware availability | Tier 2 busy → queue (batch) or escalate (interactive, if privacy allows) |
| token/API cost | per-class budget; router refuses Tier 3 above budget and queues for human |
| verification requirements | if a deterministic verifier exists (tests, schema, hash), lower tiers are allowed to try first; if none exists, minimum tier rises one step |
| risk | `high` (destructive, credential, deploy, financial) → Tier 3 proposes, **human** authorizes; the router cannot approve |

Escalation is explicit and recorded: `jobs.escalated=1` with the reason. Confidence self-reports are ignored for routing; only verifier outcomes and calibrated thresholds from the canary set count.

## 18. Local-vs-frontier cost strategy

Per-job cost model (all values recorded in `jobs`):

```text
E[cost_local_path] = c_local × (1 + r_retry) + p_escalate × c_frontier + p_wrong_accept × c_human
E[cost_frontier]   = c_frontier + p_wrong_accept_f × c_human

c_local = wall_hours × (P_watts/1000 × $/kWh + hw_amortization_$/h) + queue_delay_cost
```

Numbers to seed until measured: M4 Pro under load ≈ 60 W → ~$0.01/h at $0.17/kWh; amortizing $2,500 over 3 years of 2,000 productive hours/year ≈ $0.42/h; at 40 tok/s a local hour is ~144k output tokens → **~$3/M tokens fully amortized, ~$0.07/M in energy alone**. A frontier model costs roughly $3–15/M output tokens plus input. So per token local is cheaper by 1–100×, **but** a failed local attempt that escalates costs the local time *and* the frontier call, and a wrongly accepted local result costs human time (the most expensive input in this system, price it at ≥ $50/h).

Rule: route to local when, for that task class over the last 30 days, `E[cost_local_path] < E[cost_frontier]` **and** p50 local latency is within the class's latency budget. Classes that never clear the bar stay frontier; that is a measurement, not a failure.

Measurements (every job row): task class, tier, model, tokens in/out, wall time, prefill vs generation time, energy estimate (from a per-node W table), verification result, retries, escalation reason, human correction flag, queue delay. Weekly `report`: success rate by tier/class, escalation rate, human-correction rate, $ spent on frontier, $ saved (frontier-equivalent tokens served locally × frontier price), local utilization, p50/p95 queue delay. These same rows drive the GPU decision (§27).

Token minimization that matters more than model choice: context packs (§22), deterministic pre-processing (Tier 0 before any model), caching prompts by content hash, structured outputs (JSON schema) to stop retries, and never sending media to a frontier model when a local VLM tag + text is enough.

## 19. Media organization architecture

Pipeline per asset (all jobs on the catalog, idempotent by `asset_id + stage + model_version`):

```text
inventory/hash (Tier 0, Mac)            once
   ↓
type + technical metadata (Tier 0)      exiftool/ffprobe; captured_at, camera, dims, duration, codec
   ↓
proxies (Tier 0, Dell)                  720p H.264 proxy per video; 1024px JPEG per image; both stored under
                                        20-clients/.../20-proxies (clones are not possible here; these are new bytes)
   ↓
keyframes (Tier 0, Dell)                scene-change detection (ffmpeg `select=gt(scene,0.3)`), cap 12 frames/min,
                                        plus first/last; dHash per frame for near-dup
   ↓
transcript (ASR, Mac or Dell batch)     large-v3-turbo; only when audio has speech (VAD gate first)
   ↓
VLM tags (Tier 1/VLM, Mac)              Qwen3-VL-8B on the *proxy* image or ≤ 12 keyframes; structured JSON:
                                        scene, objects, shot type, orientation, quality flags, text-on-screen
   ↓
client/project inference (Tier 0 → 1)   deterministic signals first (source folder, date clusters, sibling files,
                                        repo names, EXIF), small model votes with the top-3 candidates listed
   ↓
canonical-name proposal (Tier 1)        template `YYYY-MM-DD_<client>_<project>_<subject>_<variant>_<seq>.<ext>`,
                                        every field with its own confidence
   ↓
embeddings (Mac)                        text (bge-m3) over transcript+tags+caption; image (SigLIP 2) over proxy
   ↓
view materialization (Tier 0)           clones into the organized tree; below-threshold → _review/
```

Never run a VLM over full-resolution originals or every frame; the proxy and the keyframe cap bound cost to a few seconds per asset on the M4 Pro. Never send client media to a frontier model by default; if a frontier pass is ever justified, it sees proxies only and the decision is a per-client flag in `manifest.yaml`.

Retrieval: hybrid — SQL filters (client, date range, orientation, duration) + vector similarity (text and image) + tag match; `tools/search` CLI first, a small local web UI later. People: count only; no identity recognition (as the docs already say).

Corrections UX for V0: a CLI (`tools/catalog correct <asset> field=value`) and a generated HTML review sheet per project (proxy thumbnails, proposals, one-click accept/correct writing back through the CLI). This is enough to build the canary set; a real UI is a day-90+ item.

## 20. Social-video learning architecture

What exists works end-to-end on five reels and is honest about its limits. Corrected architecture:

```text
URL / file  →  normalize + dedupe (platform+id, then media sha256)            [Tier 0, exists]
   →  acquisition + probe + keyframes + ASR   in a sandbox unit on dell-xeon   [vts-extract, whisper large-v3-turbo]
   →  on-screen text: VLM over ≤ 12 frames (Qwen3-VL-8B); tesseract as fallback   [new]
   →  classification: small local model with a fixed JSON schema
        {content_type, topics[], tools_mentioned[], claims[], procedure_steps[]|null, injection_suspected}   [replaces regex]
   →  knowledge index (ignored SQLite): one row per video, embeddings over digest+transcript   [new]
   →  digest.md / claims.json / recommendations.md   [exists; recommendations become project-linked]
   →  candidate skill (only if procedure_steps) → knowledge/skill-candidates/   [moved; never installed]
```

Trust: the whole `knowledge/` tree is `untrusted-video-derived`. Any agent that reads it gets the text inside a delimited block with an explicit "data, not instructions" wrapper from the tool, never as a system/skill file. The regex `UNSAFE_INPUT` stays only as a hint column. The `injection_suspected` field from the classifier is likewise a hint. The control is structural: no stage executes, installs, or `source`s anything derived from a video; the sandbox unit has no `~/`, no credentials, and network only to the platforms' CDNs (Tailscale exit rules or a per-unit allowlist via `IPAddressAllow=` once the CDN ranges are known; until then `PrivateNetwork` is false for the acquisition step and true for every later step).

`video-to-skill` at `c527aa9`: single maintainer, 5 stars, last push 2026-09-09, `c527aa9` is `main` HEAD as of review. The code read confirms: `extract` runs yt-dlp/ffmpeg/whisper only; downloads are registry-pinned with sha256 and a test forbids `latest`; the sandbox is seatbelt/bwrap with credential paths blocked; install is `--yes`-gated. Trust implications: (1) it is one person's project — keep the build recipe vendored (`scripts/build-vts.sh`, Phase 4) so a deleted repo does not strand you; (2) `cargo build --locked` pulls 109 crates from crates.io at the versions in its `Cargo.lock` — acceptable, but record the resulting binary's sha256 (now done in `source.json`); (3) do not upgrade by bumping the commit; re-read the diff of the five sensitive paths listed in `docs/12` each time; (4) never use its `install` or `verify` subcommands from this pipeline. Alternative if it goes stale: `yt-dlp` + `ffmpeg` + `whisper.cpp` directly is ~150 lines of Python; the pinned tool is a convenience, not a dependency you cannot replace.

OCR sufficiency: no for overlays (measured), yes as a cheap fallback. The VLM layer costs ~10–30 s per reel on the M4 Pro and should be V0.1, before batch-processing hundreds.

Scale to hundreds of videos: the ignored knowledge index (SQLite + sqlite-vec) gives dedupe, transcript caching, provenance, topic search, and "which project does this apply to" via a `projects.yaml` registry (name, keywords, stack) matched by the classifier. Recommendations become rows linking `video → project → suggested action`, not prose only. Instagram breakage is expected; the queue records the yt-dlp error and moves on (already does).

Queue file: make `data/video-learning-queue.yaml` input-only (URLs + notes). Completion/blocked state moves to the ignored index; a `--status` flag prints it. Stop committing state.

## 21. Agent-skill trust / sandbox model

Three trust classes, three locations, three loaders:

| Class | Location | Loaded by | Promotion |
|---|---|---|---|
| Trusted skill | `skills/<name>/SKILL.md` in Git | agent harness, as instructions | human PR with review, CI passes |
| Candidate | `knowledge/skill-candidates/<name>/` (ignored) | never automatically; a human reads it | human copies into `skills/` via PR |
| Knowledge | `knowledge/**` (ignored) | tools, as quoted data | never becomes instructions |

Sandbox for anything that consumes untrusted input (video, unknown repos, web pages, dependency installs) or runs generated code:

- Linux worker: `systemd-run` unit, `DynamicUser`, `ProtectHome`, `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies`, `BindPaths` to one workspace, `MemoryMax`, `CPUQuota`, `RuntimeMaxSec`; `PrivateNetwork=yes` unless the class declares otherwise; no Tailscale reach except the coordinator API.
- Mac (until a worker exists): `sandbox-exec` with a deny-default profile allowing the workspace, `/usr`, `/opt/homebrew`, the model cache, and network for acquisition only.
- Coding agents: fresh `git worktree` per task on the worker; a **fine-grained GitHub token** scoped to that one repository with `contents: write` and `pull_requests: write`, refused on `main` by ruleset (or hook); token minted per task where practical, otherwise rotated monthly; no `gh auth` of the owner's identity on any worker; provider API keys only in the router process on the coordinator (workers call `http://coordinator/route`).
- Secrets model: password manager for humans (T7 passphrase, restic password, B2 key, provider keys); `sops`+`age` for the few config files a service needs, with the age key in the OS keychain (Mac) or a root-only file with `systemd` `LoadCredential=` (Linux). No `.env` in repos, ever; `.gitignore` already blocks it and gitleaks now checks.
- Client isolation: a job's `mounts` is an allowlist of catalog-resolved paths; the context-pack generator refuses to include paths from more than one `client_id` unless the task declares `cross_client: true` with a reason.

## 22. Software-factory architecture

The bottleneck for "one opportunity per week" is not code generation; it is validation, launch operations, and human attention. Design the pipeline so most opportunities die cheaply and early:

```text
discovery (weekly batch, Tier 1 + web)           → 10 candidates, 1 page each
research (Tier 3, budgeted ≤ $3 each)            → market, competitors, distribution, regulatory (nurse scheduling!)
validation gate (human, 20 min)                  → kill ≥ 70 %
product spec SPEC.md (Tier 3)                    → user stories, non-goals, acceptance criteria, data model
design brief (Tier 2 + registry)                 → §23
architecture ARCHITECTURE.md (Tier 3, template)  → one page, decisions inline
repo from template (Tier 0)                      → app-template with CI, tokens, AGENTS.md, context-pack tool
PLAN.md bounded tasks (Tier 3)                   → each ≤ 12k-token context, has tests, has a stop condition
implementation (Tier 2 on worker, worktree)      → branch + PR + CI
review (Tier 3, diff + context pack only)        → approve / request changes
staging (Tier 0)                                 → preview deploy per PR
launch decision (human)
analytics / iteration (Tier 1 summaries)
```

WIP limits: max 2 products in implementation at once; max 1 in launch. A portfolio kanban (a `products.yaml` in `50-research/` plus GitHub Projects) is the control surface.

Context engineering — the mechanism that makes local models effective: a deterministic **context-pack generator** (`tools/context-pack <repo> <task-id>`) emits ≤ 12k tokens containing: the task from `PLAN.md`; the relevant `SPEC.md` slice; a repo map (file tree with symbol signatures, aider-style, ≤ 2k tokens); the exact files to edit (whole, small) and interfaces of their neighbours (signatures only); the test command and the last failing test output; the `AGENTS.md` rules; and nothing else. The same pack is what the frontier reviewer sees, so review cost is bounded too. Packs are cached by content hash and attached to the PR for traceability.

Standard files — keep six, cut three (D-015):

| File | Keep? | Why |
|---|---|---|
| `README.md` | yes | humans; setup; links |
| `AGENTS.md` | yes | the one file every harness reads: commands, conventions, boundaries, what not to touch, how to run tests — absorbs `TESTING.md` |
| `SPEC.md` | yes | acceptance criteria are what make bounded tasks verifiable |
| `ARCHITECTURE.md` | yes | one page + a `## Decisions` list; absorbs a separate `DECISIONS.md` for small apps |
| `SECURITY.md` | yes | data classes, auth model, secrets policy, threat list; also the GitHub convention |
| `PLAN.md` | yes | the bounded-task ledger with checkboxes; source of `context-pack` tasks |
| `TESTING.md` | no | duplicates `AGENTS.md` commands and drifts |
| `PROGRESS.md` | no | drifts within a day; PR history + `PLAN.md` checkboxes are the progress |
| `DECISIONS.md` | only for platform/infra repos | apps keep decisions inside `ARCHITECTURE.md` |

Template repo (`app-template`): the biggest throughput multiplier. One proven skeleton per platform pair (web: Next.js + chosen UI system + tokens; mobile: Expo + chosen system), with CI, preview deploys, analytics wiring, the six files, and the context-pack tool. Every new product starts from it, so agents see a familiar shape every time.

Verification per task: tests must exist before the local model runs (the frontier writes them with the spec, or the task is Tier 3); type-check; lint; a diff-size cap; a forbidden-paths list; a stop condition. "Build the whole app" is never a job.

## 23. UI / design-system policy review

The policy is mostly right. Corrections and gaps:

- **Platform handling**: web/React/Next.js, React Native/Expo, SwiftUI, UIKit, Compose, Flutter are each covered with a sane default and an explicit "avoid iOS-ifying". Good. Missing: **Expo's native UI path** (`@expo/ui`, SwiftUI/Compose-backed views) and **React Native Reusables** (shadcn-style, NativeWind) as registry entries — Tamagui is powerful but heavy for a small team, and the registry should offer a lighter default for Expo apps that do not share code with web. Add both with `recommendation: alternative`.
- **Design tokens**: the policy names token categories but no format. Standardize on a single `tokens.json` (W3C Design Tokens Community Group format) per product, transformed by Style Dictionary into Tailwind theme, Compose `MaterialTheme`, SwiftUI constants, and Flutter `ThemeData`. Dark mode and high contrast are token *modes*, not separate palettes.
- **Responsive behaviour**: no breakpoints or container-query guidance. Add: mobile-first; container queries for components; three named breakpoints per product; Dynamic Type/large-text tested at the largest accessibility size; landscape/rotation on tablets.
- **Accessibility acceptance is not measurable** as written. Make it: axe-core zero critical/serious violations; Lighthouse accessibility ≥ 95; keyboard-only walkthrough of every flow; VoiceOver/TalkBack pass on the primary flow; contrast ≥ 4.5:1 body text; touch targets ≥ 44 pt (Apple) / 48 dp (Android).
- **Reduced motion** is handled; add `prefers-reduced-data` and battery/`Low Power Mode` awareness for video-heavy UIs (relevant to Roam-style media apps).
- **Performance budgets** are absent. Add per-platform budgets: web LCP ≤ 2.5 s, INP ≤ 200 ms, JS ≤ 200 kB gz initial; mobile cold start ≤ 2 s, list scroll 60 fps with FlashList/LazyColumn/List; images via responsive sources.
- **Component-library selection**: add `license`, `bundle_cost`, `a11y_notes`, `maintenance_status`, `last_verified` fields to the registry; a registry without a license column will eventually pull in a GPL/BUSL component.
- **Icons and charts** are not covered: SF Symbols (Apple), Material Symbols (Android), Lucide (web/RN); charts via Recharts/Victory Native/Swift Charts/Compose charts — pick per platform in the registry.
- **Forms/validation**: react-hook-form + zod (web/RN) is the missing default.
- **Design-agent workflow**: the "design brief" risks being theater. Make it a machine-checked `design-brief.yaml` (platform, registry ids, tokens file path, component map, a11y checklist booleans, custom-primitive justifications) validated in CI; the implementation gate reads it.
- **Companion skills** (`apple-design`, `emil-design-eng`, `animate`, `react-native-best-practices`, `vercel-react-native-skills`, `react-native-testing`, `web-perf`) are referenced but do not exist in this repository. Record their source and pinned version in the registry or remove the references; a dangling skill name is a prompt-injection vector (an attacker publishes a skill with that name).
- **Visual consistency**: enforce "one system per surface" mechanically with an ESLint import rule (no `@mui/*` when `@base-ui/*` is the primary) and a Compose/SwiftUI lint for mixed component families.
- **Interaction feedback** rules are good; add explicit optimistic-UI and error-recovery patterns (retry, undo toast with timeout) since those are where generated apps usually fail users.

## 24. Kronos trust-domain isolation

Current state (measured): Kronos model weights, tokenizer, and TiRex/Chronos/TimesFM caches sit in the primary user's HF cache on the coordinator; the same user will run Ollama, coding agents, and the video pipeline. Any general agent with `~/` access can read Kronos research artifacts today, and any future broker credential in the primary keychain would be one `security find-generic-password` away from a compromised agent.

Required boundary, strongest-first:

1. **Hardware** (before any live-capital gate): execution and credentials live on a physically separate machine — `dell-i7` rebuilt, or a $200 mini PC — tagged `tag:quant`, on its own firewall (`ufw` default deny; allow only broker endpoints outbound and SSH from the owner's device), unreachable from `tag:worker` by Tailscale ACL. Paper trading may stay on the Mac; live may not.
2. **Operating system** (now): a separate macOS user `quant` on the coordinator. Separate home, keychain, HF cache, shell history. The primary user (and every agent under it) cannot read `~quant`. Kronos development happens in that user via fast user switching or SSH-to-localhost.
3. **Service accounts**: the general cluster runs Kronos jobs only as **pure functions**: inputs are public market data files, outputs are files; the job class declares `network: none` and `secrets: none`. The order/execution service has its own identity that nothing else can assume.
4. **Secrets**: broker and live-account credentials exist only in the `quant` user's keychain / a separate password-manager vault; never in the router, never in `sops` files the coordinator can decrypt, never on the T7.
5. **Network**: broker endpoints are reachable only from the execution component; `tag:quant → tag:worker` is also denied (no reason for the quant box to reach workers).
6. **Data classes** (as `docs/07` says): models, market data, prompts, research artifacts, execution credentials, broker state are different classes with different homes; only the first three may ever touch the general cluster.

The conceptual boundary `AI research → proposal → deterministic validation → risk engine → paper → explicit human live gate` stays. Add two invariants: the live gate is a **credential transition, not a config flag** (the live key is physically absent from the paper environment); and every promotion across a stage writes an append-only log line signed by the stage's own identity.

Assume the general coding agent is compromised: with the above, it can read public market data and Kronos code (already on GitHub), and nothing else. That is the acceptable blast radius.

## 25. Observability / metrics

V0 = rows, not dashboards. Every job writes one `jobs` row (§11 schema). Every node writes a `nodes` heartbeat row (load, free RAM, free disk, temperature, model resident). `tools/report` prints:

- per class/tier: count, success %, retry %, escalation %, human-correction %, p50/p95 wall, p50/p95 queue delay;
- per node: utilization %, thermal-throttle minutes, disk free;
- money: frontier $ spent, frontier-equivalent $ served locally, energy $;
- data: assets cataloged, awaiting review, corrections/week, canary score.

Alerts (a launchd/systemd timer that emails/pings): disk free < 15%, restic snapshot age > 48 h, `restic check` failure, canary drop > 5 points, queue dead-letters > 0, coordinator unreachable from a worker for > 1 h.

Day 60+: Netdata on the Linux nodes if you want live graphs (one package). Prometheus/Grafana/OTel only if the CLI report is insufficient — write down what question it cannot answer first.

## 26. Hardware expansion strategy

Order of upgrades, each gated by a measurement (§27):

1. Free the internal disk (cost $0). 2. Second/offsite backup (B2, ~$12/month). 3. A larger/second T7 or a DAS when the catalog passes 3 TB. 4. GitHub Pro ($4/month) for rulesets. 5. A used mini PC as the quant box only when a live-trading path is real. 6. **Then** GPU compute.

GPU options when justified, cheapest useful first: used RTX 3090 (24 GB, ~$700) in a used workstation — runs Qwen 3.6-27B/35B-A3B class at 2–3× the M4 Pro's tok/s and 5–10× its prefill; two 3090s (48 GB) for 70B-class dense Q4 or large MoE; RTX 5090 (32 GB) for FP4-native throughput; RTX PRO 6000 (96 GB) only if a measured workload needs it; a Mac Studio M4/M5 Ultra with 192–256 GB is the alternative if the workloads are memory-bound MoE (DeepSeek/Kimi/GLM-class) rather than prefill-bound. Cloud burst (an hourly H100) is the right answer for one-off jobs and for measuring whether a bigger model actually helps before buying anything.

The registry/router design means a new node is: install runtime → register backend URL + measured tok/s + models → router prefers it. No re-architecture.

## 27. Exact measurements that justify buying an NVIDIA GPU machine

Compute from ≥ 30 days of `jobs` rows after Gate 2. Buy only if **at least two** are red for two consecutive weeks:

| # | Metric | Red threshold |
|---|---|---|
| M1 | Tier-2 queue delay p50 (batch classes) | > 30 min |
| M2 | Coordinator inference-busy time during working hours | > 60 % |
| M3 | Prefill wait p50 for interactive Tier-2 jobs | > 90 s |
| M4 | Frontier spend on tasks that a local model completed successfully ≥ 70 % of the time in canary/eval (i.e., local-eligible spend) | > $150/month |
| M5 | Media/video backlog: assets awaiting VLM/ASR at end of each night | growing week over week |
| M6 | A named workload needing > 40 GB resident (measured, with an eval showing ≥ 10-point quality gain over the 48 GB-fit model) | exists |

Payback check: `(M4 monthly local-eligible spend + hours of owner waiting × $50) × 18 months ≥ machine cost + $30/month power`. If it does not clear 18 months, rent GPU hours instead.

## 28. Things we explicitly should NOT build yet

Kubernetes, Nomad, Ray, Temporal, Celery, Redis, NATS, RabbitMQ; distributed tensor inference; multi-node training; a metrics stack; a web UI for the catalog; automatic file renaming or deletion; automatic skill installation; a Kronos live-trading path; `dell-lite` provisioning; `dell-i7` provisioning before Gate 4; Syncthing or two-way sync of any kind; a vector database service; fine-tuning local models (measure first); an "agent that manages the cluster"; anything that gives an agent `sudo`, a broker key, or the owner's GitHub token.

## 29. Recommended V0 technology stack

| Concern | Choice | Alternatives considered |
|---|---|---|
| Storage | APFS Encrypted T7, SQLite catalog + sqlite-vec, JSON sidecars | exFAT (no), Postgres (later), LanceDB (fine, but one more format) |
| Backup | restic + rest-server `--append-only` + Backblaze B2 | Kopia (ok), Time Machine (not offsite), Borg (no object storage) |
| Network | Tailscale + ACL tags + Tailscale SSH | raw WireGuard (more work, fewer features) |
| Queue | SQLite behind a Python coordinator HTTP API; pull workers | Postgres SKIP LOCKED (at trigger), NATS/Redis (no) |
| Isolation | `systemd-run` sandboxed units; rootless Podman for image-based classes; `sandbox-exec` on Mac | Docker (no), Firecracker/VMs (later if ever) |
| Inference | Ollama (Mac); benchmark MLX; llama.cpp on Dells for embeddings/small; vLLM on a future GPU | LM Studio (interactive only) |
| ASR | whisper.cpp `large-v3-turbo` (Metal) / faster-whisper on Dells | Parakeet-TDT 0.6B v3 for English-only CPU speed |
| VLM | Qwen3-VL-8B class via Ollama; 32B class for hard frames | Gemma 3/4 vision (fine), Moondream (small) |
| Embeddings | bge-m3 (multilingual text), SigLIP 2 (image) | nomic-embed, Qwen3-Embedding |
| Router | small Python service; model registry YAML with sha256, context, tok/s, price, privacy class | LiteLLM (acceptable if you want provider adapters for free) |
| Coding agents | harness of choice on the Linux worker, fed context packs; frontier via router | — |
| Secrets | password manager; `sops`+`age`; OS keychain / `systemd` `LoadCredential` | Vault (no) |
| Linux base | Ubuntu LTS server, FDE, `ufw`, unattended-upgrades | Debian, NixOS (later, once configs stabilize) |
| Video learning | yt-dlp + `video-to-skill` extract (pinned) + whisper + VLM; SQLite index | pure yt-dlp/ffmpeg/whisper if upstream stales |
| CI | GitHub Actions, SHA-pinned; unittest; gitleaks; Dependabot | — |

## 30. 30/60/90-day implementation roadmap

Full detail with checkboxes is `PLAN.md`. Summary:

- **Days 0–30**: Phase 0 (disk, B2, secrets, repo settings, `quant` user), Phase 1 (format T7, dump, inventory, hash, lock, first restic snapshot, unpushed-repo list), start Phase 2 (Ollama, benchmark harness, router v0, `report`).
- **Days 30–60**: finish Phase 2; Phase 3 (one-client catalog pilot with canary set, clone view, search CLI); Phase 4 (`dell-xeon` as sandboxed worker; coordinator API; backup copy 2 and 3 live; social-video pipeline moved to the worker with VLM frames).
- **Days 60–90**: Phase 5 (app-template, context-pack generator, one real bounded task end-to-end by a local model with frontier review); Phase 6 (hardware decision from 30 days of rows).

## 31. Acceptance gates for every major phase

| Gate | Pass condition |
|---|---|
| 0 | ≥ 60 GB free on the Mac; B2 bucket + no-delete key; `git push origin main` refused locally; `ls ~quant` denied for the primary user |
| 1 | catalog rows == inventoried files; 1 % re-hash sample identical; restic restore of one client folder matches; sources untouched; `unpushed-repos.md` produced |
| 2 | benchmark table for every registry model (prefill 2k/8k/32k, gen tok/s, RSS, thermal at 20 min); router passes the 20-task set with ≥ 90 % verified success on tiers 0–1; `report` runs |
| 3 | canary precision ≥ 95 % client/project, ≥ 80 % tags; deliberate bad-model swap is caught; view regeneration idempotent |
| 4 | job runs on the Dell in a unit with no `~/`, no network (for a no-network class), memory cap; killed worker → job re-claimable after lease; ACL denies worker→quant; B2 restore drill passes |
| 5 | one local-model PR passes CI and human review; `report` shows $/merged-task local vs frontier; probe job cannot read `~/.ssh`, env secrets, keychain |
| 6 | GPU triggers computed; decision recorded in `DECISIONS.md` |

## 32. Git / GitHub governance recommendations

Observed: `main` unprotected (no ruleset, no branch protection); all three merge methods enabled; `delete_branch_on_merge=false`; two merged feature branches still on the remote; PR #1 mixed two concerns and carried 19 commits including `chore: temporary review marker` / `chore: remove temporary review marker`; secret scanning absent (paid on private repos); no Dependabot; CI good. The account is a personal account; if on the Free plan, rulesets are unavailable for this private repo.

Recommendations, in order of impact:

1. **Repository settings (free, now):**
   ```bash
   gh api -X PATCH repos/jjimenez723/ai-homelab-control-plane \
     -F allow_merge_commit=false -F allow_rebase_merge=false -F allow_squash_merge=true \
     -F squash_merge_commit_title=PR_TITLE -F squash_merge_commit_message=PR_BODY \
     -F delete_branch_on_merge=true -F allow_update_branch=true
   git push origin --delete feature/social-video-learning-v0 feature/design-system-ui-library-policy
   ```
2. **GitHub Pro ($4/month) and a ruleset on `main`**: require PR, require `Python tests` and `Secret scan`, require linear history, block force-push and deletion, require signed commits once signing is set up. Until then, `scripts/install-git-hooks.sh` installs the `pre-push` backstop (added).
   ```bash
   gh api -X POST repos/jjimenez723/ai-homelab-control-plane/rulesets --input - <<'JSON'
   {"name":"main","target":"branch","enforcement":"active",
    "conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},
    "rules":[{"type":"deletion"},{"type":"non_fast_forward"},{"type":"required_linear_history"},
             {"type":"pull_request","parameters":{"required_approving_review_count":0,"dismiss_stale_reviews_on_push":true,"require_last_push_approval":false,"required_review_thread_resolution":true}},
             {"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":true,
               "required_status_checks":[{"context":"Python tests"},{"context":"Secret scan"}]}}]}
   JSON
   ```
3. **Branch naming**: `feat/`, `fix/`, `docs/`, `ci/`, `chore/`, `review/` + kebab slug; one concern per branch; branch lifetime ≤ 1 week.
4. **Commits**: Conventional Commits with scope (`fix(social-video): …`); squash-merge small branches; `git merge --no-ff` only for long-lived integration branches (none exist); never rebase-merge (loses the PR grouping). Squash title = PR title, body = PR body (setting above).
5. **PR sizing**: ≤ 400 net lines for code; docs may be larger but one topic. PR #1-style "two features in one" is the anti-pattern to avoid.
6. **Signed commits**: turn on SSH commit signing for the owner (`git config gpg.format ssh; git config user.signingkey ~/.ssh/id_ed25519.pub; git config commit.gpgsign true`) and upload the key as a signing key; require signatures in the ruleset after 30 days of it working.
7. **Tags/versioning**: annotated tags at gates (`v0.1-gate1`, …). SemVer only for `tools/` once it is packaged; date-based milestone tags for the planning docs.
8. **Dependabot** (added) for actions and pip; auto-merge is off; review weekly. **Dependency review action** requires GHAS on private repos — skip; run `pip-audit` in CI once requirements grow.
9. **gitleaks** (added) as the free substitute for secret scanning; note the action sends repo owner/name to keygen.sh for license validation on org accounts (not personal); if that is unacceptable, pin the gitleaks binary by sha256 instead of the action.
10. **CODEOWNERS**: no value with one owner; skip. **Issue templates**: skip. **PR template**: added (four headings).
11. **Changelog**: GitHub Releases auto-notes at tags; no `CHANGELOG.md` churn.
12. **Rollback**: squash merges make `git revert <sha>` a one-liner per feature; tag before every gate; never force-push `main`.
13. **History hygiene**: temporary-marker commits should be squashed *before* opening a PR (`git rebase -i` on the branch is fine; the branch is not `main`).
14. **Repository visibility**: keep private; it names clients.

## 33. Concrete file-by-file repository changes

Applied on `review/astra-v0-architecture` (this PR):

| File | Change |
|---|---|
| `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` | this document |
| `PLAN.md` | new: phased roadmap with gates |
| `DECISIONS.md` | new: D-001…D-015 |
| `tools/social_video_learn.py` | candidates default → `knowledge/skill-candidates/`; real `--force`; normalized input to extractor; extractor sha256 + `trust` in `source.json`; dead `process_queue` removed; a missing `yt-dlp`/extractor now blocks the item instead of aborting the batch with a traceback (found by smoke test) |
| `tools/social_video_queue.py` | shared defaults; `--force`; testable `parse_args` |
| `tests/test_social_video_learn.py` | +4 tests (lock/constant consistency, candidate path, `--force` + normalized input, missing-tool handling) |
| `scripts/learn-video` | routes `--queue` from any argument position |
| `.gitignore` | ignore legacy `skills/candidates/` |
| `docs/02-t7-filesystem.md` | filesystem decision, clone-based view, catalog placement, internal-disk rule |
| `docs/03-compute-cluster.md` | queue decision, node roster, Tailscale/ACL, sandboxing from day one |
| `docs/09-backup-recovery.md` | concrete chain, RPO/RTO table |
| `docs/11-open-decisions.md` | decided vs open |
| `docs/12-video-learning-pipeline.md` | `--force`, candidate path, measured limitations |
| `README.md` | map, review pointers, candidate path |
| `.github/workflows/ci.yml` | gitleaks job |
| `.github/dependabot.yml`, `.github/pull_request_template.md` | new |
| `scripts/git-hooks/pre-push`, `scripts/install-git-hooks.sh` | new |

Recommended next (not in this PR; each is its own small PR):

| File | Change |
|---|---|
| `data/video-learning-queue.yaml` | strip `status:` fields; state moves to the ignored index |
| `tools/social_video_learn.py` | VLM frame pass; JSON-schema classifier replacing regex; knowledge index (SQLite) |
| `data/model-registry.yaml` (new) | model id, file sha256, quant, context, measured tok/s, privacy class, price |
| `data/ui-library-registry.yaml` | add `license`, `bundle_cost`, `a11y_notes`, `last_verified`; add `@expo/ui`, React Native Reusables, icon and chart entries; pin/remove dangling companion skills |
| `templates/design-brief.yaml` (new) | machine-checked brief |
| `docs/04-model-routing.md` | tier table + routing inputs from §17; cost model from §18 |
| `docs/05-media-pipeline.md` | proxy/keyframe/VLM bounds from §19; catalog schema from §11 |
| `docs/06-software-factory.md` | six-file contract; context-pack spec; WIP limits |
| `docs/07-trading-security-boundary.md` | separate OS user now; separate machine before live; ACL rules |
| `docs/08-security-threat-model.md` | table from §6 |
| `docs/13-dump-first-ingestion.md` | remove "hardlink" option; add lock step and canary |
| `docs/14-design-system-and-ui-library-policy.md` | measurable a11y/perf budgets; token format |
| `docs/GIT_WORKFLOW.md` | settings, ruleset, signing, sizing from §32 |
| `scripts/bootstrap-t7.sh` | add `mdutil -i off`, `tmutil addexclusion`, `90-system/catalog.sqlite` placeholder note |
| `tools/ingest/inventory.py` (new) | Phase 1 |
| `tools/coordinator/`, `tools/worker/`, `tools/router/`, `tools/report` (new) | Phases 2–4 |
| `scripts/build-vts.sh` (new) | vendored build recipe for the pinned extractor |

---

## Final review standard

| Question | Answer after this review |
|---|---|
| Could one compromised local agent destroy my files? | Not once the dump is locked, backups are append-only, and agents run in sandboxed units without `~/`. Today: yes. |
| Could one compromised worker reach every other machine? | No: ACL tags limit it to the coordinator API. Today: there are no workers. |
| Could a malicious Reel become executable instructions? | No: nothing derived from video is executed, installed, or loaded from the trusted tree. The candidates-in-`skills/` path is closed on this branch. |
| Could client data leak into another client's context? | Not once per-job mount allowlists and the context-pack refusal exist; until then, one user sees everything. |
| Could losing the T7 destroy irreplaceable data? | Not once copy 2 and 3 exist and are drilled. Today: yes, and the plan's first phases exist to close exactly this. |
| Could a broken local model silently corrupt file organization? | No: proposals never move bytes, corrections win, canary blocks regeneration. |
| Could local inference cost more than API usage? | Yes for some classes; the cost model routes those to frontier. Measured, not assumed. |
| Could a general-purpose agent reach trading credentials? | Today: yes (same user). After D-011: no. |
| Understandable to one person in six months? | Yes: three services (ingest, coordinator, router), one database file each, one worker type, `PLAN.md`/`DECISIONS.md`. |
| Add an NVIDIA node without rebuilding? | Yes: registry + router. |
| Can a future agent understand what happened from Git history? | Yes if squash titles stay descriptive and `DECISIONS.md` is kept; PR #1's body is the model. |
| Can every change be reviewed, tested, reverted, traced? | Yes with squash-only + gates + tags; server-side enforcement needs Pro. |
