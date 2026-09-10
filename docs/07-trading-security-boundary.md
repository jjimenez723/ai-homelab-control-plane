# 07 — Trading Assistant Security Boundary

## Context

`kronos-quant-lab` is a high-risk financial research system and must remain separate from the general AI homelab trust domain.

Its current design philosophy should remain:
- research/paper-trading first;
- deterministic safety/risk gates;
- AI may propose but cannot independently authorize real-money execution;
- live trading requires separately reviewed, explicit human authorization.

## Separation model

```text
GENERAL AI HOMELAB
media / app coding / indexing / research
             |
      sanitized interfaces only
             |
     +-------X--------+
     | TRUST BOUNDARY |
     +-------X--------+
             |
TRADING RESEARCH DOMAIN
market data / Kronos / experiments
             |
 deterministic risk gate
             |
 paper broker
             |
    [future explicit live gate]
```

## Rules

1. Do not grant general-purpose coding/media agents broker credentials.
2. Do not place broker/API secrets on the shared T7 project tree.
3. Do not allow arbitrary worker nodes to submit orders.
4. Keep trading execution on a dedicated service identity and preferably a dedicated runtime/node/VM/container boundary.
5. Network access to broker endpoints should be restricted to the execution component when practical.
6. Order intents must be structured, validated, logged, and risk-checked.
7. The AI model output is untrusted input to deterministic trading code.
8. Live-capital enablement must require a deliberate human-controlled configuration/credential transition.
9. Preserve append-only evidence/audit logs for experiment promotion and execution decisions.
10. Treat model files, market data, prompts, research artifacts, execution credentials, and broker state as different data classes.

## T7 placement

A clean clone can live at:

```text
T7/10-code/research/kronos-quant-lab/
```

But:
- credentials stay elsewhere;
- live broker state does not belong on shared portable storage;
- bulky temporary backtest artifacts may be placed in a dedicated data area with retention rules;
- the Git remote remains authoritative for source history.

## Development can continue independently

The cluster project should not block Kronos development.

Initially, Kronos can continue using the Mac exactly as it does today. Later the cluster may provide:
- isolated backtest workers;
- data preprocessing;
- model inference workers;
- research batch execution.

The execution/risk boundary should **not** be generalized into the shared cluster scheduler without a separate security review.
