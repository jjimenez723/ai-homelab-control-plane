from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "data" / "evaluation" / "phase2_tasks.json"

@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    prompt: str
    expected: Any
    intended_tier: int
    capabilities: tuple[str, ...]
    risk: str
    privacy: str
    verifier: str
    fixture: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Task":
        required = ("id", "kind", "prompt", "expected", "intended_tier", "capabilities", "risk", "privacy", "verifier")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"task missing fields: {', '.join(missing)}")
        return cls(id=str(value["id"]), kind=str(value["kind"]), prompt=str(value["prompt"]),
                   expected=value["expected"], intended_tier=int(value["intended_tier"]),
                   capabilities=tuple(dict.fromkeys(value["capabilities"])), risk=str(value["risk"]),
                   privacy=str(value["privacy"]), verifier=str(value["verifier"]), fixture=value.get("fixture"))

def load_corpus(path: str | Path = DEFAULT_CORPUS) -> list[Task]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("tasks"), list):
        raise ValueError("unsupported benchmark corpus")
    tasks = [Task.from_dict(item) for item in payload["tasks"]]
    if len(tasks) != 20 or len({task.id for task in tasks}) != 20:
        raise ValueError("Phase 2 corpus must contain 20 unique tasks")
    counts = {kind: sum(task.kind == kind for task in tasks) for kind in ("classification", "filename", "code")}
    if counts != {"classification": 10, "filename": 5, "code": 5}:
        raise ValueError(f"unexpected corpus composition: {counts}")
    return tasks
