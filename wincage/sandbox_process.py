"""
SandboxProcess, process handle returned by the launcher.

Wraps the Win32 handles from CreateProcessW (native path) or OpenProcess
(container path) and provides the interface expected by
``WindowsJobObject.add_process()`` and the teardown paths in
``process.run_under_job``.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import logging
from typing import TYPE_CHECKING

from .win32_types import _RESUME_THREAD_FAILED, _STILL_ACTIVE

if TYPE_CHECKING:
    from .sandbox import SandboxHandle

_log = logging.getLogger(__name__)


class SandboxProcess:
    """Process handle returned by ``process.launch_suspended``.

    Attributes:
        pid: Process ID.
        args: Command-line as a list; ``args[0]`` is the executable path.
        returncode: Exit code once the process has exited, ``None`` while running.
    """

    def __init__(
        self,
        pid: int,
        process_handle,
        thread_handle,
        args: list,
        sandbox_handle: SandboxHandle | None = None,
    ):
        self.pid = pid
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self.args = args
        self.returncode = None
        self.handle: int | None = process_handle
        self.sandbox_handle: SandboxHandle | None = sandbox_handle

    def poll(self):
        """Return exit code if the process has exited, ``None`` if still running.

        Closes OS handles when the process exits to release resources.
        Safe to call multiple times after exit.
        """
        if self._process_handle is None:
            return self.returncode
        exit_code = ctypes.wintypes.DWORD(_STILL_ACTIVE)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(
            self._process_handle, ctypes.byref(exit_code)
        )
        if not ok:
            # The BOOL return says whether the DWORD out-param is meaningful
            # at all. On failure exit_code still holds the pre-set _STILL_ACTIVE.
            #
            # Report unknown (None) rather than implicitly alive. Leave
            # returncode and the handles untouched as the process may or
            # may not have exited.
            _log.error(
                "GetExitCodeProcess failed for pid=%s (GetLastError=%s); "
                "process exit state unknown.",
                self.pid, ctypes.windll.kernel32.GetLastError(),
            )
            return None
        if exit_code.value == _STILL_ACTIVE:
            # 259 is ambiguous. GetExitCodeProcess reports it both while the
            # process is still running and when it legitimately exited with
            # real exit code 259. Disambiguate with a non-blocking wait on
            # the handle itself.
            _WAIT_TIMEOUT = 0x00000102
            wait_result = ctypes.windll.kernel32.WaitForSingleObject(
                self._process_handle, ctypes.wintypes.DWORD(0)
            )
            if wait_result != 0:
                # Not WAIT_OBJECT_0 (still running, or the wait itself
                # failed): don't risk a false exit report.
                return None
            # WAIT_OBJECT_0: handle is signaled, so 259 is the real exit code.
        self.returncode = exit_code.value
        self._close_handles()
        return self.returncode

    def terminate(self) -> None:
        """Send a termination signal to the process."""
        if self._process_handle:
            ctypes.windll.kernel32.TerminateProcess(self._process_handle, 1)
        if self.sandbox_handle is not None:
            asyncio.run(self.sandbox_handle.terminate())

    def kill(self) -> None:
        """Terminate the process immediately (same as terminate on Windows)."""
        self.terminate()

    def wait(self, timeout_ms: int = 10_000) -> int:
        """Wait up to *timeout_ms* milliseconds for the process to exit.

        Returns the exit code, or -1 if the process did not exit within the
        timeout.  Closes OS handles on return regardless of outcome.
        Callers that need to guarantee termination should call kill() first.
        """
        _WAIT_TIMEOUT = 0x00000102
        if self._process_handle:
            result = ctypes.windll.kernel32.WaitForSingleObject(
                self._process_handle,
                ctypes.wintypes.DWORD(timeout_ms),
            )
            if result == _WAIT_TIMEOUT:
                self._close_handles()
                self.returncode = -1
                return -1
            exit_code = ctypes.wintypes.DWORD(0)
            ctypes.windll.kernel32.GetExitCodeProcess(
                self._process_handle, ctypes.byref(exit_code)
            )
            self.returncode = exit_code.value
        self._close_handles()
        # Reaching here with returncode still None means no handle was ever
        # open; fall back to the same -1 sentinel the timeout branch uses so
        # the -> int annotation stays honest.
        return self.returncode if self.returncode is not None else -1

    def resume(self) -> None:
        """Resume the suspended main thread using the stored thread handle.

        Uses the thread handle from ``PROCESS_INFORMATION`` returned by
        ``CreateProcessW``, no thread snapshot required.  The thread
        handle is closed immediately after the resume call.

        Raises:
            RuntimeError: If the thread handle is already closed or
                ``ResumeThread`` reports failure.
        """
        if not self._thread_handle:
            raise RuntimeError(
                f"Thread handle is not open for process {self.pid}. "
                "resume() must be called exactly once after process creation."
            )
        result = ctypes.windll.kernel32.ResumeThread(self._thread_handle)
        ctypes.windll.kernel32.CloseHandle(self._thread_handle)
        self._thread_handle = None
        if result == _RESUME_THREAD_FAILED:
            error_code = ctypes.windll.kernel32.GetLastError()
            raise RuntimeError(
                f"ResumeThread failed for process {self.pid}. Error code: {error_code}"
            )

    def _close_handles(self) -> None:
        """Close process and thread handles to release OS resources."""
        if self._thread_handle:
            ctypes.windll.kernel32.CloseHandle(self._thread_handle)
            self._thread_handle = None
        if self._process_handle:
            ctypes.windll.kernel32.CloseHandle(self._process_handle)
            self._process_handle = None
            self.handle = None  # same OS handle value, prevent use-after-close
        self.sandbox_handle = None
