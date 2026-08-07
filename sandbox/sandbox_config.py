from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class BrokerFile:
    path: str
    access: Literal["r", "rw", "x"]
    mode: Literal["secure", "inherit", "grant"]


@dataclass
class SandboxConfig:
    moniker: str
    exe_path: str
    args: list[str] = field(default_factory=list)
    working_dir: str | None = None
    broker_files: list[BrokerFile] = field(default_factory=list)
    cpu_max_rate: int = 50
    cpu_min_rate: int = 5
    skip_cpu_limit: bool = False
    memory_limit_mb: int | None = None
    breakaway: bool = False
