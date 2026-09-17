"""Angel Naming Lab: a local-only dev/eval tool for filename-generation prompts.

Not part of production naming. Lives outside `tools/ingest/naming/` and
`tools/ingest/triage/` on purpose -- see DECISIONS.md D-054 and
`docs/23-naming-lab.md` for why this package is the one place that may
combine `tools.ingest.evidence` (OCR/vision perception) with naming
evidence, and why that combination is still a deliberate, reviewed
evaluation-only bridge rather than a change to production's
"evidence is perception, never authority" invariant.
"""
