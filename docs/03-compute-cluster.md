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

### Queue
Start simple. A durable queue plus worker leases is more important than Kubernetes.

Possible implementations to evaluate later:
- Redis + worker framework;
- NATS/JetStream;
- RabbitMQ;
- PostgreSQL-backed queue;
- lightweight bespoke queue only if requirements remain tiny.

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

Evaluate Tailscale/WireGuard rather than router port forwarding.

## Isolation

Jobs should eventually run in bounded environments:
- container where practical;
- explicit working directory;
- CPU/RAM/time limits;
- allowlisted mounts;
- least-privilege service accounts.

Local AI does not automatically mean trusted AI.
