"""
Confirms the host's final "exited" JSON handshake line is still well-formed
and parses cleanly when capture_target_stdout=True: EXITED/CLEANED_UP fire
with valid payloads, target_output actually populates, and no parse-failure error
is logged on the "wincage.sandbox" logger.

Run from the repo root after `pip install -e .`:
    python test_handshake_unaffected.py
"""

import asyncio
import logging
import sys

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.handshake_unaffected"
EXPECTED_TEXT = "HANDSHAKE_CHECK_OK"


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
        args=["-NoProfile", "-Command", f"[Console]::Out.Write('{EXPECTED_TEXT}')"],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        capture_target_stdout=True,
    )

    exited_payload = {}
    cleaned_payload = {}
    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_exited(payload):
        exited_payload["payload"] = payload

    def on_cleaned_up(payload):
        cleaned_payload["payload"] = payload
        main_loop.call_soon_threadsafe(done.set)

    capture_handler = _CaptureHandler()
    sandbox_logger = logging.getLogger("wincage.sandbox")
    prior_level = sandbox_logger.level
    sandbox_logger.addHandler(capture_handler)
    sandbox_logger.setLevel(logging.ERROR)

    try:
        handle = wincage.launch(config)
        handle.on(wincage.SandboxEvent.EXITED, on_exited)
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

    failures = []

    exited = exited_payload.get("payload")
    if exited is None:
        failures.append("EXITED event never observed")
    elif exited.exit_code != 0:
        failures.append(f"unexpected exit_code={exited.exit_code!r} on EXITED payload")

    if cleaned_payload.get("payload") is None:
        failures.append("CLEANED_UP event never observed")

    if handle.target_output is None:
        failures.append("target_output stayed None; the exited-line json.loads likely failed")
    elif handle.target_output != EXPECTED_TEXT:
        failures.append(f"target_output={handle.target_output!r}, expected {EXPECTED_TEXT!r}")

    parse_errors = [r for r in capture_handler.records if "Failed to read target_output" in r]
    if parse_errors:
        failures.append(f"parse-failure logged: {parse_errors}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: exited-line handshake parsed cleanly, EXITED/CLEANED_UP payloads sane, no logged parse errors")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
