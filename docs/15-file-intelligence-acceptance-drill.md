# 15 — File intelligence acceptance drill

A drill you run by hand, once, before trusting the pipeline with anything that
matters. It uses throwaway files in a scratch directory and a scratch T7 root.
**It never points at your real Desktop, Documents, Downloads, Screenshots
folder, or the real Angel volume.**

Ten things get demonstrated, each separately:

1. a badly named screenshot is renamed but **not** selected for Angel;
2. a badly named screenshot is renamed **and**, once you say so, selected
   for Angel;
3. a useful existing filename is left alone;
4. a uselessly named image gets a meaningful name even though it is not kept;
5. an installer in Downloads is ignored and not moved;
6. a durable document is ingested through the verified transaction path;
7. disconnecting Angel prevents ingestion and any source deletion;
8. a second run changes nothing;
9. a name you chose yourself survives every future scan;
10. the perception layer sees what is in a file and changes no decision.

Budget about ten minutes.

> **Scripted version (2026-09-18):** `python -m tools.acceptance.drill --root /tmp/angel-drill --evidence-config <yaml>` synthesizes a 16-file corpus (`tools/acceptance/corpus.py`: PDFs, screenshot, EXIF photo, HEIC, xlsx, source, zip, m4a speech, MP4 with speech / on-screen text / weak evidence / burned-in prompt injection, corrupt MP4, injection PDF, installer) and runs every demonstration below plus injection-inertness, unplug/reconnect, idempotency, and byte-level survival, writing `drill-report.md`. It refuses to run under your real working folders or `/Volumes/`. This hand-run version stays as the narrative.

---

## 0. Set up a scratch world

Nothing here is inside your home directory's real folders.

```bash
cd ~/Projects/ai-homelab-control-plane

export DRILL="$(mktemp -d /tmp/angel-drill.XXXXXX)"
mkdir -p "$DRILL"/{Downloads,Screenshots,T7,state}
echo "$DRILL"
```

Write a drill config. It points every source at the scratch directories and
uses `storage_root_override` for a fake T7 — which is exactly why it also has
to pass `--allow-dev-storage-root` on every command below. That flag exists so
a config file alone can never disable the volume-UUID check (D-026).

```bash
cat > "$DRILL/ingest.yaml" <<YAML
t7:
  volume_uuid: null
  storage_root_override: "$DRILL/T7"
  inbox_relative_path: "00-inbox/organized"
  system_relative_path: "90-system"

sources:
  desktop: "$DRILL/UNUSED-desktop"
  documents: "$DRILL/UNUSED-documents"
  downloads: "$DRILL/Downloads"
  sd_volume_uuids: []
  screenshots:
    path: "$DRILL/Screenshots"
    enabled: true

settle_seconds: 1
downloads_watcher_min_settle_seconds: 1200

naming:
  enabled: true
  source_classes: [screenshots, downloads, sd]
  min_confidence: 0.55

triage:
  enabled: true
  auto_ingest: false
  min_ingest_confidence: 0.7

automation:
  enabled: false
  # An absolute state directory keeps the drill's ledger and run records inside
  # $DRILL instead of in your checkout, so cleanup is `rm -rf "$DRILL"` and
  # nothing else.
  state_dir: "$DRILL/state"
YAML
```

One thing to know before starting: `state_relative_path` is validated to be a
path *inside the checkout*, so the drill's ledger, run lock, and logs land in
`.state/drill/` in this repository rather than under `$DRILL`. That directory is
gitignored, and the cleanup step removes it. Re-running the drill from the top
without removing it first will make `organize status` count the previous run's
files as well.

A shorthand for the rest of the drill:

```bash
organize() { .venv/bin/python -m tools.ingest.cli "$@" --config "$DRILL/ingest.yaml" --allow-dev-storage-root; }
```

Create the test files. All of them are disposable.

```bash
.venv/bin/python - <<PY
import os, time
from pathlib import Path
from PIL import Image

drill = Path(os.environ["DRILL"])
old = time.time() - 7200          # old enough to count as settled

def settled(path):
    os.utime(path, (old, old)); return path

# 1+2. Two badly named screenshots. One is a disposable fragment, one is real.
Image.new("RGB", (48, 48), (10, 10, 10)).save(drill / "Screenshots/Screenshot 2026-09-12 at 09.01.00 AM.png")
Image.new("RGB", (2560, 1440), (30, 60, 90)).save(drill / "Screenshots/Screenshot 2026-09-12 at 10.14.32 AM.png")

# 3. A filename a person clearly chose.
(drill / "Downloads/Fine-Line-Group-2026-SOW.pdf").write_bytes(b"%PDF-1.4 scope of work " + b"y" * 40000)

# 4. A uselessly named photo.
Image.new("RGB", (4032, 3024), (200, 60, 10)).save(drill / "Downloads/IMG_6909.png")

# 5. An installer.
(drill / "Downloads/GoogleChrome.dmg").write_bytes(b"x" * 50000)

# 9. A file to rename by hand later.
Image.new("RGB", (1600, 900), (90, 20, 40)).save(drill / "Downloads/IMG_7000.png")

for folder in ("Downloads", "Screenshots"):
    for path in (drill / folder).iterdir():
        settled(path)
print("fixtures ready in", drill)
PY
```

---

## 1. Look before you touch

```bash
organize doctor
organize triage all --dry-run
```

`doctor` should show naming on, `triage.auto_ingest` **false**, automation
**disabled**, and the screenshots path you configured. The dry run should end
with `Dry run. No files were renamed, moved, or removed.` Confirm nothing
changed:

```bash
ls "$DRILL/Downloads" "$DRILL/Screenshots"
```

---

## 2. Naming, with nothing kept — demonstrations 1, 3, 4, 5

```bash
organize triage all --apply
ls "$DRILL/Downloads" "$DRILL/Screenshots"
ls -R "$DRILL/T7"
```

What to check, one line each:

| # | Expect |
|---|--------|
| 1 | Both screenshots now have date-prefixed descriptive names, and **both are still in `Screenshots/`**. Nothing went to the T7. |
| 3 | `Fine-Line-Group-2026-SOW.pdf` still has exactly that name. |
| 4 | `IMG_6909.png` is now `2026-09-12_photo-4032x3024.png`, still in `Downloads/`. Note `photo`, not `screen-capture`: the probe's PNG-without-EXIF hint is not strong enough to assert an origin in a filename. |
| 5 | `GoogleChrome.dmg` is untouched — same name, same place. |

The T7 inbox should be empty or absent: `triage.auto_ingest` is false, so this
run renamed things and moved nothing. That is the whole point — **naming is not
retention.**

Then look at what it decided and why:

```bash
organize status
organize review
```

`review` lists what is waiting on you. Nothing in it has been moved.

---

## 3. Ingestion through the verified path — demonstrations 2 and 6

Now turn on auto-ingest, which is the deliberate act that lets files move.

```bash
sed -i '' 's/  auto_ingest: false/  auto_ingest: true/' "$DRILL/ingest.yaml"
grep -n auto_ingest "$DRILL/ingest.yaml"
```

Record the document's hash first, so you can prove the bytes arrived intact:

```bash
shasum -a 256 "$DRILL/Downloads/Fine-Line-Group-2026-SOW.pdf"
organize triage all --apply
```

Check:

```bash
find "$DRILL/T7/00-inbox" -type f
shasum -a 256 "$DRILL/T7/00-inbox/organized/downloads/"*.pdf
ls "$DRILL/Downloads"
```

* **6** — the PDF is now on the T7, its SHA-256 matches what you recorded, and
  it is gone from `Downloads/`. The source was removed only after the copy was
  hash-verified and the catalog committed.
* **2** — see the next section. Both screenshots land in `review`, because
  telling a disposable UI state from a durable design reference needs OCR and
  image understanding, which this phase deliberately does not build. The system
  does not guess; it asks you.

To see what it decided and why:

```bash
.venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path

ledger = Path(os.environ["DRILL"]) / "state/local-ledger.sqlite"
conn = sqlite3.connect(ledger)
for row in conn.execute(
    "SELECT current_filename, retention_recommendation, retention_reason "
    "FROM local_files WHERE source_class = 'screenshots'"
):
    print(row)
PY
```

The reason string is the point: the system says *why* it will not decide, and
leaves the file alone rather than guessing.

---

## 3a. You decide — demonstration 2

`review` is a queue, not a list. A screenshot the system could not judge is one
*you* can, and `keep` is how you say so. Turn auto-ingest back off first, to
show this does not depend on it:

```bash
sed -i '' 's/  auto_ingest: true/  auto_ingest: false/' "$DRILL/ingest.yaml"

SHOT="$DRILL/Screenshots/2026-09-12_screen-capture-2560x1440.png"
shasum -a 256 "$SHOT"
organize keep "$SHOT" --reason "client feedback on the kitchen layout"
organize triage all --apply
```

Check:

```bash
ls "$DRILL/Screenshots"
find "$DRILL/T7/00-inbox" -type f
shasum -a 256 "$DRILL/T7/00-inbox/organized/screenshots/"*.png
```

* **2** — the screenshot you renamed in step 2 is now on the T7 under that same
  generated name, its SHA-256 matches what you recorded, and it is gone from
  `Screenshots/`. It moved because *you* said so, through the same verified
  transfer path, with `auto_ingest` off.

`organize ignore PATH` is the mirror image and is worth trying too: it marks a
file as not-for-Angel, and the file **stays exactly where it is**. Ignoring is
never deleting.

---

## 4. Angel disconnected — demonstration 7

```bash
mv "$DRILL/T7" "$DRILL/T7-unplugged"

.venv/bin/python - <<'PY'
import os, time
from pathlib import Path
from PIL import Image
drill = Path(os.environ["DRILL"])
p = drill / "Downloads/IMG_8100.png"
Image.new("RGB", (3000, 2000), (10, 120, 40)).save(p)
old = time.time() - 7200
os.utime(p, (old, old))
PY

organize triage all --apply
ls "$DRILL/Downloads"
```

Expect: `Angel: not connected (...)`, **no file removed from `Downloads/`**, and
the new `IMG_8100.png` renamed anyway. Naming survives an unplugged drive
because its ledger is on the Mac; ingestion is the only thing that stops. Put
the drive back:

```bash
mv "$DRILL/T7-unplugged" "$DRILL/T7"
```

---

## 5. Idempotency — demonstration 8

```bash
ls "$DRILL/Downloads" "$DRILL/Screenshots" > "$DRILL/before.txt"
organize triage all --apply
ls "$DRILL/Downloads" "$DRILL/Screenshots" > "$DRILL/after.txt"
diff "$DRILL/before.txt" "$DRILL/after.txt" && echo "IDENTICAL — nothing churned"
```

The run should report `0 renamed`. A second pass must never rename a file it
already named.

---

## 6. Your name wins — demonstration 9

Rename a file yourself, the way you actually would — in Finder or with `mv` —
then tell the pipeline you chose it:

```bash
cd "$DRILL/Downloads"
mv 2026-09-12_photo-1600x900.png "roof-survey-north-elevation.png" 2>/dev/null \
  || mv "$(ls | grep -E '^2026.*1600x900' | head -1)" "roof-survey-north-elevation.png"
cd - >/dev/null

organize correct-name "$DRILL/Downloads/roof-survey-north-elevation.png"
organize triage all --apply
ls "$DRILL/Downloads"
```

`roof-survey-north-elevation.png` must still be called exactly that. Run it a
third time if you like; a human-authored name outranks every future model
output, permanently.

Check the audit trail survived all of it:

```bash
organize status
organize duplicates
```

---

## 6a. What the file actually contains — perception, with no authority

The evidence layer ran during every `organize triage` above. It read the
Markdown and PDF text, described the images, and cached all of it keyed by
content hash. It changed none of the decisions you just watched.

```bash
organize evidence
organize evidence "$DRILL/Downloads/roof-survey-north-elevation.png"
```

The summary reports how much evidence exists, from which producers, at which
statuses. The per-file view reports the content hash the evidence describes,
which routing rules fired, which producer and version answered each stage, the
confidences, and any warnings — and **no extracted text**. Ask for the text
explicitly if you want it:

```bash
organize evidence "$DRILL/Downloads/notes.md" --show-text 2>/dev/null || true
```

Two things worth checking here:

* The default OCR backend is `null`, so a screenshot's OCR stage records
  `backend_unavailable` rather than silently producing nothing. An absent
  backend leaves evidence of its absence.
* Run `organize triage all --apply` once more and watch `evidence_cache_hits`
  rise while nothing is re-measured. Then rename a file by hand and run again:
  the evidence still hits, because the cache is keyed by the bytes and not by
  the name.

Nothing in this step can move, rename, or delete anything. `organize evidence`
is read-only, and the evidence layer as a whole performs no filesystem writes
at all.

---

## 7. Clean up

```bash
rm -rf "$DRILL" .state/drill
unset DRILL
```

The drill created exactly two things: `$DRILL`, and `.state/drill` inside this
checkout. Nothing else was touched at any point.

---

## What this drill does not prove

Worth being explicit about, so the green result is not read as more than it is:

* It does not exercise the real Angel volume or the volume-UUID check —
  `storage_root_override` bypasses UUID resolution by design, which is why it
  demands an explicit flag. The UUID path is covered by
  `tests/ingest/test_t7_preflight.py`.
* It does not test the scheduled LaunchAgent. That is opt-in separately
  (`automation.enabled: true`, then
  `scripts/install-file-intelligence-launchd`), and an automated run waits
  1200 s of file stillness, which is not a thing to sit through by hand.
* It does not prove anything about semantic naming quality. The deterministic
  proposer names from measured facts only; a screenshot becomes
  `2026-09-12_screen-capture-2560x1440.png`, not
  `2026-09-12_github-actions-python-tests-failing.png`. Reading what a
  screenshot *shows* is now possible — see step 8 and
  `docs/17-file-evidence.md` — but that perception is deliberately not an
  input to naming yet (D-029). When it becomes one it will do so through a
  `NameProposer`, and nothing downstream will change.
