"""
Confirms capture_target_stdout's default (False) leaves SandboxHandle.target_output
absent and does not change exit reporting (EXITED/CLEANED_UP payloads/exit_code).

Run from the repo root after `pip install -e .`:
    python test_capture_default_off.py
"""

import asyncio
import sys

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.capture_default_off"


async def main() -> int:
    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command", "Write-Output 'irrelevant, capture is off'"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        # capture_target_stdout intentionally left at its default (False).
    )

    exited_payload = {}
    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_exited(payload):
        exited_payload["payload"] = payload

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)

    if handle.capture_target_stdout is not False:
        print(f"FAIL: handle.capture_target_stdout is {handle.capture_target_stdout!r}, expected False")
        return 1

    handle.on(wincage.SandboxEvent.EXITED, on_exited)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

    try:
        await asyncio.wait_for(done.wait(), timeout=20)
    except asyncio.TimeoutError:
        print("FAIL: never reached CLEANED_UP within 20s")
        await handle.terminate()
        return 1

    if handle.target_output is not None:
        print(f"FAIL: target_output is {handle.target_output!r}, expected None when capture is off")
        return 1

    exited = exited_payload.get("payload")
    if exited is None:
        print("FAIL: EXITED event never observed")
        return 1
    if exited.exit_code != 0:
        print(f"FAIL: unexpected exit_code={exited.exit_code!r}, expected 0")
        return 1
    if exited.error is not None:
        print(f"FAIL: unexpected error on EXITED payload: {exited.error!r}")
        return 1

    print("PASS: target_output stayed None with capture_target_stdout at its default, exit reporting unaffected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
