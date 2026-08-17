"""
Launches a process that writes 5,000,000 bytes of stdout with capture_target_stdout=True. 
Confirms no hang and that target_output's size and content are both intact.

Run from the repo root after `pip install -e .`:
    python test_capture_large_output.py
"""

import asyncio
import sys

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.capture_large_output"

_BLOCK = "0123456789"
_REPEAT_COUNT = 500_000
EXPECTED_TEXT = _BLOCK * _REPEAT_COUNT  # 5,000,000 chars
EXPECTED_LEN = len(EXPECTED_TEXT)


async def main() -> int:
    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command",
              f"$block = ('{_BLOCK}' * {_REPEAT_COUNT}); [Console]::Out.Write($block)"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=512,
        capture_target_stdout=True,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except asyncio.TimeoutError:
        print("FAIL: never reached CLEANED_UP within 60s, appears hung")
        await handle.terminate()
        return 1

    if handle.target_output is None:
        print("FAIL: target_output is None, capture never populated")
        return 1

    actual_len = len(handle.target_output)
    if actual_len != EXPECTED_LEN:
        print(f"FAIL: target_output length={actual_len}, expected {EXPECTED_LEN} (size mismatch, likely truncation)")
        return 1

    if handle.target_output != EXPECTED_TEXT:
        # Find the first differing offset to make a mismatch actionable.
        first_diff = next(
            (i for i in range(EXPECTED_LEN) if handle.target_output[i] != EXPECTED_TEXT[i]),
            None,
        )
        print(f"FAIL: content mismatch at offset {first_diff}, integrity not preserved")
        return 1

    print(f"PASS: captured {actual_len:,} bytes with no hang, content matches expected pattern exactly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
