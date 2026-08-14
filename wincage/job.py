"""
Windows Job Object wrapper for resource limits on the native launch path.

Each launch gets its own named Job Object so unrelated processes cannot
interfere with each other's limits. Limits (memory cap, CPU cap,
kill-on-close) are supplied by the caller as already-resolved values
"""

import ctypes
import ctypes.wintypes
import logging
import sys

from .win32_types import (
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE,
    _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
    _JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOBOBJECT_CPU_RATE_CONTROL_INFORMATION,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
)
from .sandbox_process import SandboxProcess

logger = logging.getLogger(__name__)


class WindowsJobObject:
    """Windows Job Object wrapper for process resource limits.

    Wraps a named Win32 Job Object with memory cap, CPU cap, and
    kill-on-close semantics. The expected call sequence is:

        job = WindowsJobObject(name, memory_limit_mb, cpu_limit_percent)
        job.create()
        job.set_cpu_limit(...) / job.set_memory_limit(...)
        job.add_process(sandbox_process)
        job.teardown()

    Limits must be set before add_process so the target can never run
    uncapped. ``run_under_job`` in process.py handles this sequence and is
    the preferred entry point for callers outside this file.

    Attributes:
        name: Unique name for the Win32 Job Object.
        memory_limit_mb: Per-process memory cap in MB, applied by
            ``set_memory_limit``, not by ``create``.
        cpu_limit_percent: CPU cap as a percentage of all logical
            processors (1 to 100), applied by ``set_cpu_limit``.
        cpu_min_rate_percent: CPU scheduling floor used as MinRate by
            set_cpu_limit's MIN_MAX_RATE path; supplied by the caller as an
            already-resolved value, not read internally.
        job_handle: Raw Win32 handle; ``None`` until ``create()`` is called.
        pid: PID of the process added via ``add_process``.
    """

    def __init__(
        self,
        name: str,
        memory_limit_mb: int,
        cpu_limit_percent: int,
        cpu_min_rate_percent: int = 1,
    ):
        self.name = name
        self.memory_limit_mb = memory_limit_mb
        self.cpu_limit_percent = cpu_limit_percent
        self.cpu_min_rate_percent = cpu_min_rate_percent
        self.job_handle = None
        self.pid = None

    def create(self) -> None:
        """Create the Win32 Job Object."""
        ctypes.windll.kernel32.SetLastError(0)
        self.job_handle = ctypes.windll.kernel32.CreateJobObjectW(
            None,
            ctypes.c_wchar_p(self.name)
        )

        if not self.job_handle:
            error_code = ctypes.windll.kernel32.GetLastError()
            raise RuntimeError(
                f"Failed to create Job Object '{self.name}'. Error code: {error_code}"
            )

        # CreateJobObjectW returns a handle to the EXISTING job object on a
        # name collision (ERROR_ALREADY_EXISTS) instead of failing. Two
        # unrelated launches would then silently share one kernel object,
        # and tearing down either would kill the other.
        _ERROR_ALREADY_EXISTS = 183
        error_code = ctypes.windll.kernel32.GetLastError()
        if error_code == _ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(self.job_handle)
            self.job_handle = None
            raise RuntimeError(
                f"Job Object '{self.name}' already exists (ERROR_ALREADY_EXISTS). "
                "Refusing to share a Job Object handle between launches, aborting."
            )

    def set_memory_limit(self, limit_mb: int) -> None:
        """Set the per-process memory cap and enable kill-on-close."""
        if not self.job_handle:
            raise RuntimeError("Job object not created. Call create() first.")

        limit_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limit_info.ProcessMemoryLimit = limit_mb * 1024 * 1024

        result = ctypes.windll.kernel32.SetInformationJobObject(
            self.job_handle,
            ctypes.wintypes.DWORD(9),
            ctypes.byref(limit_info),
            ctypes.sizeof(limit_info)
        )

        if not result:
            error_code = ctypes.windll.kernel32.GetLastError()
            raise RuntimeError(
                f"Failed to set memory limit to {limit_mb}MB. Error code: {error_code}"
            )

    def set_kill_on_close(self) -> None:
        """Set kill-on-close without a process memory cap."""
        if not self.job_handle:
            raise RuntimeError("Job object not created. Call create() first.")

        limit_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        result = ctypes.windll.kernel32.SetInformationJobObject(
            self.job_handle,
            ctypes.wintypes.DWORD(9),
            ctypes.byref(limit_info),
            ctypes.sizeof(limit_info)
        )

        if not result:
            error_code = ctypes.windll.kernel32.GetLastError()
            raise RuntimeError(
                f"Failed to set kill-on-close for '{self.name}'. Error code: {error_code}"
            )

    def set_cpu_limit(self, cpu_limit_percent: int) -> None:
        """Apply CPU rate control using MIN_MAX_RATE on Windows 10 1607+ or HARD_CAP as fallback."""
        if not self.job_handle:
            raise RuntimeError("Job object not created. Call create() first.")

        _MIN_RATE = self.cpu_min_rate_percent * 100

        win_build = sys.getwindowsversion().build
        if win_build >= 14393:
            configured_rate = cpu_limit_percent * 100
            if configured_rate <= _MIN_RATE:
                logger.warning(
                    "CPU cap for '%s' (%d%%, %d/10000) is at or below the MinRate floor "
                    "(%d/10000); MinRate floor will be used as MaxRate.",
                    self.name,
                    cpu_limit_percent,
                    configured_rate,
                    _MIN_RATE,
                )
            max_rate = max(_MIN_RATE, min(10000, configured_rate))
            assert 0 <= _MIN_RATE <= 0xFFFF
            assert 0 <= max_rate <= 0xFFFF
            cpu_rate_info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            cpu_rate_info.ControlFlags = (
                _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE
            )
            cpu_rate_info.CpuRate = (_MIN_RATE & 0xFFFF) | ((max_rate & 0xFFFF) << 16)

            result = ctypes.windll.kernel32.SetInformationJobObject(
                self.job_handle,
                ctypes.wintypes.DWORD(15),
                ctypes.byref(cpu_rate_info),
                ctypes.sizeof(cpu_rate_info)
            )

            if result:
                return

            error_code = ctypes.windll.kernel32.GetLastError()
            logger.warning(
                "MIN_MAX_RATE SetInformationJobObject failed for '%s' (error %d); "
                "retrying with HARD_CAP.",
                self.name,
                error_code,
            )
            cpu_rate_info.ControlFlags = (
                _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            )
            cpu_rate_info.CpuRate = max(1, min(10000, cpu_limit_percent * 100))

            result = ctypes.windll.kernel32.SetInformationJobObject(
                self.job_handle,
                ctypes.wintypes.DWORD(15),
                ctypes.byref(cpu_rate_info),
                ctypes.sizeof(cpu_rate_info)
            )
            if not result:
                error_code = ctypes.windll.kernel32.GetLastError()
                raise RuntimeError(
                    f"Failed to set CPU limit to {cpu_limit_percent}%. Error code: {error_code}"
                )
        else:
            logger.warning(
                "JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE unavailable (Windows build %d < 14393); "
                "falling back to HARD_CAP for '%s'.",
                win_build,
                self.name,
            )
            cpu_rate_info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            cpu_rate_info.ControlFlags = (
                _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            )
            cpu_rate_info.CpuRate = max(1, min(10000, cpu_limit_percent * 100))

            result = ctypes.windll.kernel32.SetInformationJobObject(
                self.job_handle,
                ctypes.wintypes.DWORD(15),
                ctypes.byref(cpu_rate_info),
                ctypes.sizeof(cpu_rate_info)
            )
            if not result:
                error_code = ctypes.windll.kernel32.GetLastError()
                raise RuntimeError(
                    f"Failed to set CPU limit to {cpu_limit_percent}%. Error code: {error_code}"
                )

    def add_process(self, process: "SandboxProcess") -> None:
        """Assign a process to the job object."""
        if not self.job_handle:
            raise RuntimeError("Job object not created. Call create() first.")

        if not process or not process.pid:
            raise RuntimeError("Invalid process or process not started.")

        self.pid = process.pid

        using_stored_handle = process.handle is not None

        if using_stored_handle:
            proc_handle = process.handle
        else:
            proc_handle = ctypes.windll.kernel32.OpenProcess(
                0x0201,
                False,
                process.pid
            )
            if not proc_handle:
                error_code = ctypes.windll.kernel32.GetLastError()
                raise RuntimeError(
                    f"Failed to open process {self.pid}. Error code: {error_code}"
                )

        try:
            _in_job = ctypes.wintypes.BOOL(False)
            ctypes.windll.kernel32.IsProcessInJob(
                proc_handle, None, ctypes.byref(_in_job)
            )
            already_in_job = bool(_in_job)

            result = ctypes.windll.kernel32.AssignProcessToJobObject(
                self.job_handle,
                proc_handle
            )

            if not result:
                error_code = ctypes.windll.kernel32.GetLastError()
                if error_code == 5:
                    raise RuntimeError(
                        f"Failed to add process {self.pid} to job object."
                        f" Error code: 5. retry_with_breakaway"
                    )
                extra = (
                    " The process is still inside an OS managed job object, "
                    "nested assignment failed. This should not occur on Windows 8+; "
                    "check for third party job managers or restricted environments."
                    if already_in_job else ""
                )
                raise RuntimeError(
                    f"Failed to add process {self.pid} to job object."
                    f" Error code: {error_code}.{extra}"
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Unexpected error assigning process {self.pid} to job object: {exc}"
            ) from exc
        finally:
            if not using_stored_handle:
                ctypes.windll.kernel32.CloseHandle(proc_handle)

    def teardown(self) -> None:
        """Terminate all processes in the job object and release all associated resources."""
        termination_errors = []

        if self.job_handle:
            try:
                result = ctypes.windll.kernel32.TerminateJobObject(
                    self.job_handle,
                    1
                )
                if not result:
                    error_code = ctypes.windll.kernel32.GetLastError()
                    termination_errors.append(
                        f"TerminateJobObject failed with error code: {error_code}"
                    )
            except Exception as e:
                termination_errors.append(f"Exception during job termination: {str(e)}")

            try:
                ctypes.windll.kernel32.CloseHandle(self.job_handle)
            except Exception as e:
                termination_errors.append(f"Failed to close job handle: {str(e)}")

        self.job_handle = None

        if termination_errors:
            raise RuntimeError(
                f"Job object termination encountered errors for {self.name}. "
                f"Some resources may require manual cleanup. "
                f"Errors: {'; '.join(termination_errors)}"
            )

    def close(self) -> None:
        """Close the job object handle.

        Once set_memory_limit or set_kill_on_close has run, KILL_ON_JOB_CLOSE
        is in force, so closing the last handle to this job terminates every
        process assigned to it. Call teardown() instead when an explicit
        TerminateJobObject call is required before closing.
        """
        if self.job_handle:
            ctypes.windll.kernel32.CloseHandle(self.job_handle)
            self.job_handle = None

    def __del__(self) -> None:
        if self.job_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.job_handle)
            except Exception:
                pass
            self.job_handle = None

    def handle_is_open(self) -> bool:
        """Check whether the job object handle is open and queryable.

        A handle can be open and queryable with
        zero processes running in the job.
        """
        if not self.job_handle:
            return False

        try:
            accounting_info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            result = ctypes.windll.kernel32.QueryInformationJobObject(
                self.job_handle,
                ctypes.wintypes.DWORD(1),
                ctypes.byref(accounting_info),
                ctypes.sizeof(accounting_info),
                None
            )
            return bool(result)
        except Exception:
            return False
