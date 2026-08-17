# wincage

[![Windows Only](https://img.shields.io/badge/platform-Windows--10%20%2F%2011-blue.svg)](https://microsoft.com/windows)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Runtime Dependencies: Zero](https://img.shields.io/badge/runtime%20deps-zero-brightgreen.svg)]()
[![Native build: MSYS2 UCRT64](https://img.shields.io/badge/native%20build-MSYS2%20UCRT64-purple.svg)](https://github.com/msys2)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

wincage gives a host a control layer with hard resource limits and process isolation. GPU and audio access stay intact. Originally built as small scripts inside [Peach 1UP](https://github.com/rymorrisj/peach_1up) I extracted it out, and generalized it so as to be a more general tool for others.

Windows process sandboxing. Runs an executable inside an AppContainer with a Job Object applying CPU/memory limits. Includes a diagnostic package: a baseline check that core confinement works at all, plus optional probes for whether a machine's AppContainer allows the graphics/audio API stacks a sandboxed process needs.

[Peach 1UP](https://github.com/rymorrisj/peach_1up) runs a wide variety of third-party emulator software, and that software doesn't always behave. Hanging processes, memory leaks, and runaway CPU usage are common failure modes with binaries you didn't write and can't patch.

| Package | Purpose |
|---|---|
| `wincage` | Core sandbox. `launch(config)` provisions a per-moniker AppContainer, grants `broker_files` access, starts the target suspended under a Job Object, resumes it, returns a `SandboxHandle` with event callbacks (`STARTED`/`EXITED`/`ERROR`/`CLEANED_UP`). `reset_container(moniker)` deletes a provisioned profile. `revoke_grants(moniker, broker_files)` reverses a prior grant. No GPU or windowing dependency. |
| `wincage.checker` | Nested diagnostic subpackage. `run_baseline_checks()` confirms core confinement (AppContainer, Job Object limits, launch/terminate, OS version) works on the host. `run_gpu_checks()` runs disposable D3D11/OpenGL/Qt probes inside a throwaway AppContainer, only relevant if your own workload does GPU rendering or opens windows. `run_checks()` always runs the baseline first, then the GPU probes too if `include_gpu_checks=True`. Launches its probes through `wincage.launch()` itself, so it ships nested rather than standalone. |

## Requirements

`wincage` core has no GPU or hardware dependency. It works on any host meeting the OS and Python requirements below. A build toolchain (MSYS2 UCRT64/GCC) is only needed if you're building from source; `pip install wincage` ships prebuilt binaries and needs nothing beyond Python. See [Install / build](#install--build) for both paths.

Confirmed on Windows 10 and 11. The Job Object CPU rate control APIs wincage uses are documented as
available since Windows 8, so wincage may work there too, but that is unconfirmed: this project has
never tested or run on Windows 8/8.1.

## Two isolation modes

- **AppContainer + Job Object** (`launch()`), the default. Full security isolation plus resource limits. Use this unless a target process needs raw device I/O.
- **Job Object only** (`launch_suspended()` / `run_under_job()`), a fallback. Resource limits without AppContainer confinement. Use it for processes that call `DeviceIoControl` against a raw device handle, since that fails under AppContainer or any situation in which you do not/cannot use AppContainer but still need resource limits.

## Architecture

AppContainer and Job Object solve different problems, both needed:

- **Job Object**, resource limits only (CPU rate, memory cap, kill-on-close). No security containment.
- **AppContainer**, confines the process to a derived SID without changing identity. Audio session, GPU adapter, and window station access keep working. Under a separate low-privilege account, all three would fail silently instead. This is regular AppContainer, not LPAC. LPAC strips `ALL APPLICATION PACKAGES`, which breaks OpenGL ICD loading from DriverStore.

Provisioning runs in a separate native process, `sandbox_host.exe`, not in-process via `ctypes`. This is a crash-fault boundary, not a style choice. A hard fault in native AppContainer/Job Object code crashes whatever process runs it. Windows 11 places nearly every process in a job by default, so an in-process crash would take the host down and cascade-kill everything else it launched via `KILL_ON_JOB_CLOSE`. As a separate child process, the same crash costs one failed launch instead.

## Install / build

Two ways to get wincage, for two different purposes.

### Install with pip (prebuilt binaries)

```sh
pip install wincage
```

The wheel ships `sandbox_host.exe`, the checker's GPU probe binaries and DLLs
(`test_sdl2_d3d11.exe`, `test_sdl2_opengl.exe`, `SDL2.dll`,
`libwinpthread-1.dll`), and the `scripts/*.ps1` diagnostics. No
MSYS2, no GCC, nothing beyond Python is needed if you just need wincage as is.

### Build from source (MSYS2 UCRT64)

Only needed if you're modifying the C++ source, or building from a git
checkout instead of a released wheel.

Requires GCC from MSYS2 UCRT64:

```sh
pacman -S mingw-w64-ucrt-x86_64-gcc \
          mingw-w64-ucrt-x86_64-SDL2 \
          mingw-w64-ucrt-x86_64-pkg-config \
          mingw-w64-ucrt-x86_64-qt5-base   # optional, for the qt_qpa check
```

Build the sandbox host from an MSYS2 UCRT64 terminal:

```sh
bash wincage/build.sh                       # generates wincage/sandbox_host.exe
OUT_NAME=myhost.exe bash wincage/build.sh   # custom output name
```

Build the checker's capability probes separately if you need GPU checks:

```sh
bash wincage/checker/src/build_tests.sh
```

Outputs `test_sdl2_d3d11.exe`, `test_sdl2_opengl.exe`, and (if Qt is available) `test_qt_qpa.exe` into `wincage/checker/src/`. Neither binary set is committed to git; building from a git checkout means building both yourself. (A released wheel from `pip install wincage` already includes them prebuilt, see above.)

Install the Python package from the checkout:

```sh
pip install -e .
```

`wincage` itself has zero non-stdlib runtime dependencies (`ctypes`, `asyncio`, `pathlib`, `typing`, `dataclasses`, `enum`, `json`, `os`, `subprocess`, `threading`, `logging` only).

## Usage

```python
import wincage

config = wincage.SandboxConfig(
    moniker="myapp.worker",       # stable identifier, reused across launches
    exe_path="C:/apps/worker.exe",
    args=["--headless"],
    working_dir="C:/data/jobs",
    broker_files=[
        wincage.BrokerFile(path="C:/data/jobs", access="rw", mode="grant"),
    ],
    cpu_max_rate=60,
    cpu_min_rate=5,
    skip_cpu_limit=False,          # True skips the CPU rate limit entirely; memory_limit_mb still applies
    memory_limit_mb=512,
    breakaway=False,                # True passes CREATE_BREAKAWAY_FROM_JOB, for targets whose parent job forbids a second job assignment
)

handle = wincage.launch(config)   # synchronous; raises SandboxError on failure

handle.on(wincage.SandboxEvent.EXITED, lambda p: print(f"exited: {p.exit_code}"))
handle.on(wincage.SandboxEvent.ERROR,  lambda p: print(f"error: {p.error}"))

await handle.terminate()  # resolves when CLEANED_UP fires

wincage.reset_container("myapp.worker")  # delete a container profile later
```

`revoke_grants()` reverses a prior grant. It's an independent, stateless call:
pass it the same `broker_files` list you originally granted with, whenever
you decide the access is no longer needed. It doesn't wait for or care about
`terminate()`/`reset_container()`, and it's fine to call while a process is
still running under that moniker; whether that's safe to do is on the
caller to judge, not wincage.

```python
wincage.revoke_grants(
    "myapp.worker",
    broker_files=[
        wincage.BrokerFile(path="C:/data/jobs", access="rw", mode="grant"),
    ],
)
```

`launch()` returns a `SandboxHandle` once the child process starts. `STARTED` has already happened by the time you get the handle back, so registering a `STARTED` callback replays it immediately and synchronously. `EXITED`, `ERROR`, and `CLEANED_UP` fire later from the watcher's asyncio task. Call `launch()` from within a running event loop, or register callbacks any time before the event fires.

Run the baseline checker to confirm core confinement works on this host, no GPU/UI probes involved:

```python
from wincage.checker import run_baseline_checks, CheckStatus

for r in run_baseline_checks():
    print(f"{r.name}: {r.status.value} - {r.message}")
```

Add the GPU/audio capability probes too, before relying on the sandbox for a specific graphics/audio API:

```python
from wincage.checker import run_checks, CheckStatus

for r in run_checks(include_gpu_checks=True):
    print(f"{r.name}: {r.status.value} - {r.message}")
    if r.status == CheckStatus.FAIL:
        print(f"  affects: {', '.join(r.affects)}")
```

`run_checks()` always runs `run_baseline_checks()` first. `run_gpu_checks()` only follows if
`include_gpu_checks=True` (default `False`) and no baseline result is a `FAIL`. Call
`run_gpu_checks()` directly if you want the GPU/UI probes. Each returns `list[CheckResult]` with `status` one of `PASS`, `FAIL`, `SKIP`, or `UNCONFIRMED`. `SKIP` means a GPU probe binary isn't built yet. These functions always return one of those values and do not throw on error.

`UNCONFIRMED` means a baseline check (currently just the OS version floor) passed on a configuration this project hasn't verified, not a failure. `affects` is empty by default for GPU checks and always empty for baseline checks, since a baseline failure affects everything downstream. Pass your own `affects={"sdl2_d3d11": [...], ...}` mapping to `run_checks()`/`run_gpu_checks()` if you want each GPU result to carry which of your components a failure impacts.

### Runtime diagnostic scripts

`wincage/scripts/` checks live instances of wincage running on a host. `Test-AppContainerStatus.ps1` and `Test-JobObjectStatus.ps1` both take `-Moniker`, matching `SandboxConfig.moniker`, or `-ProcessId` directly. See `Get-Help .\Test-AppContainerStatus.ps1 -Full` for what each check can and can't prove.

Use these to confirm that your use of wincage is working as expected.

**`Test-AppContainerStatus.ps1`**

<details>
<summary><b>Click to view example output</b></summary>

```
PS> .\Test-AppContainerStatus.ps1 -Moniker "Peach1UP.duckstation.shared"
Searching running processes for moniker 'Peach1UP.duckstation.shared' (expected SID S-1-15-2-2176738053-2059683841-676532045-2718669675-2683758908-3894446449-788966096)...
Found 1 process(es) confirmed under moniker 'Peach1UP.duckstation.shared' (skipped 120 inaccessible process(es)):
  PID Name                           Path
  --- ----                           ----
26292 duckstation-qt-x64-ReleaseLTCG C:\Path\peach_1up\emulators\duckstat...

PS> .\Test-AppContainerStatus.ps1 -Moniker "Peach1UP.xenia.shared"
Searching running processes for moniker 'Peach1UP.xenia.shared' (expected SID S-1-15-2-...)...
No running process is confined under moniker 'Peach1UP.xenia.shared' (skipped 121 inaccessible process(es)).
If the target process runs under another user session, re-run this script elevated to include it in the search.
```
</details>

**`Test-JobObjectStatus.ps1`**

<details>
<summary><b>Click to view example output</b></summary>

```
PS> .\Test-JobObjectStatus.ps1 -Moniker "Peach1UP.duckstation.shared"
Searching running processes for moniker 'Peach1UP.duckstation.shared'...
Found 1 process(es) confirmed under this moniker's AppContainer:
  PID Name                           Path
  --- ----                           ----
26292 duckstation-qt-x64-ReleaseLTCG C:\Path\peach_1up\emulators\duckstat...
Job Object status for PID 26292:
  IsProcessInJob: True
WARNING: This only confirms the process is in SOME Job Object, not specifically
the sandbox package's own resource-limiting job. On Windows 11, nearly every
process is pre-assigned to an OS-managed job by default, so True does not prove
the app's CPU/memory limits are actually active. The container path's Job Object
is created unnamed, so it cannot be looked up directly by moniker.
For a definitive answer, check Sysinternals Process Explorer:
  select the process > Properties > Job tab > view the actual configured limits.

PS> .\Test-JobObjectStatus.ps1 -Moniker "Peach1UP.duckstation.shared" -ProcessId 26292
Checking Job Object status for PID 26292 (duckstation-qt-x64-ReleaseLTCG)...
IsProcessInJob: True
WARNING: [same caveat as above]
```
</details>

## Public API

### `wincage`

| Export | What it is |
|---|---|
| `launch(config)` | Provisions the AppContainer, starts the process, returns a `SandboxHandle`. |
| `reset_container(moniker, broker_files=None)` | Deletes a provisioned AppContainer profile. Pass the same `broker_files` list you granted with to also revoke its DACL ACEs before deletion. |
| `revoke_grants(moniker, broker_files)` | Removes the container SID's DACL ACEs for `broker_files`, the mirror of what `launch()` grants. Stateless and independent of container/process lifecycle. |
| `SandboxConfig` | Launch parameters: moniker, exe_path, broker_files, resource limits. |
| `BrokerFile` | One file/directory grant entry for `SandboxConfig.broker_files`. |
| `SandboxHandle` | Returned by `launch()`; `.on(event, callback)`, `.terminate()`. |
| `SandboxEvent` | `STARTED`, `EXITED`, `ERROR`, `CLEANED_UP`. |
| `SandboxPayload` | Passed to event callbacks. |
| `SandboxStage` | Identifies which launch/teardown stage an error or payload refers to. |
| `SandboxError` | Raised by `launch()`/`reset_container()`; carries `.stage` and `.suggestions`. |
| `EXE_NAME` | Host executable name `launch()` spawns (default `"sandbox_host.exe"`). |
| `set_exe_name(value)` | Changes `EXE_NAME` before the first `launch()` call, e.g. `wincage.set_exe_name("myhost.exe")`. |
| `launch_suspended(exe, args, flags, ...)` | Starts a process suspended (native or AppContainer), returns a `SandboxProcess`. |
| `run_under_job(executable_path, ..., process, job_name, ...)` | Assigns a suspended `SandboxProcess` to a new Job Object, resumes it, returns `(SandboxProcess, WindowsJobObject)`. |
| `SandboxProcess` | Process handle from `launch_suspended()`/`run_under_job()`; `.poll()`, `.terminate()`, `.kill()`, `.wait()`, `.resume()`. |
| `WindowsJobObject` | Job Object wrapper from `run_under_job()`; `.set_memory_limit()`, `.set_cpu_limit()`, `.teardown()`, `.close()`. |

`BrokerFile.access` values:

| Access | Grants | Use for |
|---|---|---|
| `"r"` | `FILE_GENERIC_READ` | Opening specific files whose names you already know. |
| `"rw"` | `FILE_GENERIC_READ` + `FILE_GENERIC_WRITE` | Same, plus writing those files. |
| `"x"` | `FILE_TRAVERSE` | Passing through a directory to reach a path below it, without listing its contents. |
| `"rx"` | `FILE_GENERIC_READ` + `FILE_TRAVERSE` | Listing a directory's contents. `"r"` alone can't: Windows denies a directory enumeration request unless traverse is granted too. |

`BrokerFile.mode` values:

| Mode | Effect | Use for |
|---|---|---|
| `"secure"` | Sets a DACL ACE on the existing file/directory for the container SID, no propagation. | A single existing file. |
| `"grant"` | Sets a DACL ACE and propagates it across the existing tree under `path`. | A directory the target needs to browse or write into. |
| `"inherit"` | Never touches the DACL. The host opens `path` with an inheritable handle and passes its value to the child as the environment variable `SANDBOX_HANDLE_<i>`, where `<i>` is that entry's index in `broker_files`. The child reads `SANDBOX_HANDLE_<i>` and casts it back to a `HANDLE` to use the file the host already opened. | A single file, when you'd rather not touch the filesystem ACL at all. |

### `wincage.checker`

| Export | What it is |
|---|---|
| `run_checks(moniker_prefix=..., affects=..., include_gpu_checks=False)` | Runs `run_baseline_checks()`, then `run_gpu_checks()` too if `include_gpu_checks=True` and the baseline didn't `FAIL`. Never raises for a per-check failure. |
| `run_baseline_checks(moniker_prefix=...)` | Runs the OS version, AppContainer, Job Object, and launch/terminate baseline checks. No GPU/windowing dependency. Never raises for a per-check failure. |
| `run_gpu_checks(moniker_prefix=..., affects=...)` | Runs the D3D11/OpenGL/Qt capability probes. Never raises for a per-probe failure. |
| `CheckResult` | `name`, `status`, `message`, `affects`. |
| `CheckStatus` | `PASS`, `FAIL`, `SKIP`, `UNCONFIRMED`. |
| `DEFAULT_MONIKER_PREFIX` | Default AppContainer moniker prefix used when `moniker_prefix` isn't passed. |

## Package layout

```
wincage/                  # sandbox core
├── __init__.py           # public exports
├── sandbox.py            # launch() / reset_container(), talks to sandbox_host.exe
├── sandbox_config.py     # SandboxConfig, BrokerFile
├── sandbox_error.py      # SandboxError
├── sandbox_event.py      # SandboxEvent, SandboxStage, SandboxPayload
├── job.py                # WindowsJobObject (native launch path)
├── process.py            # launch_suspended() / run_under_job() (native launch path)
├── sandbox_process.py    # SandboxProcess handle (native launch path)
├── win32_types.py        # ctypes structures/constants, Win32 interop only
├── build.sh              # builds sandbox_host.exe
├── src/                  # sandbox_host.exe C++ source
├── scripts/               # runtime diagnostic PowerShell scripts
├── tests/                 # regression test suite (run_tests.py)
└── checker/               # nested diagnostic subpackage
    ├── __init__.py       # public exports
    ├── checker.py        # run_checks() / run_baseline_checks() / run_gpu_checks()
    ├── results.py        # CheckResult, CheckStatus
    └── src/               # capability-probe test programs + build_tests.sh
```

## Tests, diagnostics, and checks

This repo has three separate things that look like "tests":

- `wincage/tests/`: the actual regression suite, started as manual verification scripts used
  during development that became the current test suite. Run `python wincage/tests/run_tests.py`
  after touching `sandbox.py`, `sandbox_config.py`, `main.cpp`, or `checker.py`; it runs every
  `test_*.py` script in the directory and prints a pass/fail/skip line for each.
  `test_job_inspect.py` runs separately by hand (use Process Explorer or similar to inspect the Job).
- `wincage/scripts/*.ps1`: diagnostic tools for a live process, not a test suite. Use them to check
  AppContainer/Job Object isolation on something already running. See [Runtime diagnostic
  scripts](#runtime-diagnostic-scripts).
- `wincage.checker.run_baseline_checks()`: checks whether core confinement (AppContainer, Job Object
  limits, launch/terminate) and the OS version work on this host at all. No GPU dependency.
- `wincage.checker.run_gpu_checks()`: checks whether D3D11/OpenGL/Qt survive confinement on your
  machine. Run it (or `run_checks(include_gpu_checks=True)`) before shipping something that needs
  GPU/UI access under wincage.

## Known limitations

- **DACL grants persist on the path.** `mode="grant"`/`"secure"` modify the filesystem ACL and don't revert on exit. `grant` also propagates its ACE across the existing tree. Broker only what's needed, and prefer `secure`/`inherit` over `grant` for a single file. Set `should_revert_grants=True` on `SandboxConfig` to have grants revoked automatically at cleanup, call `revoke_grants(moniker, broker_files)` yourself with the same list you granted with, or pass that same list to `reset_container(moniker, broker_files)` since deleting the profile alone doesn't revoke its ACEs either.
- **A crash mid-grant can leave a stray marker file.** `grant_directory`/`revoke_directory` write a `:wincage.pending` marker before it starts and removes it when done. If the process is killed partway the marker is left behind. It's harmless, the only effect is that the next grant or revoke on that folder redoes the full check instead of trusting the previous one.
- **That marker doesn't work on FAT32/exFAT drives.** It relies on an NTFS/ReFS-only feature. On FAT32 or exFAT, the marker silently fails to write, so a crash mid-grant won't be detected or corrected automatically. NTFS/ReFS are the filesystems Windows uses for its main C drive
- **`broker_files` paths are capped at MAX_PATH (260 characters).** Grant/secure/inherit all reach the file through the plain Win32 file APIs, no `\\?\` long-path prefix. A longer path fails the grant outright.
- **Low level device I/O is not sandboxed.** AppContainer blocks `DeviceIoControl` by default, and wincage doesn't currently do the extra work needed to allow it. Use `launch_suspended()`/`run_under_job()`, the native path, instead for these processes.
- **AppContainer confinement grants zero capabilities.** wincage never populates `SECURITY_CAPABILITIES.Capabilities`/`CapabilityCount`, so every confined process gets Windows default/deny result for named capabilities: 
  - no network access (no `internetClient`)
  - no device/sensor access (camera, microphone, GPS, cellular)
  
  Any target process that needs these will fail under confinement, this is Windows' own AppContainer default/deny behavior, not a wincage bug. There's currently no `SandboxConfig` option to request specific capabilities. If your workload needs network or device access, don't enable AppContainer confinement for it, Job Object limits alone remain available without this restriction. Looking into adding support for this in the backlog below
- **The Qt/QPA confinement probe (`qt_qpa`) is currently disabled.** `build_tests.sh` no longer builds `test_qt_qpa.exe`, and `run_gpu_checks()` reports it as `SKIP`. Building it produced a binary with an unresolved runtime DLL dependency (`STATUS_DLL_NOT_FOUND`) that persisted across multiple packaging approaches, including manual dependency copying and `windeployqt-qt5.exe`. This does not affect wincage's core functionality or the SDL2-based GPU checks (`sdl2_d3d11`, `sdl2_opengl`), which work correctly. Tracked as a backlog item below.

## Backlog

- Capability requests: AppContainer grants zero capabilities by default (no network, no device/sensor access), and there is currently no `SandboxConfig` option to request specific capabilities for a confined process.
- `revoke_grants()` only logs a warning on a no-op revoke (moniker matches nothing). Could return a structured result instead of/alongside logging, so a caller can detect this programmatically.
- `qt_qpa` probe: re-enable once `test_qt_qpa.exe`'s unresolved runtime DLL dependency (`STATUS_DLL_NOT_FOUND`) is fixed. See Known Limitations above.

## Security disclaimer

Read this before relying on `wincage` as a hard security control layer

- AppContainer plus a Job Object reduces what a launched process can reach. **It does not eliminate risk.**
- A process started through `launch()` still runs as the launching user's identity. It still has whatever `broker_files` paths you granted it. It still shares the desktop, window station, audio session, and GPU with everything else that user runs. Its access to that shared desktop and window station is narrowed to creating and drawing its own window and receiving its own input, not the hooks, clipboard, or screen-capture access that would let it interfere with other apps on it.
- **This is not a complete sandbox against malicious or untrusted code.** It's built for code you already trust: your own executables, a plugin or worker whose failure mode is a bug rather than an attack, or a third-party tool you're limiting for robustness. It has had an internal logic/pattern review (ACL over-grant paths, desktop/window-station access scope, PID reuse, JSON parser input handling), not a penetration test or formal security assessment. It also was never designed as a containment boundary for hostile code: no adversarial threat model, no fuzzing.
- `run_baseline_checks()` tells you whether core confinement works at all; `run_gpu_checks()` tells you whether a specific API stack survives confinement on a given machine. Neither tells you confinement is sufficient for your threat model.
- For a hard security boundary against actively distrusted code, use a VM or dedicated hardware isolation instead.

## Contributing

Open an issue for bugs or platform-compatibility gaps. Changes to the AppContainer/Job Object provisioning code (`sandbox_host.exe`, `src/`) should include the reasoning behind any new native Win32 call.

I will support this package for a time but my primary focus is [Peach 1UP](https://github.com/rymorrisj/peach_1up). Would love any help or consideration others would like to give if they find this useful. 

## License

MIT. See [LICENSE](LICENSE)