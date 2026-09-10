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
