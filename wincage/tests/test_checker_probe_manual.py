"""
Runs one real checker probe (test_sdl2_opengl.exe) and confirms it completes
without hanging and reports a real CheckResult. Exercises the
should_revert_grants/timeout/mtime fixes in wincage/checker/checker.py
together, in one real launch through wincage.launch().

wincage.checker.run_checks() always runs every probe in _CHECKS with no way
to select just one, so this imports checker.py's private _run_one() directly,
the "equivalent direct call" for a single probe. Not a reason to touch
checker.py itself.

Requires test_sdl2_opengl.exe to already be built, from an MSYS2 UCRT64
terminal (a bare "bash" from a plain PowerShell/cmd prompt can hit Windows'
WSL bash.exe shim instead of MSYS2's):
    bash wincage/checker/src/build_tests.sh

See README for details

Run from the repo root after `pip install -e .`:
    python test_checker_probe_manual.py
"""

import sys
import threading
import time

from wincage.checker.checker import _run_one
from wincage.checker.results import CheckResult, CheckStatus

MONIKER_PREFIX = "wincage.test.checker_probe_manual"
CHECK_NAME = "sdl2_opengl"
EXE_NAME = "test_sdl2_opengl.exe"
PASS_MESSAGE = "OpenGL 4.5 core context created via WGL inside AppContainer"

# checker.py's own probe-wait ceiling is 30s; give real slack above that
# before this script itself declares a hang.
_HANG_TIMEOUT_SECONDS = 45


def main() -> int:
    result_holder: dict[str, CheckResult] = {}
    error_holder: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_holder["result"] = _run_one(
                CHECK_NAME, EXE_NAME, PASS_MESSAGE, [], MONIKER_PREFIX,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            error_holder["error"] = exc

    print(f"Running probe '{CHECK_NAME}' ({EXE_NAME}) via checker._run_one()...")
    t0 = time.monotonic()
    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout=_HANG_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - t0

    if worker.is_alive():
        print(f"FAIL: probe did not complete within {_HANG_TIMEOUT_SECONDS}s, appears hung")
        return 1

    print(f"Probe run finished in {elapsed:.1f}s")

    if "error" in error_holder:
        print(f"FAIL: _run_one() raised an exception: {error_holder['error']!r}")
        return 1

    result = result_holder.get("result")
    if not isinstance(result, CheckResult):
        print(f"FAIL: did not get back a real CheckResult, got {result!r}")
        return 1

    print(f"CheckResult: name={result.name!r} status={result.status.value} message={result.message!r}")

    if result.status == CheckStatus.SKIP:
        print("PASS: probe run completed without hanging (SKIP: binary not built or stale, see message above)")
        return 0

    if result.status == CheckStatus.FAIL:
        print("PASS: probe run completed without hanging and returned a real CheckResult "
              "(status=FAIL is a machine-capability result, not a test failure; see message above)")
        return 0

    print("PASS: probe run completed without hanging and returned a real CheckResult (status=PASS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
