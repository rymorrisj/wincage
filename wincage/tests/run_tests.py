"""
Runs every test_*.py script in this directory as a subprocess,
prints one pass/fail/skip line per script, then a final summary line.

Python, no third-party test framework, matching the style of the
scripts it runs.

Run from the repo root after `pip install -e .`:
    python wincage/tests/run_tests.py
"""

import subprocess
import sys
import time
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]

# If this doesn't pass, every other script's result
# is noise, not signal, so the rest are marked SKIPPED instead of run.
_GATING_TEST = "test_capture_basic.py"

#  Run it by hand; see its own docstring. 
# Process Explorer within a live 60-second window and always exits 0 regardless
_INTERACTIVE_TESTS = "test_job_inspect.py"

_PER_TEST_TIMEOUT_SECONDS = 120


def _discover() -> list[str]:
    on_disk = set({p.name for p in _TESTS_DIR.glob("test_*.py")})
    on_disk.add(_INTERACTIVE_TESTS)
    return on_disk


def _run_one(name: str) -> tuple[bool, float, str]:
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(_TESTS_DIR / name)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=_PER_TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return False, elapsed, f"did not finish within {_PER_TEST_TIMEOUT_SECONDS}s, appears hung"

    elapsed = time.monotonic() - t0
    if result.returncode == 0:
        return True, elapsed, ""

    tail_lines = (result.stdout + result.stderr).strip().splitlines()[-10:]
    detail = "\n".join(f"      {line}" for line in tail_lines)
    return False, elapsed, f"exit code {result.returncode}\n{detail}"


def main() -> int:
    names = _discover()

    passed = 0
    failed = 0
    skipped = 0
    gating_failed = False

    for name in names:
        if name in _INTERACTIVE_TESTS:
            print(f"[SKIP] {name} - interactive/manual verification only, run separately")
            skipped += 1
            continue

        if gating_failed:
            print(f"[SKIP] {name} - gating test '{_GATING_TEST}' failed, result would be noise")
            skipped += 1
            continue

        ok, elapsed, detail = _run_one(name)
        if ok:
            print(f"[PASS] {name} ({elapsed:.1f}s)")
            passed += 1
        else:
            print(f"[FAIL] {name} ({elapsed:.1f}s) - {detail}")
            failed += 1
            if name == _GATING_TEST:
                gating_failed = True

    total_run = passed + failed
    print(f"\n{passed}/{total_run} passed ({skipped} skipped)")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
