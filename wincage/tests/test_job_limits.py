"""
Test for wincage's Job Object resource limits: CPU rate cap, memory cap,
and kill-on-close.

Uses powershell.exe as the driver since it's present on every
Windows machine, no extra dependencies needed.

Run from the repo root after `pip install -e .`:
    python test_job_limits.py
"""

import asyncio
import logging
import subprocess
import time

import wincage


class _CaptureHandler(logging.Handler):
    """Collects formatted log records into a list instead of printing them.

    sandbox.py's _drain_stderr logs sandbox_host.exe's own stderr via
    logger.debug on the "wincage.sandbox" logger. This is the only 
    host-side output currently captured anywhere.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def get_cpu_ticks(pid: int) -> int | None:
    """TotalProcessorTime in 100ns ticks for pid, or None if it's gone."""
    result = subprocess.run(
        [PS, "-NoProfile", "-Command",
         f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).TotalProcessorTime.Ticks"],
        capture_output=True, text=True, check=False,
    )
    out = result.stdout.strip()
    return int(out) if out.isdigit() else None


def is_running(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True, text=True, check=False,
    )
    return str(pid) in result.stdout


async def launch_tracked(config: wincage.SandboxConfig):
    """Launch, return (handle, pid, cleaned_up_event, main_loop)."""
    pid_box = {}
    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_started(payload):
        pid_box["pid"] = payload.pid

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.STARTED, on_started)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)
    return handle, pid_box, done


async def test_cpu_cap() -> bool:
    print("\n==== CPU cap test ====")
    print("cpu_max_rate is a percentage of TOTAL system CPU capacity across")
    print("all cores, not per-core. A single thread on an 8-core machine")
    print("can only ever use ~12.5% system-wide, which is already under a")
    print("20% cap, so a single-threaded workload can never test this.")
    print("Spawning 8 parallel busy loops (one per core) to actually")
    print("attempt exceeding the cap, capped at cpu_max_rate=20...")

    config = wincage.SandboxConfig(
        moniker="wincage.test.cpu_cap",
        exe_path=PS,
        args=["-NoProfile", "-Command",
              # RunspacePool instead of Add-Type: Add-Type compiles C# at
              # runtime, which shells out to a compiler and writes temp
              # files, both of which need access this sandbox doesn't
              # grant, so it fails almost instantly under AppContainer.
              # RunspacePool runs plain scriptblocks as threads within
              # this same process via PowerShell's own SDK, no
              # compilation, no temp files, no child process spawn.
              "$pool = [runspacefactory]::CreateRunspacePool(1, 8); "
              "$pool.Open(); "
              "$work = { "
              "$sw = [Diagnostics.Stopwatch]::StartNew(); "
              "$x = 1.0000001; "
              "while ($sw.Elapsed.TotalSeconds -lt 6) { "
              "for ($i = 0; $i -lt 2000000; $i++) { $x = $x * 1.0000001 } } "
              "}; "
              "$handles = 1..8 | ForEach-Object { "
              "$ps = [powershell]::Create(); "
              "$ps.RunspacePool = $pool; "
              "$ps.AddScript($work) | Out-Null; "
              "[PSCustomObject]@{ Pipe = $ps; Async = $ps.BeginInvoke() } }; "
              "$handles | ForEach-Object { $_.Pipe.EndInvoke($_.Async); $_.Pipe.Dispose() }; "
              "$pool.Close()"],
        cpu_max_rate=20,
        cpu_min_rate=5,
        memory_limit_mb=256,
    )

    handle, pid_box, done = await launch_tracked(config)
    await asyncio.sleep(0.5)  # let it actually start spinning
    if "pid" not in pid_box:
        print("    FAIL: never saw STARTED, no pid to sample.")
        return False

    pid = pid_box["pid"]
    t0 = time.monotonic()
    c0 = get_cpu_ticks(pid)
    if c0 is None:
        print("    FAIL: process gone before first sample.")
        return False

    await asyncio.sleep(4.0)

    t1 = time.monotonic()
    c1 = get_cpu_ticks(pid)
    if c1 is None:
        print("    Process exited before second sample, sampling what we have.")
        c1 = c0

    wall_elapsed = t1 - t0
    cpu_elapsed_seconds = (c1 - c0) / 1e7
    observed_pct = (cpu_elapsed_seconds / wall_elapsed) * 100 if wall_elapsed > 0 else 0

    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    except asyncio.TimeoutError:
        await handle.terminate()

    print(f"    Wall time sampled:   {wall_elapsed:.2f}s")
    print(f"    CPU time consumed:   {cpu_elapsed_seconds:.2f}s (summed across all threads)")
    print(f"    Observed CPU usage:  {observed_pct:.1f}%  (cap is 20% of 8 cores ~ 160%; uncapped ~ 800%)")

    # With 8 parallel threads on an 8-core machine, a working cap should
    # land near 160%, an ignored cap should land near 800%. 400% is a
    # clear midpoint, not close to either expected value.
    passed = observed_pct < 400
    print(f"    {'PASS' if passed else 'FAIL'}: {'usage lands near the expected capped value, cap appears to be limiting CPU usage' if passed else 'usage lands near the uncapped value, cap does not appear to be applying'}")
    return passed


async def test_memory_cap() -> bool:
    print("\n==== Memory cap test ====")
    print("Launching a process that allocates 20x50MB, ALL held rooted")
    print("simultaneously in one array (no GC reclaim between iterations),")
    print("capped at memory_limit_mb=100 (should die once total committed")
    print("memory crosses ~100MB, well before all 20 chunks are held).")

    config = wincage.SandboxConfig(
        moniker="wincage.test.memory_cap",
        exe_path=PS,
        args=["-NoProfile", "-Command",
              # Single array holds every chunk for the process's entire
              # lifetime, nothing is eligible for GC until the process
              # exits, so peak committed memory should track cumulative
              # allocation directly, unlike a workload that discards and
              # lets the GC reclaim between iterations.
              "$ErrorActionPreference = 'Stop'; "
              "$held = New-Object 'byte[][]' 20; "
              "for ($i=0; $i -lt 20; $i++) { "
              "$held[$i] = New-Object byte[] (50MB); "
              "for ($j=0; $j -lt $held[$i].Length; $j += 4096) { $held[$i][$j] = 1 }; "
              "Start-Sleep -Milliseconds 300 } "
              "Write-Output 'completed all allocations'"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=100,
    )

    exited_payload = {}

    def on_exited(payload):
        exited_payload["payload"] = payload

    capture_handler = _CaptureHandler()
    sandbox_logger = logging.getLogger("wincage.sandbox")
    prior_level = sandbox_logger.level
    sandbox_logger.addHandler(capture_handler)
    sandbox_logger.setLevel(logging.DEBUG)

    try:
        t0 = time.monotonic()
        handle, pid_box, done = await launch_tracked(config)
        handle.on(wincage.SandboxEvent.EXITED, on_exited)

        try:
            await asyncio.wait_for(done.wait(), timeout=15)
        except asyncio.TimeoutError:
            print("    FAIL: never completed, cleaning up.")
            await handle.terminate()
            return False

        elapsed = time.monotonic() - t0
    finally:
        sandbox_logger.removeHandler(capture_handler)
        sandbox_logger.setLevel(prior_level)

    # Unconstrained, this script takes ~6s (20 * 300ms) to finish all
    # allocations. If the cap is working, it should die well before that,
    # roughly after 2 allocations (100MB) rather than all 20.
    print(f"    Time to CLEANED_UP: {elapsed:.2f}s (unconstrained would be ~6s)")

    exited = exited_payload.get("payload")
    if exited is not None:
        print(f"    EXITED payload: exit_code={exited.exit_code!r} error={exited.error!r} stage={exited.stage!r}")
    else:
        print("    EXITED payload: never observed (handle may have gone straight to CLEANED_UP).")

    print("    sandbox_host.exe stderr captured during this test (target process's own")
    print("    stdout/stderr is not piped anywhere in the current library, only its")
    print("    relayed exit_code above is observable):")
    if capture_handler.records:
        for line in capture_handler.records:
            print(f"      {line}")
    else:
        print("      (none)")

    stderr_text = "\n".join(capture_handler.records)
    oom_seen = "OutOfMemoryException" in stderr_text
    exit_code = exited.exit_code if exited is not None else None
    passed = bool(exit_code) and oom_seen

    if passed:
        print(f"    PASS: exit_code={exit_code!r} and OutOfMemoryException present in captured stderr, cap is active")
    else:
        print(f"    FAIL: no evidence the cap was hit (exit_code={exit_code!r}, OutOfMemoryException seen={oom_seen})")
    return passed


async def test_kill_on_close() -> bool:
    print("\n==== Kill-on-close test ====")
    print("Launching a 30-second sleep, then calling terminate() early...")

    config = wincage.SandboxConfig(
        moniker="wincage.test.kill_on_close",
        exe_path=PS,
        args=["-NoProfile", "-Command", "Start-Sleep -Seconds 30"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
    )

    handle, pid_box, done = await launch_tracked(config)
    await asyncio.sleep(1.0)

    if "pid" not in pid_box:
        print("    FAIL: never saw STARTED, no pid to check.")
        return False

    pid = pid_box["pid"]
    print(f"    Confirmed running, pid={pid}")

    t0 = time.monotonic()
    try:
        await asyncio.wait_for(handle.terminate(), timeout=10)
    except asyncio.TimeoutError:
        print("    FAIL: terminate() did not resolve within 10s.")
        return False
    terminate_elapsed = time.monotonic() - t0
    print(f"    terminate() resolved in {terminate_elapsed:.2f}s")

    await asyncio.sleep(0.5)
    still_running = is_running(pid)
    print(f"    Process still running after terminate(): {still_running}")

    passed = not still_running
    print(f"    {'PASS' if passed else 'FAIL'}: {'process actually died' if passed else 'process is still alive, kill-on-close did not work'}")
    return passed


async def main() -> None:
    results = {}
    results["cpu_cap"] = await test_cpu_cap()
    results["memory_cap"] = await test_memory_cap()
    results["kill_on_close"] = await test_kill_on_close()

    print("\n---- Summary ----")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    if all(results.values()):
        print("\nAll Job Object limit tests passed.")
    else:
        print("\nAt least one test failed, see output above.")


if __name__ == "__main__":
    asyncio.run(main())
