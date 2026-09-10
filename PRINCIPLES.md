# Design Principles

## 1. Jobs before distributed inference

The initial cluster distributes independent jobs among machines. Distributed tensor/model parallelism across heterogeneous Macs and Intel Linux laptops over ordinary networking is explicitly out of scope for V0.

## 2. Local-first, not local-only

Prefer local execution when it is adequate, cheaper, more private, or can run unattended. Escalate to frontier models when the expected quality improvement justifies cost or risk.

## 3. Capability-aware routing

Model routing must consider:
- task type;
- required context size;
- code/vision/reasoning capability;
- memory requirement;
- hardware architecture;
- estimated runtime;
- privacy classification;
- monetary cost;
- task risk;
- confidence/verification requirements.

## 4. AI proposes; policy authorizes

For destructive, financial, credential, deployment, or production actions, an LLM response is never itself authorization.

## 5. Git is code truth; storage is workspace

Version-controlled code belongs in Git remotes. The T7 is a fast local workspace/mirror, not the only authoritative copy.

## 6. Metadata is durable

AI-generated tags and filenames must be stored in machine-readable manifests/sidecars so they can be corrected without repeatedly reprocessing original media.

## 7. Idempotent automation

Running an ingestion, rename, indexing, or synchronization job twice should not duplicate or corrupt data.

## 8. Raw data is immutable

Original media should enter an immutable/raw tier before derivatives, renamed exports, proxies, or edited media are generated.

## 9. Workers are disposable

A worker node should be rebuildable from configuration and should not hold unique irreplaceable state.

## 10. Secrets are not files in the project tree

Use an operating-system keychain, dedicated secret manager, encrypted environment injection, or equivalent. Never document real credentials in this repository.

## 11. Risk domains are isolated

The trading assistant, client production systems, experimental local agents, and general media automations should not share unrestricted credentials or write authority.

## 12. Measure before upgrading hardware

Collect queue delay, inference speed, RAM pressure, CPU utilization, storage throughput, and task failure data before buying additional compute.
