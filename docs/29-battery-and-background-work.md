# 29 — Battery, background work, and what Angel actually runs

Measured on `mac-m4pro` on 2026-09-17/18 during the V1 completion pass,
after the owner reported significant battery drain during development. The
question this answers is narrow: **does anything Angel installs, schedules,
or leaves running contribute to that drain?** The method distinguishes four
states for every candidate — exists in source, installed, enabled, actually
running — and reads real counters (`launchctl print`, `ps` cumulative CPU
time, the backup JSONL log) rather than guessing.

## Verdict

Angel's own background footprint on this Mac is **one short job per day**
plus **one short job per week**; nothing Angel ships is resident. The drain
the owner observed is dominated by processes Angel does not own, chiefly
Google Drive desktop sync over a `~/Documents` that holds ~30 GB of Git
working trees with `node_modules`, and by the Mac being kept awake.

## What Angel schedules, and how much it costs

| Job | Source | Installed | Enabled | Running | Measured cost |
|---|---|---|---|---|---|
| `com.ai-homelab.t7-backup` | `launchd/com.ai-homelab.t7-backup.plist.template` | yes (`~/Library/LaunchAgents`) | yes, `StartCalendarInterval` 02:15 daily | one-shot, exits | 15–28 s per successful run (JSONL log 09-12..09-15); < 1 s when Angel is unplugged (exits at the UUID check; last exit code 1 on 09-16 and 09-17 for that reason, not a fault) |
| `com.ai-homelab.t7-check` | `launchd/com.ai-homelab.t7-check.plist.template` | yes | yes, Sunday 03:15 | one-shot, exits | 34–41 s (metadata + 5 % data read) |
| `com.ai-homelab.file-intelligence` | `launchd/com.ai-homelab.file-intelligence.plist.template` | **not installed** | n/a (`automation.enabled: false`, installer refuses) | never | 0 |
| Ollama | Homebrew formula | installed | **no service** (`brew services list`: `ollama none`; no launchd entry) | not running at audit time | 0 while idle; when a campaign runs, whole-model inference is the dominant Mac load and is a deliberate, attended act |
| Naming Lab (Streamlit) | `tools/naming_lab/` | n/a | n/a | only when launched by hand | 0 |
| Router/coordinator/report | `tools/router`, `tools/coordinator`, `tools/report` | n/a | n/a | library code, no daemon | 0 |

There is **no FSEvents watcher, no polling loop, and no resident daemon** in
`tools/` or `scripts/`: the only `while True` loops are bounded read loops in
`hashing.py`/`transaction.py`, a retry helper in `migrations.py`, and a
0.25 s wait inside `ollama_local.py` used only while a model is loading.

## What is actually consuming the battery (top cumulative CPU, `ps -axo time`)

| Process | CPU time / uptime | Angel-owned? |
|---|---|---|
| Google Drive desktop client | 576 min over 28 h (~34 % of one core, continuously) | no |
| WindowServer | 447 min over 18 days | no |
| contactsd | 278 min over 3.3 days | no |
| Google Chrome (GPU helper + browser) | 156 min over 3.3 days | no |
| VS Code renderer/helper processes | ~90 min over 3 days | no (development host) |
| MacWhisper 4 (GUI app, resident 3 days) | 18 min | no |
| Angel backup/check jobs | seconds per day | yes |

Also relevant: `pmset -g` shows sleep prevented by Amphetamine and the Claude
Code extension (`sleep 1 (sleep prevented by Claude, Amphetamine, powerd)`),
so the machine does not sleep on battery while a development session is open.

## Angel defects found and fixed in this pass

- None of the scheduled jobs were wasteful. One improvement was made to
  the *evidence* layer rather than to scheduling: content-keyed caching now
  also covers speech transcription (the most expensive per-file step), so a
  rerun over the same bytes never transcribes twice (docs/17).
- Spotlight is still indexing the Angel volume (`mdutil -s /Volumes/Angel`
  → "Indexing enabled"). Disabling it needs an interactive `sudo mdutil -i
  off /Volumes/Angel`, which this non-interactive pass cannot run; it is
  recorded as an owner action in PLAN.md Phase 1. Time Machine already
  excludes the volume (`tmutil isexcluded` → `[Excluded]`). The T7's USB link
  was repaired after this audit: `ioreg` now reports 10 Gb/s USB 3.x; the old
  34–39 MB/s write probes were USB 2.0 measurements.

## Recommendations outside Angel's authority (owner decisions)

1. Google Drive is syncing `~/Documents`, which contains 16 Git working
   trees including `node_modules`-heavy projects; that is the single largest
   CPU consumer on the machine. Either move active repositories to
   `~/Projects` (the canonical development home, docs/25) or exclude them
   from Drive sync. Angel's inventory of those repositories is in
   `/Volumes/Angel/90-system/migration-reports/repos-inventory.yaml`.
2. Amphetamine keeps the Mac awake on battery; quit it when not needed.
3. MacWhisper 4 stays resident with a Sparkle updater; quit it when idle.

## How to re-measure

```bash
launchctl print "gui/$UID/com.ai-homelab.t7-backup" | grep -E "runs|last exit"
launchctl print "gui/$UID/com.ai-homelab.t7-check"  | grep -E "runs|last exit"
launchctl list | grep ai-homelab            # only the two backup jobs
brew services list | grep ollama            # must be "none"
ps -axo time,etime,command -r | head -20    # who is burning CPU
python3 - <<'PY'
import json; [print(json.loads(l).get("event"), json.loads(l).get("outcome"), json.loads(l).get("elapsed_seconds")) for l in open(".state/backup/logs/backup-2026-09.jsonl")]
PY
```
