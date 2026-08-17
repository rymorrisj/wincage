from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Side-effect import: registers argtypes/restype for the kernel32 functions
# used below (OpenEventW, WaitForSingleObject, CloseHandle) before they're called.
from . import win32_types as _win32_types  # noqa: F401
from .sandbox_config import BrokerFile, SandboxConfig
from .sandbox_error import SandboxError
from .sandbox_event import (
    SandboxEvent,
    SandboxPayload,
    SandboxStage,
)

logger = logging.getLogger(__name__)

EXE_NAME: str = "sandbox_host.exe"

def _exe() -> Path:
    p = Path(__file__).parent / EXE_NAME
    if not p.exists():
        raise RuntimeError(
            f"{EXE_NAME} not found at {p}. Run build.sh to compile it."
        )
    return p


def _build_stdin_payload(config: SandboxConfig) -> dict:
    return {
        "moniker": config.moniker,
        "exe_path": config.exe_path,
        "args": config.args,
        "working_dir": config.working_dir or "",
        "broker_files": [
            {"path": bf.path, "access": bf.access, "mode": bf.mode}
            for bf in config.broker_files
        ],
        "job_config": {
            "cpu_max_rate": config.cpu_max_rate,
            "cpu_min_rate": config.cpu_min_rate,
            "skip_cpu_limit": config.skip_cpu_limit,
            "memory_limit_mb": config.memory_limit_mb or 0,
            "skip_memory_limit": config.memory_limit_mb is None,
        },
        "parent_pid": os.getpid(),
        "breakaway": config.breakaway,
        "capture_target_stdout": config.capture_target_stdout,
    }

def _kill_and_drain(proc: subprocess.Popen) -> str:
    """Kill *proc* and drain its pipes.

    Used when launch() aborts after the host has spawned; without this the
    host keeps running untracked and can block writing to an unread stderr pipe.

    Returns the decoded stderr text, or an empty string if there is none.
    """
    proc.kill()
    try:
        _, stderr_bytes = proc.communicate(timeout=5)
    except Exception:
        stderr_bytes = b""
    if not stderr_bytes:
        return ""
    return stderr_bytes.decode(errors="replace").strip()


def _drain_stderr(proc: subprocess.Popen) -> None:
    """Read and discard the host's stderr until it closes.

    Runs for the life of a successfully launched host; without this nothing
    reads its pipe, so it blocks writing once the OS pipe buffer fills.
    """
    try:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.debug("%s stderr: %s", EXE_NAME, text)
    except (OSError, ValueError):
        pass


# DWORD-sized for win32 PID; Windows never assigns PIDs anywhere near the top
# of that range, so this is a sanity check against a corrupted/malicious value, not a real limit.
_MAX_SANE_PID = 0x7FFFFFFF

# Upper bound for a duplicated Win32 HANDLE value (pointer-sized on x64);
# a sanity check against a corrupted/malicious value, not a real limit.
_MAX_SANE_HANDLE = 0xFFFFFFFFFFFFFFFF

# How long launch() waits for the host's handshake line on stdout before
# killing it and giving up.
_HANDSHAKE_TIMEOUT_SECONDS = 15.0


def _validate(config: SandboxConfig) -> None:
    errors: list[str] = []

    if not config.moniker:
        errors.append("moniker must not be empty")
    if not config.exe_path:
        errors.append("exe_path must not be empty")
    if not Path(config.exe_path).is_file():
        errors.append(f"exe_path does not exist: {config.exe_path}")
    if not (1 <= config.cpu_max_rate <= 100):
        errors.append("cpu_max_rate must be 1–100")
    if not (1 <= config.cpu_min_rate <= 100):
        errors.append("cpu_min_rate must be 1–100")
    if config.cpu_min_rate > config.cpu_max_rate:
        errors.append("cpu_min_rate must not exceed cpu_max_rate")
    if config.memory_limit_mb is not None and config.memory_limit_mb <= 0:
        errors.append("memory_limit_mb must be a positive integer or None")

    if errors:
        raise SandboxError(
            message="; ".join(errors),
            stage=SandboxStage.CONFIG_VALIDATION,
            suggestions=["Check SandboxConfig fields before calling launch()"],
        )


@dataclass
class SandboxHandle:
    moniker: str
    container_sid: str
    pid: int
    # A handle to the target process, duplicated across the process
    # boundary by the host. None if the host did not report one.
    process_handle: int | None = None
    # Carried from SandboxConfig so _watch_event()'s CLEANED_UP dispatch can
    # call revoke_grants() without needing its own thread-start arg.
    broker_files: list[BrokerFile] = field(default_factory=list)
    should_revert_grants: bool = False
    capture_target_stdout: bool = False
    # Populated by _watch_event() from the host's final "exited" JSON line, only
    # when capture_target_stdout was set; stays None otherwise or if parsing fails.
    target_output: str | None = field(default=None, compare=False, hash=False)
    _callbacks: dict[SandboxEvent, list[Callable[[SandboxPayload], None]]] = field(
        default_factory=lambda: defaultdict(list),
        compare=False,
        hash=False,
    )
    _proc: subprocess.Popen = field(compare=False, hash=False, repr=False,
                                    default=None)  # type: ignore[assignment]
    # Set to True by _fire() once CLEANED_UP has been dispatched, so that
    # terminate() can detect the event already fired before it registered.
    _cleaned_up: bool = field(default=False, compare=False, hash=False)
    # Guards process_handle against a close (CLEANED_UP in _fire()) racing a
    # read (checker.py, process.py) that wants to use it before it closes.
    _process_handle_lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, hash=False, repr=False
    )
    # STARTED always fires before the caller can have a handle to listen on, so
    # it's stashed here and replayed to each callback as it registers in on().
    _started_payload: SandboxPayload | None = field(default=None, compare=False, hash=False)

    def on(
        self,
        event: SandboxEvent,
        callback: Callable[[SandboxPayload], None],
    ) -> None:
        self._callbacks[event].append(callback)
        if event == SandboxEvent.STARTED and self._started_payload is not None:
            callback(self._started_payload)

    async def terminate(self) -> None:
        loop = asyncio.get_event_loop()
        cleanup_future: asyncio.Future[None] = loop.create_future()

        def _on_cleaned_up(payload: SandboxPayload) -> None:
            if not cleanup_future.done():
                loop.call_soon_threadsafe(cleanup_future.set_result, None)

        # Registered before inspecting _cleaned_up so a CLEANED_UP event that fires
        # between registration and the check below still resolves the future.
        self.on(SandboxEvent.CLEANED_UP, _on_cleaned_up)

        if self._cleaned_up:
            # CLEANED_UP already fired before we registered the callback;
            # the callback will never be invoked, so resolve the future now.
            if not cleanup_future.done():
                cleanup_future.set_result(None)
        elif self._proc and self._proc.poll() is None:
            self._proc.terminate()
            # No further wait needed. The watcher's proc.poll() fallback already
            # detects this exit and fires CLEANED_UP, which resolves cleanup_future.

        await cleanup_future


async def _watch_event(
    event_name: str,
    handle: SandboxHandle,
    proc: subprocess.Popen,
) -> None:
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    SYNCHRONIZE = 0x00100000
    EVENT_MODIFY_STATE = 0x0002
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    loop = asyncio.get_event_loop()

    # Started here, before any wait below. If nothing reads this pipe while the
    # host writes its large final "exited" line, the write blocks the host inside
    # WriteFile before it can ever signal completion, hanging every wait forever.
    # Only started when capture_target_stdout is set, so other readers of
    # handle._proc.stdout (e.g. wincage.checker) see no competing reader.
    target_output_future: asyncio.Future[bytes] | None = None
    if handle.capture_target_stdout and proc.stdout is not None:
        target_output_future = loop.run_in_executor(None, proc.stdout.readline)

    async def _resolve_target_output() -> None:
        if target_output_future is None:
            return
        try:
            line = await target_output_future
            if line:
                handle.target_output = json.loads(line).get("target_output")
        except Exception:
            logger.error(
                "Failed to read target_output for moniker=%s",
                handle.moniker,
                exc_info=True,
            )

    h_event = kernel32.OpenEventW(
        SYNCHRONIZE | EVENT_MODIFY_STATE,
        False,
        event_name,
    )
    if not h_event:
        error_reason = f"OpenEventW failed for {event_name}: {ctypes.GetLastError()}"
        _fire(handle, SandboxEvent.ERROR, SandboxPayload(
            event=SandboxEvent.ERROR,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=None,
            error=error_reason,
            stage=SandboxStage.WATCHDOG,
        ))
        # Without the named event, fall back to a direct wait on the process itself
        # so EXITED/CLEANED_UP still fire and terminate() awaits a future that only
        # resolves via CLEANED_UP. So skipping it would hang terminate() forever.
        loop = asyncio.get_event_loop()
        rc = await loop.run_in_executor(None, proc.wait)
        await _resolve_target_output()
        _fire(handle, SandboxEvent.EXITED, SandboxPayload(
            event=SandboxEvent.EXITED,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=error_reason,
            stage=None,
        ))
        _revert_grants_if_configured(handle)
        _fire(handle, SandboxEvent.CLEANED_UP, SandboxPayload(
            event=SandboxEvent.CLEANED_UP,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=error_reason,
            stage=SandboxStage.CLEANUP,
        ))
        return

    loop = asyncio.get_event_loop()

    try:
        while True:
            result = await loop.run_in_executor(
                None,
                lambda: kernel32.WaitForSingleObject(h_event, 500),
            )
            if result == WAIT_OBJECT_0:
                break
            if result != WAIT_TIMEOUT:
                break
            if proc.poll() is not None:
                # The host exited without signaling h_event; a hard host crash
                # looks exactly like this, so this is the only way to detect it.
                break

        rc = proc.wait()

        await _resolve_target_output()

        payload_exited = SandboxPayload(
            event=SandboxEvent.EXITED,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=None,
            stage=None,
        )
        _fire(handle, SandboxEvent.EXITED, payload_exited)

        _revert_grants_if_configured(handle)

        payload_cleaned = SandboxPayload(
            event=SandboxEvent.CLEANED_UP,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=None,
            stage=SandboxStage.CLEANUP,
        )
        _fire(handle, SandboxEvent.CLEANED_UP, payload_cleaned)

    finally:
        kernel32.CloseHandle(h_event)


def _revert_grants_if_configured(handle: SandboxHandle) -> None:
    """Best-effort revoke_grants() call for should_revert_grants at CLEANED_UP.

    Exceptions are logged instead of raised so a revoke failure never blocks
    cleanup or leaves SandboxHandle.terminate() hanging.
    """
    if not (handle.should_revert_grants and handle.broker_files):
        return
    try:
        revoke_grants(handle.moniker, handle.broker_files)
    except Exception:
        logger.error(
            "revoke_grants failed during cleanup for moniker=%s",
            handle.moniker,
            exc_info=True,
        )


def _fire(
    handle: SandboxHandle,
    event: SandboxEvent,
    payload: SandboxPayload,
) -> None:
    # Mark before calling callbacks so terminate() sees the flag even if a
    # callback re-enters _fire or inspects _cleaned_up synchronously.
    if event == SandboxEvent.CLEANED_UP:
        handle._cleaned_up = True
        # Only true for direct launch() callers; process.py's container path
        # nulls this out itself once ownership passes to a SandboxProcess.
        #
        # Lock releases before the callback loop below. callbacks are
        # consumer passed in, so holding it there risks a self-deadlock.
        with handle._process_handle_lock:
            if handle.process_handle is not None:
                ctypes.windll.kernel32.CloseHandle(handle.process_handle)
                handle.process_handle = None
    for cb in handle._callbacks.get(event, []):
        try:
            cb(payload)
        except Exception:
            logger.error(
                "Callback raised an unhandled exception [event=%s, callback=%r]",
                event,
                cb,
                exc_info=True,
            )


def _spawn_host(exe_path: Path) -> subprocess.Popen:
    """Start the host executable as a subprocess with piped stdio.

    Returns the Popen handle, or raises SandboxError if the process
    could not be spawned.
    """
    try:
        return subprocess.Popen(
            [str(exe_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise SandboxError(
            message=f"Failed to spawn {EXE_NAME}: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[f"Ensure {EXE_NAME} is built and accessible"],
        ) from exc


def _write_handshake(proc: subprocess.Popen, config: SandboxConfig) -> None:
    """Encode *config* as JSON and write it to the host's stdin.

    Closes stdin after writing so the host sees EOF. Raises SandboxError
    if the write fails.
    """
    # ensure_ascii=False so non-ASCII path characters survive as raw UTF-8;
    # json_parse.h copies bytes verbatim, so \uXXXX escapes would corrupt them.
    stdin_data = json.dumps(_build_stdin_payload(config), ensure_ascii=False).encode()
    try:
        proc.stdin.write(stdin_data)
        proc.stdin.flush()
        proc.stdin.close()
    except OSError as exc:
        raise SandboxError(
            message=f"Failed to write to {EXE_NAME} stdin: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc


def _read_handshake_response(proc: subprocess.Popen, timeout: float) -> dict:
    """Read and parse the host's handshake line from stdout.

    Kills *proc* if no line arrives within *timeout* seconds.

    Raises SandboxError for:
    - a timeout
    - empty output
    - invalid JSON

    Returns the parsed response dict otherwise.
    """
    _timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        _timed_out.set()
        proc.kill()

    _timer = threading.Timer(timeout, _kill_on_timeout)
    _timer.start()
    try:
        stdout_line = proc.stdout.readline()
    except OSError as exc:
        raise SandboxError(
            message=f"Communication with {EXE_NAME} failed: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc
    finally:
        _timer.cancel()

    if _timed_out.is_set():
        raise SandboxError(
            message=f"{EXE_NAME} did not respond within {timeout:.0f} seconds",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=["Check for AppContainer provisioning delays or permission issues"],
        )

    if not stdout_line:
        raise SandboxError(
            message=f"{EXE_NAME} produced no output",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[f"Run {EXE_NAME} manually to debug startup"],
        )

    first_line = stdout_line.strip()
    try:
        return json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise SandboxError(
            message=f"Invalid JSON from {EXE_NAME}: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc


def _validate_handshake_response(response: dict) -> int:
    """Validate the handshake *response* and extract the child's pid.

    Checks for:
    - required fields
    - an embedded error stage
    - a plausible pid range

    Returns the validated pid, or raises SandboxError.
    """
    required = {"sid", "pid", "event_name", "stage"}
    missing = required - response.keys()
    if missing:
        raise SandboxError(
            message=f"{EXE_NAME} response missing fields: {missing}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        )

    if response.get("stage") == "error":
        # error_stage comes from the child's JSON and isn't trusted. SandboxStage[...]
        # raises KeyError on any non-member name, which the try/except below catches.
        error_stage_raw = str(response.get("error_stage", "PROCESS_CREATE")).upper()
        try:
            error_stage = SandboxStage[error_stage_raw]
        except KeyError:
            error_stage = SandboxStage.PROCESS_CREATE
        raise SandboxError(
            message=response.get("error", f"Unknown error from {EXE_NAME}"),
            stage=error_stage,
            suggestions=response.get("suggestions", []),
        )

    # response["pid"] is child-controlled and reaches OpenProcess(PROCESS_ALL_ACCESS,
    # ...) plus Job Object assignment with KILL_ON_JOB_CLOSE. A wrong-but-plausible
    # integer would be destructive to an unrelated process. This range check alone
    # does not close the PID-reuse race.
    pid = response["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or not (0 < pid <= _MAX_SANE_PID):
        raise SandboxError(
            message=f"{EXE_NAME} reported an invalid pid: {pid!r}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        )

    # process_handle is optional but when present and non-null it is used directly as a Win32
    # HANDLE. Note: a wrong but valid value would be passed straight into WinAPI calls.
    process_handle = response.get("process_handle")
    if process_handle is not None:
        if (
            isinstance(process_handle, bool)
            or not isinstance(process_handle, int)
            or not (0 < process_handle <= _MAX_SANE_HANDLE)
        ):
            raise SandboxError(
                message=f"{EXE_NAME} reported an invalid process_handle: {process_handle!r}",
                stage=SandboxStage.PROCESS_CREATE,
                suggestions=[],
            )

    return pid


def _build_sandbox_handle(
    config: SandboxConfig,
    response: dict,
    pid: int,
    proc: subprocess.Popen,
) -> SandboxHandle:
    """Build the SandboxHandle for a launched host.

    Registers an ERROR listener that logs, so ERROR events are never
    silently dropped. Returns the new handle.
    """
    handle = SandboxHandle(
        moniker=config.moniker,
        container_sid=response["sid"],
        pid=pid,
        process_handle=response.get("process_handle"),
        broker_files=config.broker_files,
        should_revert_grants=config.should_revert_grants,
        capture_target_stdout=config.capture_target_stdout,
        _callbacks=defaultdict(list),
        _proc=proc,
    )

    handle._started_payload = SandboxPayload(
        event=SandboxEvent.STARTED,
        moniker=handle.moniker,
        pid=handle.pid,
        exit_code=None,
        error=None,
        stage=None,
    )

    # Registered before the watcher thread starts so it's always present
    # by the time _watch_event could fire ERROR.
    def _log_sandbox_error(payload: SandboxPayload) -> None:
        logger.error(
            "Sandbox ERROR event: moniker=%s pid=%s stage=%s error=%s",
            payload.moniker, payload.pid, payload.stage, payload.error,
        )
    handle.on(SandboxEvent.ERROR, _log_sandbox_error)

    return handle


def _start_background_threads(
    handle: SandboxHandle,
    proc: subprocess.Popen,
    response: dict,
) -> None:
    """Start the stderr drain and event watcher threads.

    Both threads are daemon threads that run for the life of the
    launched host.
    """
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    loop = asyncio.new_event_loop()

    def _run_watcher() -> None:
        try:
            loop.run_until_complete(
                _watch_event(response["event_name"], handle, proc)
            )
        finally:
            # loop.close() alone doesn't drain the default executor that
            # run_in_executor(None, ...) lazily creates; without this its worker thread leaks.
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

    threading.Thread(target=_run_watcher, daemon=True).start()


def launch(config: SandboxConfig) -> SandboxHandle:
    _validate(config)

    proc = _spawn_host(_exe())

    # success stays False until the handle and its background threads are fully
    # up; the finally block below cleans up proc for any exception before that.
    success = False
    try:
        _write_handshake(proc, config)
        response = _read_handshake_response(proc, _HANDSHAKE_TIMEOUT_SECONDS)
        pid = _validate_handshake_response(response)
        handle = _build_sandbox_handle(config, response, pid, proc)
        _start_background_threads(handle, proc, response)

        success = True
        return handle
    finally:
        if not success:
            stderr_text = _kill_and_drain(proc)
            if stderr_text:
                logger.error("%s stderr: %s", EXE_NAME, stderr_text)


def reset_container(moniker: str, broker_files: list[BrokerFile] | None = None) -> None:
    if not moniker:
        raise SandboxError(
            message="moniker must not be empty",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=[
                "Pass the same non-empty moniker string that was used to launch "
                "the sandbox, i.e. SandboxConfig.moniker",
            ],
        )

    # Deleting the profile alone leaves any DACL ACEs granted to its SID orphaned.
    # Revoking first (order doesn't matter, see revoke_grants()) closes that gap
    # for callers who still have the broker_files list on hand.
    if broker_files:
        revoke_grants(moniker, broker_files)

    try:
        proc = subprocess.run(
            [str(_exe()), "--reset", moniker],
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(
            message=f"reset_container timed out for moniker '{moniker}'",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=["Check if AppContainer delete is stuck"],
        )
    except OSError as exc:
        raise SandboxError(
            message=f"Failed to invoke {EXE_NAME} --reset: {exc}",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=[],
        ) from exc

    if proc.returncode != 0:
        stderr_text = proc.stderr.decode(errors="replace").strip()
        raise SandboxError(
            message=f"reset_container failed for '{moniker}': {stderr_text}",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=["Moniker may not exist; this is safe to ignore on first run"],
        )


def _parse_error_response(stdout_bytes: bytes) -> dict | None:
    try:
        return json.loads(stdout_bytes.decode(errors="replace").strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def revoke_grants(moniker: str, broker_files: list[BrokerFile]) -> None:
    """Mirror of the DACL grants launch() applies for broker_files.

    - mode="grant"/"secure" entries have their ACE removed. mode="inherit"
      never touched a DACL, so those are skipped.
    - Stateless: pass the same broker_files list you originally granted with.
    - Not coupled to container lifecycle: safe regardless of whether a
      process is still running under moniker; independent of
      reset_container().
    """
    if not moniker:
        raise SandboxError(
            message="moniker must not be empty",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=[
                "Pass the same moniker string that was used to grant "
                "broker_files, i.e. SandboxConfig.moniker",
            ],
        )

    stdin_data = json.dumps(
        {
            "broker_files": [
                {"path": bf.path, "access": bf.access, "mode": bf.mode}
                for bf in broker_files
            ],
        },
        ensure_ascii=False,
    ).encode()

    try:
        proc = subprocess.run(
            [str(_exe()), "--revoke", moniker],
            input=stdin_data,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(
            message=f"revoke_grants timed out for moniker '{moniker}'",
            stage=SandboxStage.DACL_REVOKE,
            suggestions=["Check if a DACL revoke walk is stuck on a large tree"],
        )
    except OSError as exc:
        raise SandboxError(
            message=f"Failed to invoke {EXE_NAME} --revoke: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc

    if proc.returncode != 0:
        response = _parse_error_response(proc.stdout)
        if response is not None and response.get("stage") == "error":
            # error_stage comes from the child's JSON and isn't trusted: SandboxStage[...]
            # raises KeyError on any non-member name, which the try/except below catches.
            error_stage_raw = str(response.get("error_stage", "DACL_REVOKE")).upper()
            try:
                error_stage = SandboxStage[error_stage_raw]
            except KeyError:
                error_stage = SandboxStage.DACL_REVOKE
            raise SandboxError(
                message=response.get("error", f"revoke_grants failed for '{moniker}'"),
                stage=error_stage,
                suggestions=response.get("suggestions", []),
            )

        stderr_text = proc.stderr.decode(errors="replace").strip()
        raise SandboxError(
            message=f"revoke_grants failed for '{moniker}': {stderr_text or 'unknown error'}",
            stage=SandboxStage.DACL_REVOKE,
            suggestions=[],
        )
