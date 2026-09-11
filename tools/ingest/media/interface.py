"""Clean interface for media derivative generators.

Phase 1A ships one implementation (`heic.HeicToJpegDerivative`), but every
future derivative type (e.g. proxy video, thumbnail) should implement this
same shape so the transaction layer and catalog wiring do not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class DerivativeResult:
    success: bool
    output_path: Path | None = None
    sha256: str | None = None
    size: int | None = None
    converter: str = ""
    converter_version: str = ""
    error_message: str | None = None


class MediaDerivative(Protocol):
    kind: str
    converter: str
    converter_version: str

    def applies_to(self, source_path: Path) -> bool:
        """Whether this derivative type should run for a given original file."""
        ...

    def generate(self, source_path: Path, output_path: Path) -> DerivativeResult:
        """Produce the derivative at `output_path`. Must never modify `source_path`."""
        ...
