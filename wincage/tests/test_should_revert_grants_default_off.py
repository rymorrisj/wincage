"""
Launches with should_revert_grants left at its default (False) and a
broker_files grant. Confirms the ACE is NOT revoked after CLEANED_UP fires,
preserving existing/default behavior (grants persist on the path unless the
caller explicitly opts into should_revert_grants=True or calls
revoke_grants() itself).

This script calls wincage.revoke_grants() at the end to clean up the ACE it
left behind, since should_revert_grants=False means wincage itself will not.

Run from the repo root after `pip install -e .`:
    python test_should_revert_grants_default_off.py
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.revert_grants_default_off"


def sid_present_in_acl(path: str) -> bool:
    result = subprocess.run(
        ["icacls", path], capture_output=True, text=True, check=False
    )
    return "S-1-15-2-" in result.stdout


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="wincage_revert_grants_default_off_"))
    granted_dir = root / "granted"
    granted_dir.mkdir()
    (granted_dir / "file.txt").write_text("grant target")

    broker_files = [wincage.BrokerFile(path=str(granted_dir), access="rw", mode="grant")]

    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=PS,
        args=["-NoProfile", "-Command", "Start-Sleep -Seconds 2"],
        working_dir=str(granted_dir),
        broker_files=broker_files,
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
        # should_revert_grants intentionally left at its default (False).
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    passed = False
    try:
        handle = wincage.launch(config)

        if handle.should_revert_grants is not False:
            print(f"FAIL: handle.should_revert_grants is {handle.should_revert_grants!r}, expected False")
            return 1

        handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        except asyncio.TimeoutError:
            print("FAIL: never reached CLEANED_UP within 20s")
            await handle.terminate()
            return 1

        still_granted = sid_present_in_acl(str(granted_dir))
        print(f"ACE still present after CLEANED_UP: {still_granted}  (expected: True, default is not to revert)")

        passed = still_granted
        if passed:
            print("PASS: grant persisted past CLEANED_UP with should_revert_grants left at its default")
        else:
            print("FAIL: ACE was removed even though should_revert_grants was never set to True")
    finally:
        # Clean up the grant this script made, since should_revert_grants=False
        # means wincage itself never will.
        try:
            wincage.revoke_grants(MONIKER, broker_files)
            print("Cleanup: revoke_grants() called to remove the leftover ACE")
        except wincage.SandboxError as exc:
            print(f"Cleanup warning: revoke_grants() failed: {exc}")
        shutil.rmtree(root, ignore_errors=True)

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
