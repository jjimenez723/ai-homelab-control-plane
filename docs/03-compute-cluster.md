# 03 — Compute Cluster V0

## Goal

Coordinate heterogeneous machines without pretending they are one shared-memory computer.

## Logical components

```text
                 +--------------------+
                 |   Control Plane    |
                 | initially M4 Mac   |
                 +---------+----------+
                           |
             +-------------+--------------+
             |             |              |
        Task Queue     Node Registry   Policy Engine
             |             |              |
             +-------------+--------------+
                           |
                 Capability Router
                           |
          +----------------+----------------+
          |                |                |
      mac-m4pro        dell-xeon         dell-i7
      inference        CPU worker        CPU worker
          |
      dell-lite
  monitoring/utility
```

## V0 responsibilities

### Control plane
- receives jobs;
- assigns job ID;
- records required capabilities;
- chooses worker/model;
- tracks state;
- captures structured outputs;
- retries safe/idempotent failures;
- escalates uncertain tasks.

### Worker agent
Each worker should advertise:
- hostname/node ID;
- OS/architecture;
- CPU;
- RAM;
- accelerator availability;
- free disk;
- installed runtimes;
- model inventory;
- current load;
- accepted task classes.

### Queue (decided — D-006)
Start simple. A durable queue plus worker leases is more important than Kubernetes.

V0 is one coordinator process on the Mac exposing a small HTTP API (`POST /jobs`, `POST /jobs/claim`, `POST /jobs/{id}/heartbeat`, `POST /jobs/{id}/complete|fail`) backed by a SQLite `jobs` table with leases, attempts, idempotency keys, and structured results. Workers are pull-based `systemd` services that only speak this API over Tailscale; they never open the database. The storage behind the API becomes PostgreSQL only when a measured trigger fires (see review §15). Redis, NATS, RabbitMQ, Celery, Temporal, Ray, Nomad, and Kubernetes are explicitly not used in V0.

### Node roster (decided — D-005)
- `mac-m4pro`: coordinator, all model inference, transcription, vision, T7 host.
- `dell-xeon`: first Linux worker; also hosts the on-LAN second backup copy on its internal disk.
- `dell-i7`: second Linux worker, provisioned only after `dell-xeon` passes its acceptance gate; reserved as the future dedicated Kronos execution box if live trading ever happens.
- `dell-lite` (8 GB, broken display): **retired**. It is not worth a service identity, a Tailscale node, and a patch cadence. If an always-on tiny node is ever needed, a used mini PC is cheaper than the maintenance.

## Explicit non-goals for V0

- Kubernetes;
- distributed tensor parallelism;
- multi-node training;
- self-modifying infrastructure;
- autonomous root access;
- arbitrary agent-to-agent shell access;
- shared writable home directories.

## Networking

Preferred:
- wired Ethernet for stationary nodes;
- private LAN;
- SSH keys, no password SSH;
- host firewall;
- no worker exposed directly to the public Internet;
- remote access via a private overlay/VPN if required.

Decided (D-007): Tailscale (personal plan) with ACL tags `tag:coordinator`, `tag:worker`, `tag:quant`; Tailscale SSH; no port forwarding. Raw WireGuard is not worth the key management for three nodes. The LAN stays a plain LAN; nothing listens on `0.0.0.0` except SSH and the coordinator API bound to the Tailscale interface.

## Isolation

Jobs run in bounded environments from the first worker, not "eventually":
- Linux workers: each job is a transient `systemd-run` unit with `DynamicUser=yes`, `ProtectHome=yes`, `ProtectSystem=strict`, `PrivateTmp=yes`, a per-job `BindPaths=` workspace, `MemoryMax=`, `CPUQuota=`, `RuntimeMaxSec=`, and `PrivateNetwork=yes` unless the job class declares network. Rootless Podman is used only for job classes that need a full container image (builds with system deps).
- Mac: untrusted inputs (video, unknown repos) are processed on a Linux worker whenever one exists. Until then, `sandbox-exec` profiles with no `~/` access, as `video-to-skill` does.
- Explicit working directory, allowlisted mounts, least-privilege service accounts, no `sudo` for any agent identity.

Local AI does not automatically mean trusted AI.
