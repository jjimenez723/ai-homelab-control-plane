# 06 — Software Factory

## Goal

Create a pipeline capable of moving multiple app opportunities through research, specification, implementation, verification, and launch with high automation.

The goal is **pipeline throughput**, not pretending a production-quality business can always be completed in seven days.

UI implementation follows `docs/14-design-system-and-ui-library-policy.md` and the `ui-design-system-builder` skill as a required gate. Every generated app selects a platform-appropriate design system and approved component library, defines tokens and a component map, and documents any justified custom primitives. Apple HIG is the quality baseline for Apple platforms and interaction craft; Android, web, and Flutter conventions remain platform-specific.

## Example portfolio

Current/near-term products include:
- Mafi — game;
- Squad Quit — nicotine cessation product with possible later market localization;
- Brazilian nurse scheduling app — scheduling across multiple hospitals/per-diem style work.

## Stages

```text
Idea
  ↓
Research
  ↓
Evidence / decision gate
  ↓
Product brief
  ↓
Architecture + threat model
  ↓
Design system / UX
  ↓
Implementation plan
  ↓
Bounded coding goals
  ↓
Tests + security + review
  ↓
Staging
  ↓
Launch decision
  ↓
Telemetry / iteration
```

## Agent-readable repository contract

Each serious app should converge toward a small set of durable files such as:

```text
README.md
AGENTS.md
SPEC.md
ARCHITECTURE.md
SECURITY.md
TESTING.md
PLAN.md
PROGRESS.md
DECISIONS.md
docs/
```

Do not create documentation merely for volume. Every document must reduce ambiguity for human and AI contributors.

## Agent delegation

Frontier models:
- strategy;
- spec;
- architecture;
- security;
- difficult review.

Local capable models:
- bounded implementation;
- tests;
- refactors;
- docs updates.

Small models/deterministic tools:
- lint fixes;
- metadata;
- classification;
- logs;
- repetitive transforms.

## Acceptance gates

Each autonomous implementation task should define:
- allowed scope;
- files/areas that must not change;
- commands to run;
- expected artifacts;
- stopping condition;
- escalation condition.

Do not issue "build the whole startup" as one unattended coding task.
