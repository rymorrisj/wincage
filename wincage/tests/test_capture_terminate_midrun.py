"""
Launches a long-running process with capture_target_stdout=True, calls
terminate() mid-run, confirms cleanup completes without hanging and does a
best-effort check for an obvious handle/thread leak. Process handle count
(via GetProcessHandleCount) and Python thread count sampled before launch
and again after CLEANED_UP plus a short settle delay.

This is not a leak-proof: a small, bounded increase in handles
is normal (subprocess pipe buffers, OS bookkeeping). It looks for a 
dramatic, unbounded increase instead.

Run from the repo root after `pip install -e .`:
    python test_capture_terminate_midrun.py
"""

import asyncio
import ctypes
import sys
import threading
import time

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.capture_terminate_midrun"

_kernel32 = ctypes.windll.kernel32

# Slack allowed above the pre-launch handle count before this is treated as a
# leak signal rather than normal OS/runtime bookkeeping noise.
_HANDLE_SLACK = 20


def get_handle_count() -> int:
    count = ctypes.c_ulong(0)
    pseudo_handle = _kernel32.GetCurrentProcess()
    _kernel32.GetProcessHandleCount(pseudo_handle, ctypes.byref(count))
    return count.value


async def main() -> int:
    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command", "Start-Sleep -Seconds 30"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        capture_target_stdout=True,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handles_before = get_handle_count()
    threads_before = threading.active_count()

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

    await asyncio.sleep(1.0)  # let it actually get running before terminating

    t0 = time.monotonic()
    try:
        await asyncio.wait_for(handle.terminate(), timeout=15)
    except asyncio.TimeoutError:
        print("FAIL: terminate() did not resolve within 15s")
        return 1
    terminate_elapsed = time.monotonic() - t0
    print(f"terminate() resolved in {terminate_elapsed:.2f}s")

    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    except asyncio.TimeoutError:
        print("FAIL: CLEANED_UP never fired within 5s of terminate() resolving")
        return 1

    # Give the daemon watcher/stderr-drain threads a moment to actually exit
    # after CLEANED_UP fires, rather than sampling mid-teardown.
    await asyncio.sleep(1.5)

    handles_after = get_handle_count()
    threads_after = threading.active_count()

    print(f"Process handle count: before={handles_before} after={handles_after}")
    print(f"Python thread count:  before={threads_before} after={threads_after}")

    failures = []
    if handles_after > handles_before + _HANDLE_SLACK:
        failures.append(
            f"handle count grew by {handles_after - handles_before}, "
            f"more than the {_HANDLE_SLACK} slack allowed"
        )
    if threads_after > threads_before:
        failures.append(
            f"thread count grew from {threads_before} to {threads_after}, "
            f"background threads may not have wound down"
        )

    if failures:
        print("FAIL (best-effort leak check):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: terminate() and cleanup completed promptly, no obvious handle/thread leak")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
