from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

class SandboxEvent(Enum):
    STARTED = "started"
    EXITED = "exited"
    ERROR = "error"
    CLEANED_UP = "cleaned_up"

class SandboxStage(Enum):
    CONFIG_VALIDATION = "config_validation"
    CONTAINER_PROVISION = "container_provision"
    DACL_GRANT = "dacl_grant"
    DACL_REVOKE = "dacl_revoke"
    PROCESS_CREATE = "process_create"
    JOB_ASSIGN = "job_assign"
    WATCHDOG = "watchdog"
    CLEANUP = "cleanup"

@dataclass(frozen=True)
class SandboxPayload:
    event: SandboxEvent
    moniker: str
    pid: int
    exit_code: int | None
    error: str | None
    stage: SandboxStage | None
