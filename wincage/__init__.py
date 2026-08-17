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


def set_exe_name(value: str) -> None:
    """Set the host executable name launch() spawns, overriding the sandbox_host.exe default."""
    globals()["EXE_NAME"] = value
    _sandbox_module.EXE_NAME = value


__all__ = [
    "launch",
    "reset_container",
    "revoke_grants",
    "EXE_NAME",
    "set_exe_name",
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
