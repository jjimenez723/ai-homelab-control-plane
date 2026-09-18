"""Named campaign profiles: `--profile gate2` resolves to a manifest path.

A tiny table rather than a config-discovery mechanism, so `angel-eval
campaign --profile gate2` has exactly one place to look.
"""

from __future__ import annotations

from pathlib import Path

PROFILES_DIR = Path(__file__).resolve().parents[2] / "config" / "campaigns"

PROFILES: dict[str, str] = {
    "gate2": "gate2.yaml",
    "gate2-dev": "gate2-dev.yaml",
    "gate2-dev-naming": "gate2-dev-naming.yaml",
    "gate2-dev-reasoning": "gate2-dev-reasoning.yaml",
    "gate2-validation-002": "gate2-validation-002.yaml",
    "gate2-holdout": "gate2-holdout.yaml",
}


class UnknownProfile(KeyError):
    pass


def manifest_path_for_profile(profile: str) -> Path:
    try:
        filename = PROFILES[profile]
    except KeyError as exc:
        raise UnknownProfile(f"unknown profile {profile!r}; known: {sorted(PROFILES)}") from exc
    return PROFILES_DIR / filename
