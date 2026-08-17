from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as _wt
from collections.abc import Mapping
from pathlib import Path

from ..sandbox import (
    SandboxConfig,
    SandboxError,
    SandboxHandle,
    SandboxStage,
    launch,
    reset_container,
)
from ..sandbox_config import BrokerFile
from .results import CheckResult, CheckStatus

_SRC = Path(__file__).parent / "src"

# Mirrors ../scripts/Test-AppContainerStatus.ps1's Get-ProcessAppContainerSidString,
# but skips SID derivation and its own OpenProcess call since handle.container_sid
# and handle.process_handle already provide both.
_TOKEN_QUERY = 0x0008
_TOKEN_APP_CONTAINER_SID = 31

_advapi32 = ctypes.windll.advapi32
_advapi32.OpenProcessToken.argtypes = [_wt.HANDLE, _wt.DWORD, ctypes.POINTER(_wt.HANDLE)]
_advapi32.OpenProcessToken.restype = _wt.BOOL
_advapi32.GetTokenInformation.argtypes = [
    _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_wt.DWORD),
]
_advapi32.GetTokenInformation.restype = _wt.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
_advapi32.ConvertSidToStringSidW.restype = _wt.BOOL

_kernel32 = ctypes.windll.kernel32


def _verify_confinement(handle: SandboxHandle) -> str | None:
    """Confirm the probe process's token AppContainer SID matches handle.container_sid.

    Returns None once confirmed, otherwise a short description of what
    could not be confirmed and why.
    """
    token = _wt.HANDLE()
    with handle._process_handle_lock:
        if handle.process_handle is None:
            return "no process handle reported by the host"
        if not _advapi32.OpenProcessToken(handle.process_handle, _TOKEN_QUERY, ctypes.byref(token)):
            return f"OpenProcessToken failed (error {_kernel32.GetLastError()})"

    try:
        required = _wt.DWORD(0)
        _advapi32.GetTokenInformation(
            token, _TOKEN_APP_CONTAINER_SID, None, 0, ctypes.byref(required)
        )
        if required.value == 0:
            return f"GetTokenInformation size probe failed (error {_kernel32.GetLastError()})"

        buf = ctypes.create_string_buffer(required.value)
        actual = _wt.DWORD(0)
        if not _advapi32.GetTokenInformation(
            token, _TOKEN_APP_CONTAINER_SID, buf, required.value, ctypes.byref(actual)
        ):
            return f"GetTokenInformation failed (error {_kernel32.GetLastError()})"

        # TOKEN_APPCONTAINER_INFORMATION is one PSID field. Null means the
        # process has no AppContainer SID at all, so it isn't confined.
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid_ptr:
            return "process token has no AppContainer SID, not confined"

        str_ptr = ctypes.c_wchar_p()
        if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
            return f"ConvertSidToStringSidW failed (error {_kernel32.GetLastError()})"
        try:
            actual_sid = str_ptr.value
        finally:
            _kernel32.LocalFree(str_ptr)

        if actual_sid != handle.container_sid:
            return f"confined under a different SID ({actual_sid}, expected {handle.container_sid})"
        return None
    finally:
        _kernel32.CloseHandle(token)


# Default AppContainer moniker prefix for probe profiles; override via
# run_checks(moniker_prefix=...) to namespace them under your own app.
DEFAULT_MONIKER_PREFIX: str = "SandboxChecker"

# Seconds _async_run_one waits for a launched probe to exit before
# terminating it and reporting a timeout failure.
_PROBE_WAIT_TIMEOUT_SECONDS = 30.0

# (name, exe_name, pass_message); which programs a failure affects is
# supplied by the caller via run_checks(affects=...), not stored here.
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
        # Works even if the process has already exited: the AppContainer token is
        # assigned at process creation, before the target runs any of its own code.
        confinement_error = _verify_confinement(handle)

        try:
            exit_code = await asyncio.wait_for(
                asyncio.to_thread(handle._proc.wait),
                timeout=_PROBE_WAIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            handle._proc.terminate()
            await asyncio.to_thread(handle._proc.wait)
            return CheckResult(
                name=name,
                status=CheckStatus.FAIL,
                message=(
                    f"probe did not exit within "
                    f"{_PROBE_WAIT_TIMEOUT_SECONDS:.0f}s, terminated"
                ),
                affects=affects,
            )

        # sandbox_host.exe writes a final "exited" JSON line after launch()
        # already consumed the started-line; drain it so it's not left unread.
        stdout_text = b""
        if handle._proc.stdout is not None:
            stdout_text = await asyncio.to_thread(handle._proc.stdout.read)
    finally:
        # Each probe provisions a real, persistent AppContainer profile;
        # without this, repeated check runs leave every prior profile behind.
        try:
            reset_container(config.moniker)
        except SandboxError:
            pass

    if confinement_error is not None:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            message=(
                f"could not confirm AppContainer confinement: {confinement_error} "
                f"(test exited with code {exit_code})"
            ),
            affects=affects,
        )

    if exit_code == 0:
        return CheckResult(
            name=name,
            status=CheckStatus.PASS,
            message=pass_message,
            affects=affects,
        )

    detail = stdout_text.decode(errors="replace").strip() or "no output"
    return CheckResult(
        name=name,
        status=CheckStatus.FAIL,
        message=(
            f"confinement confirmed, but the probe itself failed "
            f"(exit code {exit_code}): {detail}"
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

    # 1:1 naming per build_tests.sh: test_foo.cpp builds test_foo.exe.
    src = exe.with_suffix(".cpp")
    if src.exists() and src.stat().st_mtime > exe.stat().st_mtime:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            message="stale binary, rebuild: source is newer than the compiled exe, run build_tests.sh",
            affects=affects,
        )

    config = SandboxConfig(
        moniker=f"{moniker_prefix}.{name}",
        exe_path=str(exe),
        # Probes load DLLs (SDL2, Qt) from their own directory, and some
        # also enumerate it during init. "rx": "r" alone can't list a directory.
        broker_files=[
            BrokerFile(path=str(_SRC), access="rx", mode="grant"),
        ],
        cpu_max_rate=50,
        cpu_min_rate=5,
        # Each run provisions a real AppContainer profile and grants it a DACL
        # ACE on _SRC; without this the ACE is left behind on every run.
        should_revert_grants=True,
    )

    return asyncio.run(_async_run_one(name, config, pass_message, affects))


def run_checks(
    moniker_prefix: str = DEFAULT_MONIKER_PREFIX,
    affects: Mapping[str, list[str]] | None = None,
) -> list[CheckResult]:
    """Run every capability probe and return one CheckResult per probe, never raising for a per-probe failure.

    Raises SandboxError if called from a running event loop, since each probe launch needs its own asyncio.run().

    Args:
        moniker_prefix: namespaces the persistent per-user AppContainer
            profiles this provisions, as f"{moniker_prefix}.{check_name}".
        affects: caller's impacted-components list per check name, copied
            onto the matching CheckResult; names absent get an empty list.
    """
    # asyncio.run() raises a raw RuntimeError if a loop is already running on
    # this thread; detect that up front and fail with a clear SandboxError instead.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise SandboxError(
            message="run_checks() cannot be called from within a running event loop",
            stage=SandboxStage.CONFIG_VALIDATION,
            suggestions=[
                "Call run_checks() from synchronous code with no event loop "
                "running, or run it in a separate thread",
            ],
        )

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
