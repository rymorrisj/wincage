from . import sandbox as _sandbox_module
from .sandbox import launch, reset_container, revoke_grants, SandboxHandle
from .sandbox_config import BrokerFile, SandboxConfig
from .sandbox_error import SandboxError
from .sandbox_event import (
    SandboxEvent,
    SandboxPayload,
    SandboxStage,
)
from .process import launch_suspended, run_under_job
from .sandbox_process import SandboxProcess
from .job import WindowsJobObject

EXE_NAME: str = _sandbox_module.EXE_NAME


def __setattr__(name: str, value: object) -> None:
    # Write-through so `import wincage; wincage.EXE_NAME = "x"` takes effect
    # on the submodule that _exe() reads from.
    globals()[name] = value
    if name == "EXE_NAME":
        _sandbox_module.EXE_NAME = value  # type: ignore[assignment]


__all__ = [
    "launch",
    "reset_container",
    "revoke_grants",
    "EXE_NAME",
    "SandboxConfig",
    "SandboxHandle",
    "SandboxEvent",
    "SandboxPayload",
    "SandboxError",
    "SandboxStage",
    "BrokerFile",
    "launch_suspended",
    "run_under_job",
    "SandboxProcess",
    "WindowsJobObject",
]
