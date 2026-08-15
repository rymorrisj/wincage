# wincage

[![Windows Only](https://img.shields.io/badge/platform-Windows--10%20%2F%2011-blue.svg)](https://microsoft.com/windows)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: Zero](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Peach 1UP](https://github.com/rymorrisj/peach_1up) runs a wide variety of third-party emulator software, and that software doesn't always behave. Hanging processes, memory leaks, and runaway CPU usage are common failure modes with binaries you didn't write and can't patch. wincage gives a host application a control layer over that: hard resource limits and process isolation. GPU and audio access stay intact, which those emulators need to work.

Windows process sandboxing. Runs an executable inside an AppContainer with a Job Object applying CPU/memory limits. Includes a diagnostic package that checks whether a machine's AppContainer allows the graphics/audio API stacks a sandboxed process needs.

| Package | Purpose |
|---|---|
| `wincage` | Core sandbox. `launch(config)` provisions a per-moniker AppContainer, grants `broker_files` access, starts the target suspended under a Job Object, resumes it, returns a `SandboxHandle` with event callbacks (`STARTED`/`EXITED`/`ERROR`/`CLEANED_UP`). `reset_container(moniker)` deletes a provisioned profile. `revoke_grants(moniker, broker_files)` reverses a prior grant. |
| `wincage.checker` | Nested diagnostic subpackage. `run_checks()` runs disposable probes inside a throwaway AppContainer and reports per-API-stack pass/fail. Launches its probes through `wincage.launch()` itself, so it ships nested rather than standalone. |

## Two isolation modes

- **AppContainer + Job Object** (`launch()`), the default. Full security isolation plus resource limits. Use this unless a target process needs raw device I/O.
- **Job Object only** (`launch_suspended()` / `run_under_job()`), a fallback. Resource limits without AppContainer confinement. Use it for processes that call `DeviceIoControl` against a raw device handle, since that fails under AppContainer.

## Architecture

AppContainer and Job Object solve different problems, both needed:

- **Job Object**, resource limits only (CPU rate, memory cap, kill-on-close). No security containment.
- **AppContainer**, confines the process to a derived SID without changing identity. Audio session, GPU adapter, and window station access keep working. Under a separate low-privilege account, all three would fail silently instead. This is regular AppContainer, not LPAC. LPAC strips `ALL APPLICATION PACKAGES`, which breaks OpenGL ICD loading from DriverStore.

Provisioning runs in a separate native process, `sandbox_host.exe`, not in-process via `ctypes`. This is a crash-fault boundary, not a style choice. A hard fault in native AppContainer/Job Object code crashes whatever process runs it. Windows 11 places nearly every process in a job by default, so an in-process crash would take the host down and cascade-kill everything else it launched via `KILL_ON_JOB_CLOSE`. As a separate child process, the same crash costs one failed launch instead.

## Install / build

Requires GCC from MSYS2 UCRT64:

```sh
pacman -S mingw-w64-ucrt-x86_64-gcc \
          mingw-w64-ucrt-x86_64-SDL2 \
          mingw-w64-ucrt-x86_64-pkg-config \
          mingw-w64-ucrt-x86_64-qt5-base   # optional, for the qt_qpa check
```

Build the sandbox host from an MSYS2 UCRT64 terminal:

```sh
bash wincage/build.sh                       # -> wincage/sandbox_host.exe
OUT_NAME=myhost.exe bash wincage/build.sh   # custom output name
```

Build the checker's capability probes separately:

```sh
bash wincage/checker/src/build_tests.sh
```

Outputs `test_sdl2_d3d11.exe`, `test_sdl2_opengl.exe`, and (if Qt is available) `test_qt_qpa.exe` into `wincage/checker/src/`. Neither binary set is committed. Both must be built before use.

Install the Python package:

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
    memory_limit_mb=512,
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

`launch()` returns a `SandboxHandle` once the child process starts. Callbacks fire from an asyncio task. Call `launch()` from within a running event loop, or register callbacks any time before the event fires.

Run the capability checker before relying on the sandbox for a specific graphics/audio API:

```python
from wincage.checker import run_checks, CheckStatus

for r in run_checks():
    print(f"{r.name}: {r.status.value} - {r.message}")
    if r.status == CheckStatus.FAIL:
        print(f"  affects: {', '.join(r.affects)}")
```

`run_checks()` never raises. It always returns `list[CheckResult]`, each `PASS`, `FAIL`, or `SKIP`. `SKIP` means probe binaries aren't built yet. `affects` is empty by default. Pass your own `affects={"sdl2_d3d11": [...], ...}` mapping if you want each result to carry which of your components a failure impacts.

### Runtime diagnostic scripts

`wincage/scripts/` answers a different question than the checker: not "will sandboxing work here" but "is this running process actually confined as expected." `Test-AppContainerStatus.ps1` and `Test-JobObjectStatus.ps1` both take `-Moniker`, matching `SandboxConfig.moniker`, or `-ProcessId` directly. See `Get-Help .\Test-AppContainerStatus.ps1 -Full` for what each check can and can't prove.

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

PS> .\Test-JobObjectStatus.ps1 -ProcessId 26292
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
| `reset_container(moniker)` | Deletes a provisioned AppContainer profile. |
| `revoke_grants(moniker, broker_files)` | Removes the container SID's DACL ACEs for `broker_files`, the mirror of what `launch()` grants. Stateless and independent of container/process lifecycle. |
| `SandboxConfig` | Launch parameters: moniker, exe_path, broker_files, resource limits. |
| `BrokerFile` | One file/directory grant entry for `SandboxConfig.broker_files`. |
| `SandboxHandle` | Returned by `launch()`; `.on(event, callback)`, `.terminate()`. |
| `SandboxEvent` | `STARTED`, `EXITED`, `ERROR`, `CLEANED_UP`. |
| `SandboxPayload` | Passed to event callbacks. |
| `SandboxStage` | Identifies which launch/teardown stage an error or payload refers to. |
| `SandboxError` | Raised by `launch()`/`reset_container()`; carries `.stage` and `.suggestions`. |
| `EXE_NAME` | Host executable name `launch()` spawns (default `"sandbox_host.exe"`); assignable before first call. |
| `launch_suspended(exe, args, flags, ...)` | Starts a process suspended (native or AppContainer), returns a `SandboxProcess`. |
| `run_under_job(executable_path, ..., process, job_name, ...)` | Assigns a suspended `SandboxProcess` to a new Job Object, resumes it, returns `(SandboxProcess, WindowsJobObject)`. |
| `SandboxProcess` | Process handle from `launch_suspended()`/`run_under_job()`; `.poll()`, `.terminate()`, `.kill()`, `.wait()`, `.resume()`. |
| `WindowsJobObject` | Job Object wrapper from `run_under_job()`; `.set_memory_limit()`, `.set_cpu_limit()`, `.teardown()`, `.close()`. |

### `wincage.checker`

| Export | What it is |
|---|---|
| `run_checks(moniker_prefix=..., affects=...)` | Runs every capability probe, never raises. |
| `CheckResult` | `name`, `status`, `message`, `affects`. |
| `CheckStatus` | `PASS`, `FAIL`, `SKIP`. |
| `DEFAULT_MONIKER_PREFIX` | Default AppContainer moniker prefix `run_checks()` uses when none is passed. |

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
└── checker/               # nested diagnostic subpackage
    ├── __init__.py       # public exports
    ├── checker.py        # run_checks()
    ├── results.py        # CheckResult, CheckStatus
    └── src/               # capability-probe test programs + build_tests.sh
```

## Known limitations

- **Windows only.** Both `sandbox_host.exe` and the Python wrapper's `ctypes.windll` calls require Win32 AppContainer/Job Object APIs. Import fails on non-Windows hosts.
- **DACL grants persist on the path.** `mode="grant"`/`"secure"` modify the filesystem ACL and don't revert on exit. `grant` also propagates its ACE across the existing tree. Broker only what's needed. Prefer `secure`/`inherit` over `grant` for a single file. Grants still don't auto-revert; call `revoke_grants(moniker, broker_files)` with the same list you granted with when you want the ACEs removed.
- **Container profiles are never auto-deleted.** A provisioned profile persists across launches and reboots by design. `reset_container()` removes one. ACEs already granted to a deleted profile's SID aren't cleaned up.
- **Qt's platform plugin fails under a memory cap.** It allocates a large heap at startup and aborts if the Job Object limit hits before the window appears. Pass `memory_limit_mb=None` for these processes. CPU limits still apply.
- **Raw device I/O is incompatible with AppContainer.** `DeviceIoControl` against a raw device handle fails under confinement. Nothing grants it back. Use `launch_suspended()`/`run_under_job()`, the native path, instead for these processes.
- **`sandbox_host.exe` and the checker's probe binaries are build artifacts, not committed source.** Both must be compiled first (see Install / build above).

## Security disclaimer

Read this before relying on `wincage` as a security control rather than a resource-limiting measure.

- AppContainer plus a Job Object reduces what a launched process can reach. **It does not eliminate that risk.**
- A process started through `launch()` still runs as the launching user's identity. It still has whatever `broker_files` paths you granted it. It still shares the desktop, window station, audio session, and GPU with everything else that user runs.
- **This is not a complete sandbox against malicious or untrusted code.** It's built for code you already trust: your own executables, a plugin or worker whose failure mode is a bug rather than an attack, or a third-party tool you're limiting for robustness. It hasn't been designed or reviewed as a containment boundary for hostile code.
- Working around raw device I/O incompatibility usually means weakening confinement. The Job-Object-only fallback drops AppContainer entirely and keeps only resource limits. See Known limitations.
- `run_checks()` tells you whether an API stack survives confinement on a given machine. It does not tell you confinement is sufficient for your threat model.
- For a hard security boundary against actively distrusted code, use a VM or dedicated hardware isolation instead.

## Contributing

Open an issue for bugs or platform-compatibility gaps. Changes to the AppContainer/Job Object provisioning code (`sandbox_host.exe`, `src/`) should include the reasoning behind any new native Win32 call, since a mistake there is a crash-fault-boundary concern, not just a bug.

## License

MIT. See [LICENSE](LICENSE)