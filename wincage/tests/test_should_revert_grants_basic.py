"""
Launches with should_revert_grants=True and a broker_files grant. Confirms
the AppContainer SID's ACE is present via icacls immediately after launch
(while the process is still running), and confirmed absent via icacls after
CLEANED_UP fires.

Run from the repo root after `pip install -e .`:
    python test_should_revert_grants_basic.py
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import wincage

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
MONIKER = "wincage.test.revert_grants_basic"


def sid_present_in_acl(path: str) -> bool:
    result = subprocess.run(
        ["icacls", path], capture_output=True, text=True, check=False
    )
    return "S-1-15-2-" in result.stdout


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="wincage_revert_grants_basic_"))
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
        should_revert_grants=True,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    try:
        handle = wincage.launch(config)
        handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

        await asyncio.sleep(0.3)  # let provisioning land before checking icacls
        granted = sid_present_in_acl(str(granted_dir))
        print(f"ACE present immediately after launch: {granted}  (expected: True)")

        try:
            await asyncio.wait_for(done.wait(), timeout=20)
        except asyncio.TimeoutError:
            print("FAIL: never reached CLEANED_UP within 20s")
            await handle.terminate()
            return 1

        revoked = not sid_present_in_acl(str(granted_dir))
        print(f"ACE absent after CLEANED_UP: {revoked}  (expected: True)")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if granted and revoked:
        print("PASS: grant landed at launch, should_revert_grants=True removed it at CLEANED_UP")
        return 0

    print("FAIL: see details above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
