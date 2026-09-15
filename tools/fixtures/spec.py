from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

SUPPORTED_MANIFEST_VERSION = 2

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 60.0
_DEFAULT_CPU_SECONDS = 8
_DEFAULT_MAX_OUTPUT_BYTES = 65536
_DEFAULT_MAX_FILE_SIZE_BYTES = 1_048_576
_DEFAULT_MAX_PROCESSES = 32


@dataclass(frozen=True)
class FixtureSpec:
    """A verifier-owned executable fixture bundle for one code task.

    ``starter_files``/``editable_files`` are what the model may see and
    change. ``probes_path`` points at the verifier-owned probe module whose
    expected values never enter the sandbox at all (see
    ``tools/fixtures/probes.py``); manifest version 2 replaced version 1's
    ``hidden_test_files``, which were copied into the work directory
    alongside the candidate and executed in its process.

    ``timeout_seconds`` bounds one probe; ``total_timeout_seconds`` bounds
    the whole fixture, so a submission cannot turn N probes into N times the
    advertised budget.
    """

    task_id: str
    dir: Path
    instructions_path: Path
    starter_dir: Path
    probes_path: Path
    starter_files: tuple[str, ...]
    editable_files: tuple[str, ...]
    timeout_seconds: float
    total_timeout_seconds: float
    cpu_seconds: int
    max_output_bytes: int
    max_file_size_bytes: int
    max_processes: int


def _require_relative(name: str, field: str) -> None:
    if not name or name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"{field} entry {name!r} must be a plain relative filename")


def load_fixture(task_id: str, *, root: Path = FIXTURES_ROOT) -> FixtureSpec:
    """Load and structurally validate a fixture bundle for ``task_id``.

    Raises on any structural problem (missing/malformed ``fixture.json``,
    missing referenced files). Callers treat a raised exception here as a
    verifier-side error, never as evidence about the candidate submission.
    """
    fixture_dir = root / task_id
    manifest_path = fixture_dir / "fixture.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no fixture bundle for task {task_id!r} at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != SUPPORTED_MANIFEST_VERSION:
        raise ValueError(
            f"unsupported fixture manifest version for {task_id!r}: "
            f"expected {SUPPORTED_MANIFEST_VERSION}, got {manifest.get('version')!r}"
        )
    if manifest.get("task_id") != task_id:
        raise ValueError(f"fixture manifest task_id mismatch: expected {task_id!r}, got {manifest.get('task_id')!r}")

    starter_files = tuple(manifest.get("starter_files") or ())
    editable_files = tuple(manifest.get("editable_files") or ())
    probes_name = manifest.get("probes")
    if not starter_files or not editable_files or not probes_name:
        raise ValueError(f"fixture manifest for {task_id!r} is missing required fields")
    if not set(editable_files) <= set(starter_files):
        raise ValueError(f"fixture manifest for {task_id!r}: editable_files must be a subset of starter_files")

    for name in starter_files:
        _require_relative(name, "starter_files")
    _require_relative(str(probes_name), "probes")

    starter_dir = fixture_dir / "starter"
    probes_path = fixture_dir / str(probes_name)
    instructions_path = fixture_dir / str(manifest.get("instructions", "task.md"))

    for name in starter_files:
        if not (starter_dir / name).is_file():
            raise FileNotFoundError(f"fixture {task_id!r} missing starter file {name!r}")
    if not probes_path.is_file():
        raise FileNotFoundError(f"fixture {task_id!r} missing probes module {probes_name!r}")
    if not instructions_path.is_file():
        raise FileNotFoundError(f"fixture {task_id!r} missing instructions file {instructions_path.name!r}")

    limits = manifest.get("limits") or {}
    return FixtureSpec(
        task_id=task_id,
        dir=fixture_dir,
        instructions_path=instructions_path,
        starter_dir=starter_dir,
        probes_path=probes_path,
        starter_files=starter_files,
        editable_files=editable_files,
        timeout_seconds=float(limits.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
        total_timeout_seconds=float(limits.get("total_timeout_seconds", _DEFAULT_TOTAL_TIMEOUT_SECONDS)),
        cpu_seconds=int(limits.get("cpu_seconds", _DEFAULT_CPU_SECONDS)),
        max_output_bytes=int(limits.get("max_output_bytes", _DEFAULT_MAX_OUTPUT_BYTES)),
        max_file_size_bytes=int(limits.get("max_file_size_bytes", _DEFAULT_MAX_FILE_SIZE_BYTES)),
        max_processes=int(limits.get("max_processes", _DEFAULT_MAX_PROCESSES)),
    )


def available_fixtures(*, root: Path = FIXTURES_ROOT) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir() if (p / "fixture.json").is_file()))
