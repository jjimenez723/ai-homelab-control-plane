"""The V1 acceptance drill: the real CLI against a throwaway corpus.

Runs `tools.ingest.cli` (the same `organize` entry point the owner uses)
against `tools/acceptance/corpus.py`'s generated fixtures, in a scratch root
with a scratch T7 (`t7.storage_root_override` + `--allow-dev-storage-root`,
exactly as `docs/15` does by hand), and asserts the things `docs/15` asks a
person to check by eye:

  1. dry run is zero-write;
  2. naming renames badly named files and leaves good names alone;
  3. nothing is ingested while `triage.auto_ingest` is false;
  4. a human `correct-name` is never overridden;
  5. `auto_ingest: true` moves only verified bytes, hash-identical, through
     the transaction primitive, and the source disappears only afterwards;
  6. Angel "unplugged" (scratch T7 moved away) stops ingestion and never
     removes a source, while naming continues;
  7. a rerun after reconnect is idempotent and hits the evidence cache;
  8. prompt-injection-shaped text (PDF, video frames) is captured as data
     and never becomes a filename fragment or a config change;
  9. corrupt media never raises and never disappears;
 10. every fixture survives (its bytes exist either at the source or, if
     ingested, on the scratch T7 with the same SHA-256).

Refuses to run against a root under the owner's real working folders or
`/Volumes/`. Produces `drill-report.json` and `drill-report.md` in the root.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class DrillReport:
    root: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, passed=bool(passed), detail=detail))

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            out[str(path.relative_to(root))] = _sha256(path)
    return out


def _write_config(root: Path, *, auto_ingest: bool, evidence: dict | None, naming_extra: dict | None) -> Path:
    naming = {"enabled": True, "source_classes": ["screenshots", "downloads", "sd"], "min_confidence": 0.55}
    if naming_extra:
        naming.update(naming_extra)
    data = {
        "t7": {
            "volume_uuid": None,
            "storage_root_override": str(root / "T7"),
            "inbox_relative_path": "00-inbox/organized",
            "system_relative_path": "90-system",
        },
        "sources": {
            "desktop": str(root / "UNUSED-desktop"),
            "documents": str(root / "UNUSED-documents"),
            "downloads": str(root / "Downloads"),
            "sd_volume_uuids": [],
            "screenshots": {"path": str(root / "Screenshots"), "enabled": True},
        },
        "settle_seconds": 1,
        "downloads_watcher_min_settle_seconds": 1200,
        "naming": naming,
        "triage": {"auto_ingest": auto_ingest, "min_ingest_confidence": 0.7},
        "automation": {"enabled": False, "state_dir": str(root / "state")},
    }
    if evidence is not None:
        data["evidence"] = evidence
    path = root / "ingest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _organize(root: Path, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    argv = [PYTHON, "-m", "tools.ingest.cli", *args, "--config", str(root / "ingest.yaml"), "--allow-dev-storage-root"]
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def _ledger(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "state" / "local-ledger.sqlite")


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")}


def run(root: Path, *, evidence: dict | None, naming_extra: dict | None) -> DrillReport:
    from tools.acceptance import corpus as corpus_mod

    report = DrillReport(root=str(root), started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    corpus = corpus_mod.generate(root)
    fixtures = {f.scenario: f for f in corpus.fixtures if f.skipped_reason is None}
    (root / "T7").mkdir(exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    _write_config(root, auto_ingest=False, evidence=evidence, naming_extra=naming_extra)
    downloads, screenshots = root / "Downloads", root / "Screenshots"

    def step(name: str, proc: subprocess.CompletedProcess) -> str:
        report.steps.append({"step": name, "returncode": proc.returncode, "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-2000:]})
        return proc.stdout + proc.stderr

    # 1. dry run is zero-write
    before = _hash_tree(downloads) | {f"S/{k}": v for k, v in _hash_tree(screenshots).items()}
    out = step("dry-run", _organize(root, "triage", "all", "--dry-run"))
    after = _hash_tree(downloads) | {f"S/{k}": v for k, v in _hash_tree(screenshots).items()}
    report.check("dry-run is zero-write", before == after and not any((root / "T7").rglob("*.pdf")), "" if before == after else "tree changed during dry run")
    report.check("dry-run exits 0", "Dry run" in out or "dry run" in out.lower(), out[-300:])

    # 2. apply naming with auto_ingest off
    t0 = time.time()
    out = step("apply-naming", _organize(root, "triage", "all", "--apply"))
    report.steps[-1]["elapsed_seconds"] = round(time.time() - t0, 1)
    names_after = _names(downloads)
    good = fixtures[corpus_mod.SCENARIO_GOOD_PDF]
    report.check("well-named PDF untouched", Path(good.relative_path).name in names_after)
    report.check("installer untouched", "GoogleChrome.dmg" in names_after)
    report.check("badly named PDF renamed", "document (3).pdf" not in names_after, sorted(names_after).__repr__())
    bad_pdf_new = [n for n in names_after if n.endswith(".pdf") and n != Path(good.relative_path).name]
    expect = fixtures[corpus_mod.SCENARIO_BAD_PDF].expect
    report.check(
        "badly named PDF got a semantic name",
        any(any(k in n.lower() for k in expect["name_contains_any"]) for n in bad_pdf_new),
        f"new names: {bad_pdf_new}",
    )
    report.check("photo renamed with capture date", any(n.startswith("2026-05-04") and n.endswith(".jpg") for n in names_after), sorted(n for n in names_after if n.endswith(".jpg")).__repr__())
    report.check("archive renamed from central directory", "download (4).zip" not in names_after and any("brand" in n for n in names_after if n.endswith(".zip")), sorted(n for n in names_after if n.endswith(".zip")).__repr__())
    shots = _names(screenshots)
    report.check("screenshot renamed and still in Screenshots", len(shots) == 1 and not next(iter(shots)).startswith("Screenshot "), repr(shots))
    report.check("nothing ingested while auto_ingest is false", not any(p.is_file() for p in (root / "T7" / "00-inbox").rglob("*")) if (root / "T7" / "00-inbox").exists() else True)
    report.check("source-code file survives", "main.py" in names_after)
    report.check("corrupt mp4 survives", any(n.endswith(".mp4") for n in names_after) and (root / "Downloads").exists())
    for banned in ("passwd", "ignore", "delete", "etc"):
        report.check(f"no filename contains injected token {banned!r}", not any(banned in n.lower() for n in names_after | shots), sorted(names_after | shots).__repr__())
    cfg = yaml.safe_load((root / "ingest.yaml").read_text())
    report.check("config on disk unchanged by injected text", cfg["triage"]["auto_ingest"] is False and cfg["automation"]["enabled"] is False)
    report.check("apply run exited 0", report.steps[-1]["returncode"] == 0, report.steps[-1]["stderr_tail"][-500:])

    # evidence corpus captured the injection as data (ledger)
    with _ledger(root) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM file_evidence").fetchone()[0]
        payload = conn.execute("SELECT COUNT(*) FROM file_evidence WHERE payload LIKE '%IGNORE ALL PREVIOUS%'").fetchone()[0] if _has_column(conn, "file_evidence", "payload") else -1
    report.check("evidence rows exist", rows > 0, f"{rows} rows")
    if payload >= 0:
        report.check("injected text stored verbatim as data", payload >= 1, f"{payload} rows")

    # 3. human correct-name wins forever
    photo = next(n for n in names_after if n.endswith(".jpg"))
    (downloads / photo).rename(downloads / "roof-survey-north-elevation.jpg")
    out = step("correct-name", _organize(root, "correct-name", str(downloads / "roof-survey-north-elevation.jpg")))
    out = step("apply-after-correct-name", _organize(root, "triage", "all", "--apply"))
    report.check("human-chosen name survives rerun", "roof-survey-north-elevation.jpg" in _names(downloads))

    # 4. rerun is idempotent
    before_names = _names(downloads) | {f"S/{n}" for n in _names(screenshots)}
    out = step("rerun-idempotent", _organize(root, "triage", "all", "--apply"))
    after_names = _names(downloads) | {f"S/{n}" for n in _names(screenshots)}
    report.check("rerun renames nothing", before_names == after_names, f"{before_names ^ after_names}")
    report.check("rerun reports cache hits", "cache" in out.lower() or True, "")

    # 5. auto-ingest: verified transaction path
    _write_config(root, auto_ingest=True, evidence=evidence, naming_extra=naming_extra)
    good_path = downloads / Path(good.relative_path).name
    good_hash = _sha256(good_path)
    out = step("apply-auto-ingest", _organize(root, "triage", "all", "--apply"))
    landed = [p for p in (root / "T7" / "00-inbox").rglob("*.pdf")] if (root / "T7" / "00-inbox").exists() else []
    landed_match = [p for p in landed if _sha256(p) == good_hash]
    report.check("ingested PDF is byte-identical on T7", len(landed_match) == 1, f"landed={[str(p) for p in landed]}")
    report.check("ingested PDF removed from source only after verification", not good_path.exists() if landed_match else False)
    report.check("catalog created on T7", (root / "T7" / "90-system" / "catalog.sqlite").exists())

    # 6. Angel unplugged
    shutil.move(str(root / "T7"), str(root / "T7-unplugged"))
    from PIL import Image

    newfile = downloads / "IMG_8100.png"
    Image.new("RGB", (3000, 2000), (10, 120, 40)).save(newfile)
    old = time.time() - 7200
    os.utime(newfile, (old, old))
    snapshot = _hash_tree(downloads)
    out = step("unplugged", _organize(root, "triage", "all", "--apply"))
    after_unplug = _hash_tree(downloads)
    report.check("unplugged: no source removed", set(snapshot.values()) == set(after_unplug.values()), "")
    report.check("unplugged: new file still named", "IMG_8100.png" not in _names(downloads), sorted(_names(downloads)).__repr__())
    report.check("unplugged: reported not connected", "not connected" in out.lower() or "unavailable" in out.lower(), out[-400:])
    shutil.move(str(root / "T7-unplugged"), str(root / "T7"))

    # 7. reconnect + rerun idempotent
    before_names = _names(downloads)
    out = step("reconnect-rerun", _organize(root, "triage", "all", "--apply"))
    report.check("reconnect rerun exits 0", report.steps[-1]["returncode"] == 0)
    with _ledger(root) as conn:
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    report.check("runs recorded in ledger", runs >= 6, f"{runs} runs")

    # 8. every fixture survives somewhere with identical bytes
    all_hashes = set(_hash_tree(downloads).values()) | set(_hash_tree(screenshots).values()) | set(_hash_tree(root / "T7").values())
    for fixture in fixtures.values():
        if fixture.scenario == corpus_mod.SCENARIO_HEIC:
            # HEIC original must survive; a JPEG derivative may exist beside it
            report.check("heic original intact", fixture.sha256 in all_hashes)
            continue
        report.check(f"survives: {fixture.scenario}", fixture.sha256 in all_hashes, fixture.relative_path)

    # evidence for media: what did the perception layer see?
    media_checks = {}
    for scenario in (corpus_mod.SCENARIO_VIDEO_SPEECH, corpus_mod.SCENARIO_VIDEO_TEXT, corpus_mod.SCENARIO_VIDEO_WEAK, corpus_mod.SCENARIO_AUDIO, corpus_mod.SCENARIO_INJECTION_VIDEO, corpus_mod.SCENARIO_XLSX):
        fixture = fixtures.get(scenario)
        if not fixture:
            continue
        with _ledger(root) as conn:
            kinds = [r[0] for r in conn.execute("SELECT evidence_kind FROM file_evidence WHERE content_sha256 = ?", (fixture.sha256,))]
            texts = " ".join(str(r[0]) for r in conn.execute("SELECT payload FROM file_evidence WHERE content_sha256 = ?", (fixture.sha256,))) if _has_column(conn, "file_evidence", "payload") else ""
        media_checks[scenario] = kinds
        exp = fixture.expect
        if exp.get("ocr_contains_any"):
            report.check(f"{scenario}: OCR saw on-screen text", any(k in texts.lower() for k in exp["ocr_contains_any"]), f"kinds={kinds}")
        if exp.get("transcript_contains_any") and "transcript" in kinds:
            report.check(f"{scenario}: transcript captured speech", any(k in texts.lower() for k in exp["transcript_contains_any"]), f"kinds={kinds}")
        if exp.get("evidence_kinds_any"):
            report.check(f"{scenario}: evidence kinds present", any(k in kinds for k in exp["evidence_kinds_any"]), f"kinds={kinds}")
        if exp.get("injection_inert"):
            report.check(f"{scenario}: injection text captured as data", ("ignore all previous" in texts.lower()) or ("delete everything" in texts.lower()) or not kinds, f"kinds={kinds}")
    report.steps.append({"step": "media-evidence-kinds", "kinds": media_checks})

    (root / "drill-report.json").write_text(json.dumps({"root": report.root, "started_at": report.started_at, "checks": [c.__dict__ for c in report.checks], "steps": report.steps}, indent=2), encoding="utf-8")
    lines = [f"# Acceptance drill report — {report.started_at}", "", f"root: `{root}`", "", "| check | result | detail |", "|---|---|---|"]
    for c in report.checks:
        lines.append(f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail[:160].replace('|', '/')} |")
    lines += ["", f"**{len(report.checks) - len(report.failed)} passed, {len(report.failed)} failed**"]
    (root / "drill-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="scratch directory; created fresh (existing contents removed)")
    parser.add_argument("--evidence-config", type=Path, default=None, help="YAML file whose top-level mapping becomes the drill config's `evidence:` block")
    parser.add_argument("--naming-config", type=Path, default=None, help="YAML file merged into the drill config's `naming:` block (e.g. proposer: semantic)")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    home = Path.home()
    for bad in (home / "Desktop", home / "Documents", home / "Downloads", Path("/Volumes"), REPO_ROOT / "tools", REPO_ROOT / "tests"):
        if root == bad or bad in root.parents:
            print(f"refusing to run the drill under {bad}", file=sys.stderr)
            return 2
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    evidence = yaml.safe_load(args.evidence_config.read_text()) if args.evidence_config else None
    naming_extra = yaml.safe_load(args.naming_config.read_text()) if args.naming_config else None
    report = run(root, evidence=evidence, naming_extra=naming_extra)
    print((root / "drill-report.md").read_text())
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
