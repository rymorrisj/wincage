"""
Launches a process that writes a non-printable control byte (0x02, STX) to
stdout with capture_target_stdout=True. Confirms the host's exited line JSON
still parses cleanly in Python

Run from the repo root after `pip install -e .`:
    python test_capture_control_chars.py
"""

import asyncio
import logging
import sys

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.capture_control_chars"
EXPECTED_TEXT = "BEFORE\x02AFTER"


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


async def main() -> int:
    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command",
              "[Console]::Out.Write('BEFORE' + [char]0x02 + 'AFTER')"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        capture_target_stdout=True,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    capture_handler = _CaptureHandler()
    sandbox_logger = logging.getLogger("wincage.sandbox")
    prior_level = sandbox_logger.level
    sandbox_logger.addHandler(capture_handler)
    sandbox_logger.setLevel(logging.ERROR)

    try:
        handle = wincage.launch(config)
        handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        except asyncio.TimeoutError:
            print("FAIL: never reached CLEANED_UP within 20s")
            await handle.terminate()
            return 1
    finally:
        sandbox_logger.removeHandler(capture_handler)
        sandbox_logger.setLevel(prior_level)

    parse_errors = [r for r in capture_handler.records if "Failed to read target_output" in r]
    if parse_errors:
        print(f"FAIL: exited-line JSON failed to parse with a control byte present: {parse_errors}")
        return 1

    if handle.target_output is None:
        print("FAIL: target_output is None; JSON parse of the exited line likely failed silently")
        return 1

    print(f"PASS: exited-line JSON parsed cleanly with a control byte in the payload (no exception raised)")

    if handle.target_output == EXPECTED_TEXT:
        print(f"  bonus: control byte round-tripped exactly, target_output == {EXPECTED_TEXT!r}")
    else:
        print(f"  note: control byte content differs from expected ({handle.target_output!r}); "
              f"this does not fail the test, only exact JSON-parseability is asserted above")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
