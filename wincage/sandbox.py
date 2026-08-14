from __future__ import annotations

import asyncio
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
# used below (OpenEventW, WaitForSingleObject, CloseHandle) on the shared
# ctypes.windll.kernel32 singleton. See win32_types.py's "kernel32 function
# signatures" section for why this must be imported before those calls run.
from . import win32_types as _win32_types  # noqa: F401
from .sandbox_config import SandboxConfig
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
    }

def _kill_and_drain(proc: subprocess.Popen) -> str:
    """Ensure *proc* is terminated and its pipes drained/closed.

    Called on every error branch in launch() after the child has spawned.

    A child may have already provisioned the AppContainer and resumed the
    process before hitting a Python side error (bad JSON, missing
    fields, a stalled handshake). Without this, it would keep running
    untracked, and its stderr pipe would go unread, risking a
    fill-and-block if it ever writes enough to it.

    Returns the decoded stderr text (possibly empty) for the caller to log.
    """
    proc.kill()
    try:
        _, stderr_bytes = proc.communicate(timeout=5)
    except Exception:
        stderr_bytes = b""
    if not stderr_bytes:
        return ""
    return stderr_bytes.decode(errors="replace").strip()


# Reasonable upper bound for a Win32 PID. DWORD-sized, but Windows never
# actually assigns process IDs anywhere near the top of that range. This
# is a sanity check against a corrupted/malicious value, not a real
# limit.
_MAX_SANE_PID = 0x7FFFFFFF


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

    def on(
        self,
        event: SandboxEvent,
        callback: Callable[[SandboxPayload], None],
    ) -> None:
        self._callbacks[event].append(callback)

    async def terminate(self) -> None:
        loop = asyncio.get_event_loop()
        cleanup_future: asyncio.Future[None] = loop.create_future()

        def _on_cleaned_up(payload: SandboxPayload) -> None:
            if not cleanup_future.done():
                loop.call_soon_threadsafe(cleanup_future.set_result, None)

        # Register the callback BEFORE inspecting _cleaned_up so that a
        # CLEANED_UP event fired between registration and the check below
        # still resolves the future via the callback, not just the check.
        self.on(SandboxEvent.CLEANED_UP, _on_cleaned_up)

        if self._cleaned_up:
            # CLEANED_UP already fired before we registered the callback;
            # the callback will never be invoked, so resolve the future now.
            if not cleanup_future.done():
                cleanup_future.set_result(None)
        elif self._proc and self._proc.poll() is None:
            self._proc.terminate()

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
        # Without the named event we can't wait for the host's own signal, but
        # we can still fall back to a direct wait on the process itself so
        # EXITED/CLEANED_UP fire regardless. SandboxHandle.terminate() awaits a
        # cleanup_future that only ever resolves via CLEANED_UP, returning
        # here without firing it would let terminate() hang forever.
        loop = asyncio.get_event_loop()
        rc = await loop.run_in_executor(None, proc.wait)
        _fire(handle, SandboxEvent.EXITED, SandboxPayload(
            event=SandboxEvent.EXITED,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=error_reason,
            stage=None,
        ))
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

        rc = proc.wait()

        payload_exited = SandboxPayload(
            event=SandboxEvent.EXITED,
            moniker=handle.moniker,
            pid=handle.pid,
            exit_code=rc,
            error=None,
            stage=None,
        )
        _fire(handle, SandboxEvent.EXITED, payload_exited)

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


def _fire(
    handle: SandboxHandle,
    event: SandboxEvent,
    payload: SandboxPayload,
) -> None:
    # Mark before calling callbacks so terminate() sees the flag even if a
    # callback re-enters _fire or inspects _cleaned_up synchronously.
    if event == SandboxEvent.CLEANED_UP:
        handle._cleaned_up = True
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


def launch(config: SandboxConfig) -> SandboxHandle:
    _validate(config)

    # ensure_ascii=False so non-ASCII path characters survive as raw UTF-8
    # across the C++ boundary. json_parse.h copies non-escape bytes verbatim,
    # so \uXXXX escapes (the ensure_ascii=True default) would corrupt them.
    stdin_data = json.dumps(_build_stdin_payload(config), ensure_ascii=False).encode()

    try:
        proc = subprocess.Popen(
            [str(_exe())],
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

    try:
        proc.stdin.write(stdin_data)
        proc.stdin.flush()
        proc.stdin.close()
    except OSError as exc:
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"Failed to write to {EXE_NAME} stdin: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc

    _timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        _timed_out.set()
        proc.kill()

    _timer = threading.Timer(15.0, _kill_on_timeout)
    _timer.start()
    try:
        stdout_line = proc.stdout.readline()
    except OSError as exc:
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"Communication with {EXE_NAME} failed: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc
    finally:
        _timer.cancel()

    if _timed_out.is_set():
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"{EXE_NAME} did not respond within 15 seconds",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=["Check for AppContainer provisioning delays or permission issues"],
        )

    if not stdout_line:
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"{EXE_NAME} produced no output",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[f"Run {EXE_NAME} manually to debug startup"],
        )

    first_line = stdout_line.strip()
    try:
        response = json.loads(first_line)
    except json.JSONDecodeError as exc:
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"Invalid JSON from {EXE_NAME}: {exc}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        ) from exc

    required = {"sid", "pid", "event_name", "stage"}
    missing = required - response.keys()
    if missing:
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"{EXE_NAME} response missing fields: {missing}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        )

    if response.get("stage") == "error":
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        # error_stage comes from the child's JSON and is not trusted.
        # SandboxStage[...] raises KeyError on anything that is not an exact
        # member name, which would escape as an unhandled exception instead
        # of the SandboxError this function promises.
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

    # response["pid"] is child-controlled. It reaches
    # OpenProcess(PROCESS_ALL_ACCESS, ...) plus Job Object assignment with
    # KILL_ON_JOB_CLOSE (see process.py's run_under_job). A
    # wrong-but-plausible integer would be destructive to an unrelated
    # process.
    #
    # Range check does not close the PID-reuse race.
    pid = response["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or not (0 < pid <= _MAX_SANE_PID):
        stderr_text = _kill_and_drain(proc)
        if stderr_text:
            logger.error("%s stderr: %s", EXE_NAME, stderr_text)
        raise SandboxError(
            message=f"{EXE_NAME} reported an invalid pid: {pid!r}",
            stage=SandboxStage.PROCESS_CREATE,
            suggestions=[],
        )

    handle = SandboxHandle(
        moniker=config.moniker,
        container_sid=response["sid"],
        pid=pid,
        _callbacks=defaultdict(list),
        _proc=proc,
    )

    # Without a listener, an ERROR payload (e.g. OpenEventW failing in the
    # watcher thread) is dispatched to zero callbacks and effectively
    # swallowed. Registered before the watcher thread starts so it's always
    # present by the time _watch_event could fire ERROR.
    def _log_sandbox_error(payload: SandboxPayload) -> None:
        logger.error(
            "Sandbox ERROR event: moniker=%s pid=%s stage=%s error=%s",
            payload.moniker, payload.pid, payload.stage, payload.error,
        )
    handle.on(SandboxEvent.ERROR, _log_sandbox_error)

    started_payload = SandboxPayload(
        event=SandboxEvent.STARTED,
        moniker=config.moniker,
        pid=pid,
        exit_code=None,
        error=None,
        stage=None,
    )
    _fire(handle, SandboxEvent.STARTED, started_payload)

    loop = asyncio.new_event_loop()

    def _run_watcher() -> None:
        try:
            loop.run_until_complete(
                _watch_event(response["event_name"], handle, proc)
            )
        finally:
            loop.close()

    threading.Thread(target=_run_watcher, daemon=True).start()

    return handle


def reset_container(moniker: str) -> None:
    if not moniker:
        raise SandboxError(
            message="moniker must not be empty",
            stage=SandboxStage.CONTAINER_PROVISION,
            suggestions=[
                "Pass the same non-empty moniker string that was used to launch "
                "the sandbox, i.e. SandboxConfig.moniker",
            ],
        )

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
