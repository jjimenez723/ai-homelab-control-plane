# 08 — Security Threat Model

## Assets

- client media and business information;
- private source code;
- GitHub credentials/tokens;
- model/provider API keys;
- production deployment credentials;
- trading/broker credentials;
- personal documents;
- proprietary research;
- local network access.

## Threats

### Compromised AI/tool invocation
A prompt, repository file, webpage, or media metadata attempts to convince an agent to exfiltrate secrets or execute unsafe commands.

### Supply-chain compromise
Malicious dependency, model file, package, container image, extension, or repository hook.

### Overprivileged worker
A worker has access to unrelated client data, SSH keys, production credentials, or broker access.

### Portable-drive loss
The T7 is physically lost or stolen.

### Destructive automation
Agent deletes/moves/renames data incorrectly or propagates corruption.

### Ransomware/malware
One workstation encrypts/mutates shared storage.

### Accidental publication
Private assets are committed/pushed into a public repository.

## Minimum controls

- encrypted T7 where practical;
- full-disk encryption on nodes;
- unique SSH keys/service identities;
- no public inbound ports;
- firewall;
- private overlay/VPN for remote access;
- least privilege;
- secret scanning;
- `.gitignore` plus server-side secret protections;
- immutable/raw media tier;
- backups disconnected or protected from ordinary worker credentials;
- checksums for important ingestion;
- execution sandboxing;
- dependency/model provenance;
- signed or pinned automation artifacts where practical;
- audit logs for destructive and high-risk actions.

## Trust labels

Suggested:
- `public`
- `internal`
- `confidential`
- `restricted`

`restricted` includes broker credentials and similarly high-impact secrets. Restricted data should never be sent to a model/provider merely because the model is capable.

## AI security rule

Treat model output exactly like input from an untrusted junior automation process: useful, but not inherently authorized or correct.

## V1 adversarial review — 2026-09-18

An adversarial pass over the V1 completion surface, written up here because
several of its findings were places this document's controls were *claimed* and
not *held*. Findings and fixes are D-063; the reproducing tests are
`tests/ingest/test_adversarial_v1.py` (35 cases). Everything below was
probed against temporary directories only — no probe touched the Angel volume
or the owner's folders.

### What was probed and held

- **The transaction primitive.** Source replaced by a symlink or a hard link
  to an unrelated file between the hash and the unlink: refused
  (`source_identity_mismatch`), and the bystander file survives. The residual
  TOCTOU after the last check is bounded by `unlink` removing the *link*
  rather than its target, which is now a test rather than an assumption.
- **Corrupted media.** ~2500 random byte mutations and truncations of
  synthesized mp4/m4a/pdf/xlsx fixtures through `probe_file` and
  `collect_evidence` under a wall-clock budget: zero escapes. Kept as a
  seeded, time-bounded test.
- **Hostile proposals.** Path traversal, absolute paths, NUL, newlines, shell
  metacharacters, RLO, over-length: already covered and still held.
- **Interrupted migration.** Killing the run between every pair of steps and
  re-running loses no row and converges on the current version.
- **The run lock.** Contention, a lock file replaced before it is locked, and
  a lock file unlinked mid-run were already covered, including the honest
  distinction between prevented and merely detected.
- **The skill trust boundary.** Nothing under `tools/` reads
  `40-ai/skills/candidates/` or `50-research/knowledge/` as code; the skills
  export is copy-only and deletes nothing (asserted over `angel_sync`'s AST,
  not its text).
- **Hard-coded machine paths.** No `/Users/`, `/Volumes/Angel` or username
  appears in `tools/` or `scripts/` outside documentation prose and one
  benchmark prompt fixture.

### What was fixed

Filename policy (`naming/policy.py`) let C1 control characters, most Unicode
format characters, lone surrogates and private-use characters through into an
applied filename, and refused a name outright rather than trimming it to the
255-byte component ceiling. Filename quality (`naming/quality.py`) could not
see a word in any non-Latin script, so every non-Latin *owner-authored* name
scored POOR and was therefore eligible for automatic renaming.
`angel-sync archive --name` and the `t7.*_relative_path` config keys could
each steer a write outside the UUID-verified volume root. A fixture `task_id`
could steer the probe loader's `exec_module` outside the reviewed tree.
`organize naming-history` printed the unsanitized `raw_output` column to a
terminal. `scripts/bootstrap-t7.sh`'s "a mounted volume needs
`--expect-uuid`" gate was a string test that a symlink defeated. See D-063.

### Accepted, not fixed

- **A volume swapped for a different one at the same mount path mid-run.**
  `resolve_t7` verifies the UUID once per run; re-verifying per file would
  cost a `diskutil` call per file to defend against physically swapping
  drives during a scan. A destination that merely *vanishes* costs the
  transfer and never the source, which is tested.
- **`raw_output` is stored verbatim and unsanitized.** Deliberate: a
  zero-width character or a NUL is *why* an answer was refused, and a column
  that had silently removed it would show a legal-looking name beside a
  rejection reason that made no sense. The escape-sequence risk is handled at
  the print boundary instead, which also protects rows written earlier.
- **pypdf logs parse failures to stderr during a scan**, quoting (escaped)
  bytes of the file being read. Noisy rather than unsafe, and suppressing a
  third-party library's diagnostics would also suppress the ones worth
  reading.
