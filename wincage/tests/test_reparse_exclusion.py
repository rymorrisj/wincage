"""
Test for the reparse-point ACL exclusion fix in grant_directory().

Sets up: a directory to grant, containing a junction that points OUTSIDE
the granted tree, to a separate directory. Grants the outer directory with
mode="grant" (recursive), then confirms the AppContainer SID's ACE landed
on the granted directory and its real contents, but did NOT propagate
through the junction onto the outside target.

Requires: mklink /J (directory junctions), which does not need admin
or Developer Mode, unlike symlinks.

Run from the repo root after `pip install -e .`:
    python test_reparse_exclusion.py
"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import wincage

MONIKER = "wincage.test.reparse_exclusion"


def sid_present_in_acl(path: str) -> bool:
    result = subprocess.run(
        ["icacls", path], capture_output=True, text=True, check=False
    )
    return "S-1-15-2-" in result.stdout


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="wincage_reparse_test_"))
    granted_dir = root / "granted"
    outside_dir = root / "outside_target"
    junction_path = granted_dir / "link_to_outside"

    granted_dir.mkdir()
    outside_dir.mkdir()
    (granted_dir / "real_file.txt").write_text("inside the granted tree")
    (outside_dir / "marker.txt").write_text("should never get an ACE")

    print(f"Granted directory: {granted_dir}")
    print(f"Outside directory (junction target): {outside_dir}")
    print(f"Junction: {junction_path} -> {outside_dir}\n")

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_path), str(outside_dir)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"FAIL: could not create junction: {result.stderr or result.stdout}")
        return
    print("Junction created.\n")

    config = wincage.SandboxConfig(
        moniker=MONIKER,
        exe_path=r"C:\Windows\System32\cmd.exe",
        args=["/c", "exit 0"],
        working_dir=str(granted_dir),
        broker_files=[
            wincage.BrokerFile(path=str(granted_dir), access="rw", mode="grant"),
        ],
        cpu_max_rate=80,
        cpu_min_rate=5,
        memory_limit_mb=256,
    )

    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_cleaned_up(payload):
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)
    await asyncio.wait_for(done.wait(), timeout=30)

    granted_has_ace = sid_present_in_acl(str(granted_dir))
    real_file_has_ace = sid_present_in_acl(str(granted_dir / "real_file.txt"))
    outside_has_ace = sid_present_in_acl(str(outside_dir))
    marker_has_ace = sid_present_in_acl(str(outside_dir / "marker.txt"))

    print("---- Results ----")
    print(f"Granted directory itself has ACE:        {granted_has_ace}  (expected: True)")
    print(f"Real file inside granted tree has ACE:   {real_file_has_ace}  (expected: True)")
    print(f"Outside directory (junction target) ACE: {outside_has_ace}  (expected: False)")
    print(f"Marker file past the junction has ACE:   {marker_has_ace}  (expected: False)")

    passed = granted_has_ace and real_file_has_ace and not outside_has_ace and not marker_has_ace
    if passed:
        print("\nPASS: grant propagated inside the granted tree, correctly stopped at the junction.")
    else:
        print("\nFAIL: check output above.")
        if outside_has_ace or marker_has_ace:
            print("  The ACE crossed the junction boundary, reparse-point exclusion did not work.")
        if not granted_has_ace or not real_file_has_ace:
            print("  The grant didn't even land on the intended tree, check that separately.")

    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
