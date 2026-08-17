"""
This script runs the workload and limit through process.py's launch_suspended()/run_under_job(), 
which assign the Job Object directly with no AppContainer. If this one dies early
and the AppContainer one doesn't, AppContainer confinement itself is
implicated. If this one also fails to die, the Job Object limit itself
is the shared cause.

Run from the repo root after `pip install -e .`:
    python test_memory_native.py
"""

import time

from wincage.process import launch_suspended, run_under_job

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

PS_COMMAND = (
    "$held = New-Object 'byte[][]' 20; "
    "for ($i=0; $i -lt 20; $i++) { "
    "$held[$i] = New-Object byte[] (50MB); "
    "for ($j=0; $j -lt $held[$i].Length; $j += 4096) { $held[$i][$j] = 1 }; "
    "Start-Sleep -Milliseconds 300 }; "
    "Write-Output 'completed all allocations'"
)


def main() -> None:
    print("==== Native (non-AppContainer) memory cap test ====")
    print("Launching via launch_suspended() + run_under_job(), memory_limit_mb=100,")
    print("same held-rooted 20x50MB workload as test_job_limits.py's AppContainer test.")

    args = ["-NoProfile", "-Command", PS_COMMAND]

    # sandbox_config=None selects the native CreateProcessW path, not the
    # AppContainer path
    process = launch_suspended(PS, args, flags=0, cwd=None, sandbox_config=None)

    t0 = time.monotonic()
    try:
        # apply_limits=True: this Job Object numerically enforces the caps
        # itself (the native path), rather than assuming sandbox_host.exe
        # already did it (that's the container path, apply_limits=False).
        process, job_object = run_under_job(
            executable_path=PS,
            args=args,
            base_flags=0,
            cwd=None,
            process=process,
            job_name="wincage.test.memory_native",
            memory_limit_mb=100,
            cpu_limit_percent=80,
            apply_limits=True,
            cpu_min_rate_percent=5,
            skip_cpu_limit=False,
            skip_memory_limit=False,
            sandbox_config=None,
        )
    except Exception as exc:
        print(f"    FAIL: run_under_job raised: {exc}")
        return

    try:
        exit_code = process.wait(timeout_ms=15_000)
    except Exception as exc:
        print(f"    FAIL: wait() raised: {exc}")
        job_object.teardown()
        return

    elapsed = time.monotonic() - t0
    job_object.teardown()

    print(f"    Exit code: {exit_code}")
    print(f"    Time to exit: {elapsed:.2f}s (unconstrained would be ~6s)")

    if elapsed < 4.0:
        print("    RESULT: died early -> memory limit WAS enforced on the native path.")
    else:
        print("    RESULT: ran close to ~6s -> memory limit was NOT enforced on the native path either.")


if __name__ == "__main__":
    main()
