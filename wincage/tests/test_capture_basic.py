"""
Basic capture_target_stdout=True test: launches a process that writes a known,
fixed string to stdout with no trailing newline, confirms
SandboxHandle.target_output matches that string exactly.

Run from the repo root after `pip install -e .`:
    python test_capture_basic.py
"""

import asyncio
import sys

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.capture_basic"

# [Console]::Out.Write (not Write-Output/WriteLine) so no trailing newline is added
EXPECTED_TEXT = "WINCAGE_CAPTURE_BASIC_MARKER_9f3a1c"


async def main() -> int:
    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command", f"[Console]::Out.Write('{EXPECTED_TEXT}')"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        capture_target_stdout=True,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

    try:
        await asyncio.wait_for(done.wait(), timeout=20)
    except asyncio.TimeoutError:
        print("FAIL: never reached CLEANED_UP within 20s")
        await handle.terminate()
        return 1

    if handle.target_output != EXPECTED_TEXT:
        print(f"FAIL: target_output={handle.target_output!r}, expected {EXPECTED_TEXT!r}")
        return 1

    print(f"PASS: target_output matched the expected fixed text exactly ({len(EXPECTED_TEXT)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
