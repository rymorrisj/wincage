"""
Suspended process launch and Job Object assignment. wincage's two public
entry points for the native, non-AppContainer launch path.

- ``launch_suspended`` starts a target process, natively via
  CreateProcessW or inside an AppContainer via the sandbox package, with
  its main thread suspended.
- ``run_under_job`` creates a Windows Job Object, optionally applies
  CPU/memory limits to it, and assigns the suspended process. Retries
  once with CREATE_BREAKAWAY_FROM_JOB if the first assignment hits
  ERROR_ACCESS_DENIED (Windows 11's default job nesting refusing a
  second assignment). Resumes the process's main thread once assignment
  has succeeded.

Both functions take already-resolved values rather than reading any
configuration themselves.
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

    Caller MUST call ``process.resume()`` exactly once after Job Object
    assignment, or terminate it instead; otherwise it stays suspended
    forever and its thread handle leaks.
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

    # The host hands over its own duplicated handle instead of Python reopening
    # by pid, closing a pid-reuse race window. The claim (grab + null) happens
    # under the same lock CLEANED_UP's close uses, and must not wrap any wait
    # for CLEANED_UP or the two would deadlock each other.
    with sandbox_handle._process_handle_lock:
        win32_handle = sandbox_handle.process_handle
        sandbox_handle.process_handle = None

    if win32_handle is None:
        try:
            asyncio.run(sandbox_handle.terminate())
        except Exception as te:
            logger.warning(
                "Failed to terminate container pid %d after missing "
                "process_handle: %s",
                sandbox_handle.pid, te,
            )
        raise RuntimeError(
            f"sandbox_host.exe did not report a process_handle for "
            f"container pid {sandbox_handle.pid}. It may be an older build."
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

    With sandbox_config=None, launches natively via CreateProcessW
    (CREATE_SUSPENDED). With a SandboxConfig, launches inside an
    AppContainer, which resumes the process itself inside sandbox_host.exe
    after applying its own Job Object limits.
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

    *process* must already be suspended and not yet assigned to any job.
    memory_limit_mb, cpu_limit_percent, and the cpu/skip flags are all
    pre-resolved by the caller; this function does not fetch them itself.

    apply_limits=True numerically enforces the limits (native path).
    apply_limits=False only sets kill-on-close, since sandbox_host.exe's own
    Job Object already enforced the limits before this process was resumed.

    sandbox_config not None marks a container launch: the breakaway retry
    re-launches through the container path, and the final resume() is
    skipped since the process was already resumed inside sandbox_host.exe.

    On Job Object assignment failure, *process* is terminated and the
    launch is aborted; there is no unsandboxed fallback.
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
            # Limits were already enforced inside sandbox_host.exe's own Job
            # Object; only kill-on-close is set so closing this handle still kills it.
            job_object.set_kill_on_close()
    except Exception as e:
        # TerminateProcess works on a suspended process, so there is no need
        # to resume it first, which would let this doomed process run uncapped.
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

    _needs_breakaway_retry = False
    try:
        job_object.add_process(process)
    except RuntimeError as exc:
        if "retry_with_breakaway" not in str(exc):
            # Kill while still suspended; resuming first would let a doomed
            # process run uncapped.
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
        # Bounds the wait on the first attempt's stub so a stalled process can't
        # leak a handle into the retry; force-kills it if it hasn't exited in time.
        if process.sandbox_handle is not None:
            stub = process.sandbox_handle
            try:
                asyncio.run(asyncio.wait_for(stub.terminate(), timeout=5.0))
            except asyncio.TimeoutError:
                logger.error(
                    "sandbox_host stub did not exit within timeout for pid=%s "
                    "during breakaway retry teardown; force-killing", process.pid,
                )
                stub_proc = getattr(stub, "_proc", None)
                if stub_proc is not None:
                    try:
                        stub_proc.kill()
                    except Exception as exc_kill:
                        logger.error(
                            "force-kill of sandbox_host stub failed for pid=%s: %s",
                            process.pid, exc_kill,
                        )
            except Exception as exc:
                logger.error(
                    "stub termination failed for pid=%s during breakaway retry teardown: %s",
                    process.pid, exc,
                )
            finally:
                process.sandbox_handle = None
        # Kill while still suspended; resuming first would let a doomed
        # process run uncapped.
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
            # Kill while still suspended; resuming first would let a doomed
            # process run uncapped.
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

    # Only the native path resumes here; container launches were already resumed
    # inside sandbox_host.exe. Abort on failure rather than return a hung process.
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

    # The process handle stays open here so SandboxProcess.poll() can call
    # GetExitCodeProcess; _close_handles() closes it once the exit is observed.

    # The caller must also retain job_object: it holds the only handle to a
    # KILL_ON_JOB_CLOSE job, so dropping it terminates the target.
    return (process, job_object)
