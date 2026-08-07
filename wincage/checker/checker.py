from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from ..sandbox import SandboxConfig, SandboxError, launch, reset_container
from .results import CheckResult, CheckStatus

_SRC = Path(__file__).parent / "src"

# Default AppContainer moniker prefix. Each probe provisions a real, persistent
# AppContainer profile named f"{moniker_prefix}.{check_name}", so an embedding
# application that wants its probe profiles namespaced under its own identity
# should pass moniker_prefix to run_checks() rather than accept this default.
DEFAULT_MONIKER_PREFIX: str = "SandboxChecker"

# (name, exe_name, pass_message)
#
# What each probe verifies is a property of the capability, not of any
# particular application. Which of the caller's programs a failure impacts is
# the caller's knowledge, so it is supplied via run_checks(affects=...) and
# deliberately not encoded here.
_CHECKS: list[tuple[str, str, str]] = [
    (
        "sdl2_d3d11",
        "test_sdl2_d3d11.exe",
        "SDL2 init, WASAPI audio, and D3D11 hardware device all accessible in AppContainer",
    ),
    (
        "sdl2_opengl",
        "test_sdl2_opengl.exe",
        "OpenGL 4.5 core context created via WGL inside AppContainer",
    ),
    (
        "qt_qpa",
        "test_qt_qpa.exe",
        "Qt 5.15 QPA platform plugin loaded and window displayed inside AppContainer",
    ),
]


async def _async_run_one(
    name: str,
    config: SandboxConfig,
    pass_message: str,
    affects: list[str],
) -> CheckResult:
    try:
        handle = launch(config)
    except SandboxError as exc:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            message=str(exc),
            affects=affects,
        )
    except Exception as exc:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            message=f"unexpected error launching check: {exc}",
            affects=affects,
        )

    try:
        exit_code = await asyncio.to_thread(handle._proc.wait)
        # sandbox_host.exe writes a final "exited" JSON line to stdout after
        # the started-line sandbox.launch() already consumed; nothing else
        # reads it, so drain it here rather than leaving it unread on the pipe.
        if handle._proc.stdout is not None:
            await asyncio.to_thread(handle._proc.stdout.read)
    finally:
        # Each probe provisions a real, persistent AppContainer profile
        # (moniker_prefix.name). Without this, repeated check runs leave
        # every prior run's profile behind with nothing to clean it up.
        try:
            reset_container(config.moniker)
        except SandboxError:
            pass

    if exit_code == 0:
        return CheckResult(
            name=name,
            status=CheckStatus.PASS,
            message=pass_message,
            affects=affects,
        )
    return CheckResult(
        name=name,
        status=CheckStatus.FAIL,
        message=(
            f"test exited with code {exit_code}, "
            "AppContainer may be blocking a required API; "
            "disable sandbox for affected emulators"
        ),
        affects=affects,
    )


def _run_one(
    name: str,
    exe_name: str,
    pass_message: str,
    affects: list[str],
    moniker_prefix: str,
) -> CheckResult:
    exe = _SRC / exe_name
    if not exe.exists():
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            message="not built, run build_tests.sh",
            affects=affects,
        )

    config = SandboxConfig(
        moniker=f"{moniker_prefix}.{name}",
        exe_path=str(exe),
        cpu_max_rate=50,
        cpu_min_rate=5,
    )

    return asyncio.run(_async_run_one(name, config, pass_message, affects))


def run_checks(
    moniker_prefix: str = DEFAULT_MONIKER_PREFIX,
    affects: Mapping[str, list[str]] | None = None,
) -> list[CheckResult]:
    """Run every capability probe and return one CheckResult per probe.

    Never raises: a probe that cannot launch, or that exits non-zero, comes back
    as CheckStatus.FAIL, and a probe whose binary was never built comes back as
    CheckStatus.SKIP.

    Args:
        moniker_prefix: AppContainer moniker prefix for the probe profiles, which
            are provisioned as f"{moniker_prefix}.{check_name}". These are real,
            persistent per-user profiles, so pass a prefix that namespaces them
            under the calling application.
        affects: Optional mapping of check name to the caller's own list of
            impacted components, copied verbatim onto the matching CheckResult.
            Names absent from the mapping get an empty list. The checker reports
            which capability failed; deciding what that impacts is the caller's.

    Returns:
        A list of CheckResult, one per entry in _CHECKS, in declaration order.
    """
    # sandbox_host.exe must be built alongside wincage/ before calling
    # run_checks(), see the repo root README.md.
    affects_map: Mapping[str, list[str]] = affects or {}

    results: list[CheckResult] = []
    for name, exe_name, pass_message in _CHECKS:
        results.append(
            _run_one(
                name,
                exe_name,
                pass_message,
                list(affects_map.get(name, [])),
                moniker_prefix,
            )
        )
    return results
