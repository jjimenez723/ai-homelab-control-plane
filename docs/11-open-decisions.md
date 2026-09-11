# 11 — Open Decisions for Architecture Review

Most items below were decided on 2026-09-10; see `DECISIONS.md` for the decision and `reviews/ASTRA-V0-ARCHITECTURE-REVIEW.md` for the reasoning. Items still open are marked **OPEN** and depend on measurements from `PLAN.md` phases.

Decided: T7 filesystem/security (D-017, superseding D-002), T7 host (D-003 as modified by D-017), immediate off-site backup target (D-018, with later append-only D-004 retained), node roster (D-005), queue (D-006), network (D-007), inference runtime (D-008), catalog (D-009), coding-agent worktree/permission model (D-010), sidecar/catalog format (D-009), Kronos placement (D-011), metrics (D-012).

Still **OPEN** pending measurement: embedding model choice, rename confidence threshold, keyframe sampling policy, GPU purchase, whether `dell-i7` becomes the dedicated Kronos box.

## Storage
- **DECIDED (D-017):** keep the populated T7 as exFAT with Samsung Magician password/security; do not reformat for V0.
- **DECIDED (D-003/D-017):** T7 remains Mac-attached for ingestion; Linux workers receive job payloads/copies rather than using it as their active working filesystem.
- **DECIDED:** active developer work stays on native internal filesystems; T7 is storage/archive/media, not the primary package-manager workspace.
- **DECIDED (D-018):** immediate off-site protection is encrypted restic repository data via rclone in the dedicated Google Drive path. D-004's append-only Dell and B2 path remains a later independent failure domain.

## Network
- Ethernet topology and switch speed?
- SMB vs NFS vs Syncthing vs object storage for specific data classes?
- Tailscale/WireGuard for remote administration?

## Scheduler
- Redis, NATS, RabbitMQ, Postgres queue, or something else?
- How should leases/retries/idempotency be modeled?
- Is a central scheduler required immediately?

## Local inference
- MLX on Apple Silicon?
- llama.cpp/Ollama/vLLM or another runtime where compatible?
- Which actual models fit the M4 Pro at useful speeds?
- What jobs are worth CPU inference on the Dell nodes?
- Is a dedicated future NVIDIA box economically justified?

## Coding agents
- How should local coding models receive isolated Git worktrees?
- What permissions can they have?
- How are diffs/tests reviewed before merge?

## Media
- Sidecar format and database/index choice?
- Embedding model?
- Keyframe sampling policy?
- Rename confidence threshold?
- Human correction UX?

## Trading
- Which node/runtime becomes the isolated research worker?
- Should any cluster worker ever receive market/broker credentials?
- How is future live execution physically/logically separated?
- Which evidence gates must be satisfied before any real-money pilot?

## Observability
- Metrics stack appropriate for four nodes?
- Which measurements are needed before buying GPUs?

## Hardware expansion
- Used NVIDIA workstation vs eGPU (where technically viable) vs Mac expansion vs cloud burst?
- Required VRAM target based on measured workloads, not model hype?
