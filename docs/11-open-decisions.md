# 11 — Open Decisions for Architecture Review

A reviewer should explicitly decide or recommend experiments for these items.

## Storage
- T7 filesystem: APFS Encrypted vs alternative?
- Will T7 remain Mac-attached or move physically between systems?
- Should media and active developer work share one volume?
- What is the second backup target?

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
