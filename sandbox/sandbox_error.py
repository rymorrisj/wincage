from __future__ import annotations

from dataclasses import dataclass, field

from .sandbox_event import SandboxStage

@dataclass
class SandboxError(Exception):
    message: str
    stage: SandboxStage
    suggestions: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message
