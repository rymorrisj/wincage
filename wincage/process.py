"""
Suspended process launch and Job Object assignment for Peach 1UP.

``launch_suspended`` starts an emulator process, natively via CreateProcessW
or inside an AppContainer via the sandbox package, with its main thread
suspended. ``run_under_job`` then creates a Windows Job Object, optionally
applies CPU/memory limits to it, assigns the suspended process, retries once
with CREATE_BREAKAWAY_FROM_JOB if the first assignment hits
ERROR_ACCESS_DENIED (Windows 11's default job nesting refusing a second
assignment), and resumes the process's main thread once assignment has
succeeded and any limits are in force.

Both functions take already-resolved numbers rather than reading eras.yaml,
the emulator catalog, or settings themselves, launcher.py owns that
resolution (era/catalog lookups, job-name-prefix convention) and calls these
as its two delegation points.
"""

import asyncio
import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
from pathlib import Path

from .win32_types import (
    _CREATE_BREAKAWAY_FROM_JOB,
    _CREATE_SUSPENDED,
    _STARTF_USESHOWWINDOW,
    _SW_SHOWNORMAL,
    STARTUPINFOW,
    PROCESS_INFORMATION,
)
from . import sandbox as _sandbox
from .sandbox_config import SandboxConfig
from .sandbox_error import SandboxError
from .sandbox_process import SandboxProcess
from .job import WindowsJobObject

logger = logging.getLogger(__name__)


def _launch_native(
    executable_path: str,
    args: list[str],
    creation_flags: int,
    cwd: str | None = None,
) -> SandboxProcess:
    """Launch a suspended process under the current user account via CreateProcessW.

    The process is created with CREATE_SUSPENDED and the returned
    SandboxProcess retains the main-thread handle; the caller MUST call
    ``process.resume()`` exactly once after the process is assigned to its Job
    Object (or terminate it), otherwise the process is left permanently
    suspended and its thread handle leaks.
    """
    cmd_line = subprocess.list2cmdline([executable_path] + args)
    cmd_buf = ctypes.create_unicode_buffer(cmd_line)

    cwd = cwd if cwd is not None else str(Path(executable_path).parent)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = _STARTF_USESHOWWINDOW
    si.wShowWindow = _SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()

    # CREATE_SUSPENDED: the process must not run before the Job Object limits
    # are applied and it is assigned, so it can never execute uncapped.
    suspended_flags = creation_flags | _CREATE_SUSPENDED

    logger.debug(
        "launch_suspended (native): exe=%s cwd=%s flags=%#x args=%s cmd=%s",
        executable_path, cwd, suspended_flags, args, cmd_line,
    )

    result = ctypes.windll.kernel32.CreateProcessW(
        ctypes.c_wchar_p(executable_path),
        cmd_buf,
        None,
        None,
        False,
        ctypes.wintypes.DWORD(suspended_flags),
        None,
        ctypes.c_wchar_p(cwd) if cwd else None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )

    if not result:
        error_code = ctypes.windll.kernel32.GetLastError()
        raise RuntimeError(
            f"Failed to launch '{os.path.basename(executable_path)}'. "
            f"Error code: {error_code}."
        )

    # Retain hThread, it is needed for ResumeThread after Job Object
    # assignment. resume() (or _close_handles() on teardown) closes it.
    return SandboxProcess(
        pid=pi.dwProcessId,
        process_handle=pi.hProcess,
        thread_handle=pi.hThread,
        args=[executable_path] + args,
    )


def _launch_in_container(
    executable_path: str,
    args: list[str],
    creation_flags: int,
    sandbox_config: SandboxConfig,
    cwd: str | None = None,
) -> SandboxProcess:
    """Launch a process in a Windows AppContainer via the sandbox package."""
    logger.debug(
        "launch_suspended (container): exe=%s cwd=%s flags=%#x args=%s",
        executable_path, cwd, creation_flags, args,
    )

    if cwd is not None:
        sandbox_config.working_dir = cwd

    sandbox_config.args = list(args)

    if creation_flags & _CREATE_BREAKAWAY_FROM_JOB:
        sandbox_config.breakaway = True

    sandbox_handle = _sandbox.launch(sandbox_config)

    PROCESS_ALL_ACCESS = 0x001FFFFF
    win32_handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_ALL_ACCESS, False, sandbox_handle.pid
    )
    if not win32_handle:
        error_code = ctypes.windll.kernel32.GetLastError()
        try:
            asyncio.run(sandbox_handle.terminate())
        except Exception as te:
            logger.warning(
                "Failed to terminate container pid %d during OpenProcess cleanup: %s",
                sandbox_handle.pid, te,
            )
        raise RuntimeError(
            f"OpenProcess failed for container pid {sandbox_handle.pid} "
            f"(error {error_code}) after sandbox.launch() succeeded."
        )

    return SandboxProcess(
        pid=sandbox_handle.pid,
        process_handle=win32_handle,
        thread_handle=None,
        args=[executable_path] + args,
        sandbox_handle=sandbox_handle,
    )


def launch_suspended(
    exe: str,
    args: list[str],
    flags: int,
    cwd: str | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> SandboxProcess:
    """Launch exe suspended, natively or inside an AppContainer.

    Dispatches on whether sandbox_config is given: None launches natively via
    CreateProcessW (CREATE_SUSPENDED); a SandboxConfig launches inside an
    AppContainer via the sandbox package, which creates the process suspended
    and resumes it itself inside sandbox_host.exe after that process applies
    its own Job Object limits, see run_under_job's apply_limits parameter for
    why the caller-side Job Object does not re-apply them for this path.
    """
    if sandbox_config is not None:
        return _launch_in_container(exe, args, flags, sandbox_config, cwd=cwd)
    return _launch_native(exe, args, flags, cwd=cwd)


def run_under_job(
    executable_path: str,
    args: list[str],
    base_flags: int,
    cwd: str | None,
    process: SandboxProcess,
    job_name: str,
    memory_limit_mb: int,
    cpu_limit_percent: int,
    apply_limits: bool,
    cpu_min_rate_percent: int = 5,
    skip_cpu_limit: bool = False,
    skip_memory_limit: bool = False,
    sandbox_config: SandboxConfig | None = None,
) -> tuple[SandboxProcess, WindowsJobObject]:
    """Create a Job Object, assign *process* to it, and resume it.

    *process* must already be suspended (see launch_suspended) and not yet
    assigned to any job. memory_limit_mb, cpu_limit_percent,
    cpu_min_rate_percent, skip_cpu_limit, and skip_memory_limit are all
    pre-resolved by the caller (launcher.py) from eras.yaml/the emulator
    catalog; this function does not fetch them itself.

    apply_limits controls whether this Job Object numerically enforces
    memory_limit_mb/cpu_limit_percent (the native, non-containerized path) or
    only sets kill-on-close (container launches, where sandbox_host.exe's own
    Job Object already applied the limits before this process was ever
    resumed, re-applying them here would be redundant and could disagree
    with what was actually enforced if the two code paths ever drift). When
    apply_limits is True, skip_cpu_limit/skip_memory_limit independently gate
    each resource, matching a Job Object's own no-cap-if-skipped semantics.

    sandbox_config being non-None marks this as a container launch: the
    breakaway retry re-launches through the container path, and the final
    resume() is skipped (a container launch's process was resumed already,
    inside sandbox_host.exe, and carries no thread handle).

    If Job Object assignment fails, *process* is terminated and the launch is
    aborted, there is no unsandboxed fallback.
    """
    container_enabled = sandbox_config is not None

    job_object = WindowsJobObject(
        job_name, memory_limit_mb, cpu_limit_percent, cpu_min_rate_percent
    )
    try:
        job_object.create()

        if apply_limits:
            if not skip_cpu_limit:
                job_object.set_cpu_limit(job_object.cpu_limit_percent)

            if skip_memory_limit:
                job_object.set_kill_on_close()
            else:
                job_object.set_memory_limit(job_object.memory_limit_mb)
        else:
            # Limits already applied inside sandbox_host.exe's own Job
            # Object; only kill-on-close is set so tearing down this handle
            # still terminates the process.
            job_object.set_kill_on_close()
    except Exception as e:
        # Terminate directly while suspended, TerminateProcess works on a
        # suspended process, so there is no need to resume it first (which
        # would let this doomed process run uncapped, however briefly).
        cleanup_errors = []
        try:
            process.kill()
            process.wait()
        except Exception as exc:
            logger.error("kill failed for pid=%s during job setup cleanup: %s", process.pid, exc)
        try:
            job_object.teardown()
        except Exception as ce:
            cleanup_errors.append(str(ce))
        msg = f"Failed to set up job object for {executable_path}: {str(e)}"
        if cleanup_errors:
            msg += f" (Cleanup errors: {'; '.join(cleanup_errors)})"
        raise RuntimeError(msg)

    # SAFETY: handle is closed by wait(); do not call add_process after kill/wait
    _needs_breakaway_retry = False
    try:
        job_object.add_process(process)
    except RuntimeError as exc:
        if "retry_with_breakaway" not in str(exc):
            # Terminate the still-suspended process directly (see phase 1).
            try:
                process.kill()
                process.wait()
            except Exception as exc2:
                logger.error("kill failed for pid=%s during job assignment cleanup: %s", process.pid, exc2)
            try:
                job_object.teardown()
            except Exception:
                pass
            raise RuntimeError(f"Failed to assign process to job object: {exc}")
        _needs_breakaway_retry = True

    if _needs_breakaway_retry:
        # Terminate the still-suspended process directly (see phase 1).
        try:
            process.kill()
            process.wait()
        except Exception as exc:
            logger.error("kill failed for pid=%s during breakaway retry teardown: %s", process.pid, exc)
        try:
            process = launch_suspended(
                executable_path, args,
                base_flags | _CREATE_BREAKAWAY_FROM_JOB,
                cwd, sandbox_config,
            )
        except SandboxError:
            try:
                job_object.teardown()
            except Exception:
                pass
            raise
        except Exception as exc2:
            try:
                job_object.teardown()
            except Exception:
                pass
            raise RuntimeError(
                f"Cannot launch '{os.path.basename(executable_path)}': "
                f"CREATE_BREAKAWAY_FROM_JOB failed after assignment error 5 ({exc2})."
            )
        try:
            job_object.add_process(process)
        except Exception as exc3:
            # Terminate the still-suspended breakaway process directly (see phase 1).
            try:
                process.kill()
                process.wait()
            except Exception as exc4:
                logger.error("kill failed for pid=%s during post-breakaway assignment cleanup: %s", process.pid, exc4)
            try:
                job_object.teardown()
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to assign breakaway process to job object: {exc3}"
            )

    # Assignment succeeded and limits are in force, resume the suspended main
    # thread (native launches only; container launches were resumed inside
    # sandbox_host.exe and carry no thread handle). resume() closes hThread.
    # A resume failure here would leave the emulator hung, so it is fatal:
    # terminate and abort rather than return a permanently-suspended process.
    if not container_enabled:
        try:
            process.resume()
        except Exception as exc:
            try:
                process.kill()
                process.wait()
            except Exception as exc2:
                logger.error(
                    "kill failed for pid=%s after resume failure: %s", process.pid, exc2
                )
            try:
                job_object.teardown()
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to resume process {process.pid} after job assignment: {exc}"
            )

    # pi.hProcess is kept open so SandboxProcess.poll() can call
    # GetExitCodeProcess. _close_handles() (from poll() on exit) closes it once.
    return (process, job_object)
