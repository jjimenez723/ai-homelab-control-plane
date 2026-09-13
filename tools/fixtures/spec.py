from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_CPU_SECONDS = 8
_DEFAULT_MAX_OUTPUT_BYTES = 65536
_DEFAULT_MAX_FILE_SIZE_BYTES = 1_048_576
_DEFAULT_MAX_PROCESSES = 32


@dataclass(frozen=True)
class FixtureSpec:
    """A verifier-owned executable fixture bundle for one code task.

    ``starter_files``/``editable_files`` are what the model may see and
    change. ``hidden_test_files`` are never shown to the model and are
    copied into the disposable work directory only at execution time, after
    the submission already exists.
    """

    task_id: str
    dir: Path
    instructions_path: Path
    starter_dir: Path
    hidden_dir: Path
    starter_files: tuple[str, ...]
    editable_files: tuple[str, ...]
    hidden_test_files: tuple[str, ...]
    run_module: str
    timeout_seconds: float
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
    if manifest.get("version") != 1:
        raise ValueError(f"unsupported fixture manifest version for {task_id!r}")
    if manifest.get("task_id") != task_id:
        raise ValueError(f"fixture manifest task_id mismatch: expected {task_id!r}, got {manifest.get('task_id')!r}")

    starter_files = tuple(manifest.get("starter_files") or ())
    editable_files = tuple(manifest.get("editable_files") or ())
    hidden_test_files = tuple(manifest.get("hidden_test_files") or ())
    run_module = manifest.get("run_module")
    if not starter_files or not editable_files or not hidden_test_files or not run_module:
        raise ValueError(f"fixture manifest for {task_id!r} is missing required fields")
    if not set(editable_files) <= set(starter_files):
        raise ValueError(f"fixture manifest for {task_id!r}: editable_files must be a subset of starter_files")

    for name in starter_files:
        _require_relative(name, "starter_files")
    for name in hidden_test_files:
        _require_relative(name, "hidden_test_files")

    starter_dir = fixture_dir / "starter"
    hidden_dir = fixture_dir / "hidden"
    instructions_path = fixture_dir / str(manifest.get("instructions", "task.md"))

    for name in starter_files:
        if not (starter_dir / name).is_file():
            raise FileNotFoundError(f"fixture {task_id!r} missing starter file {name!r}")
    for name in hidden_test_files:
        if not (hidden_dir / name).is_file():
            raise FileNotFoundError(f"fixture {task_id!r} missing hidden test file {name!r}")
    if not instructions_path.is_file():
        raise FileNotFoundError(f"fixture {task_id!r} missing instructions file {instructions_path.name!r}")

    limits = manifest.get("limits") or {}
    return FixtureSpec(
        task_id=task_id,
        dir=fixture_dir,
        instructions_path=instructions_path,
        starter_dir=starter_dir,
        hidden_dir=hidden_dir,
        starter_files=starter_files,
        editable_files=editable_files,
        hidden_test_files=hidden_test_files,
        run_module=str(run_module),
        timeout_seconds=float(limits.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)),
        cpu_seconds=int(limits.get("cpu_seconds", _DEFAULT_CPU_SECONDS)),
        max_output_bytes=int(limits.get("max_output_bytes", _DEFAULT_MAX_OUTPUT_BYTES)),
        max_file_size_bytes=int(limits.get("max_file_size_bytes", _DEFAULT_MAX_FILE_SIZE_BYTES)),
        max_processes=int(limits.get("max_processes", _DEFAULT_MAX_PROCESSES)),
    )


def available_fixtures(*, root: Path = FIXTURES_ROOT) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir() if (p / "fixture.json").is_file()))
