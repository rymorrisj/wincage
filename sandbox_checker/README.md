# sandbox_checker

Diagnostic tool for Peach 1UP that verifies AppContainer + Job Object compatibility
on the host system before enabling the sandbox for custom emulators.

## What it does

Runs three short test programs inside a real AppContainer (via the `sandbox` package)
and reports structured results. Each check confirms that a specific graphics or audio
API stack remains accessible when the process is confined by AppContainer security.

| Check | What it tests | Affected emulators |
|---|---|---|
| `sdl2_d3d11` | SDL2 video init, WASAPI audio, D3D11 hardware device (non-WARP adapter) | dosbox, pcsx2, duckstation |
| `sdl2_opengl` | OpenGL 4.5 core context via WGL | dosbox, mame, mupen64plus, retroarch |
| `qt_qpa` | Qt 5.15 QPA platform plugin load + window display | pcsx2, rpcs3, dolphin |

## Why it exists

AppContainer confines processes using a derived SID. Most Win32 APIs remain accessible,
but certain GPU paths, audio endpoints, and Qt platform plugin lookups can silently
fail if the DriverStore or COM server is gated by token capabilities. This tool
confirms those paths work before sandbox is enabled for an emulator profile.

A FAIL does not mean the emulator is broken. It means the API in question cannot be
reached from inside an AppContainer on this system. Disable sandbox for the affected
emulators rather than investigating why (most causes are DriverStore ACLs or
system-specific security policy).

## Build the test executables

Requires MSYS2 UCRT64 with GCC, SDL2, and optionally Qt5:

```sh
pacman -S mingw-w64-ucrt-x86_64-gcc \
          mingw-w64-ucrt-x86_64-SDL2 \
          mingw-w64-ucrt-x86_64-pkg-config \
          mingw-w64-ucrt-x86_64-qt5-base   # optional, for qt_qpa check
```

From an MSYS2 UCRT64 terminal:

```sh
bash src/build_tests.sh
```

Outputs `test_sdl2_d3d11.exe`, `test_sdl2_opengl.exe`, and (if Qt available)
`test_qt_qpa.exe` into `src/`.

`sandbox_host.exe` from the `sandbox` package must also be built before running
checks. See `../sandbox/README.md`.

## Run the checks

```python
from backend.service.utils.platform.windows.sandbox_checker import run_checks, CheckStatus

results = run_checks()

for r in results:
    print(f"{r.name}: {r.status.value}")
    print(f"  {r.message}")
    if r.status == CheckStatus.FAIL:
        print(f"  Disable sandbox for: {', '.join(r.affects)}")
```

`run_checks()` never raises. It returns a `list[CheckResult]` regardless of outcome.

## Result types

| Status | Meaning |
|---|---|
| `PASS` | The API stack works inside AppContainer on this system |
| `FAIL` | The API is blocked or missing; disable sandbox for affected emulators |
| `SKIP` | The test executable was not found — run `build_tests.sh` first |

A SKIP is not a failure. All checks return SKIP until `build_tests.sh` has been run.

## CheckResult fields

```python
@dataclass(frozen=True)
class CheckResult:
    name: str         # "sdl2_d3d11", "sdl2_opengl", "qt_qpa"
    status: CheckStatus
    message: str      # human-readable explanation of the outcome
    affects: list[str]  # emulator slugs affected by this check
```

## How each check works

Each test executable runs inside a minimal AppContainer (no DACL grants, default
CPU/memory limits) and exits 0 on success or 1 on any failure. The checker interprets
the exit code; the test program's stdout output is not captured.

The effective timeout per check is bounded by `sandbox`'s internal 15-second limit.
All three checks normally complete in under 5 seconds on any system with working GPU
drivers.
