# wincage

Windows process sandboxing: launch an executable inside an AppContainer with a
Job Object applying CPU and memory limits, plus a diagnostic package that
probes whether a given machine's AppContainer actually allows the graphics/
audio API stacks a sandboxed process will need.

Two packages ship together:

- **`wincage`** the sandbox itself. `launch(config: SandboxConfig) ->
  SandboxHandle` provisions a per-moniker AppContainer profile, grants it
  access to whatever files the caller named in `broker_files`, starts the
  target executable suspended inside that container under a Job Object with
  CPU/memory limits, resumes it, and returns a handle with an event-callback
  interface (`STARTED`, `EXITED`, `ERROR`, `CLEANED_UP`) for tracking it
  asynchronously. `reset_container(moniker)` deletes a previously-provisioned
  AppContainer profile.
- **`wincage.checker`** a nested diagnostic subpackage. `run_checks()` runs
  a handful of disposable test programs inside a throwaway AppContainer and
  reports, per API stack, whether it actually works under confinement on this
  system. It exists because AppContainer confinement can silently break a
  GPU or audio path that works fine unconfined, and the failure mode is
  usually "the sandboxed process behaves oddly" rather than a clear error —
  `run_checks()` turns that into a yes/no answer *before* you rely on the
  sandbox for something that needs one of those APIs. It ships nested inside
  `wincage` rather than as a separate package because it exists to validate
  `wincage` itself; it launches its probes through `wincage.launch()`.

## Architecture: AppContainer + Job Object, via a separate native process

AppContainer and Job Object solve different problems and both are needed:

- **Job Objects** provide resource limits (CPU rate, memory cap,
  kill-on-close). They add no security containment.
- **AppContainer** confines a process to a derived SID without changing
  identity, so it keeps the launching user's audio session, GPU adapter
  selection, and desktop window station access working (all three fail
  silently under a separate low-privilege account instead). It adds
  filesystem, network, and inter-process isolation on top of a Job Object's
  limits. This is *regular* AppContainer, not LPAC (Less Privileged
  AppContainer): LPAC strips `ALL APPLICATION PACKAGES` from the token,
  which breaks OpenGL ICD loading from DriverStore.

The AppContainer/Job Object provisioning code itself
(`CreateAppContainerProfile`, `SetNamedSecurityInfoW`/
`TreeSetNamedSecurityInfoW` DACL grants, building a `SECURITY_CAPABILITIES`
attribute list for `CreateProcessW`) runs in a separate native helper
process, `sandbox_host.exe`, rather than in-process via `ctypes`. This is a
**crash-fault containment boundary**, not a style preference: a hard fault in
that code (a bad pointer into a `PSID`, a malformed attribute list passed to
`CreateProcessW`, or any other native Win32 misuse) crashes whatever process
it runs in. Windows 11 places essentially every process inside a job by
default, so if this logic ran in-process in a host application, a crash
there would take the host down, and a `KILL_ON_JOB_CLOSE` job containing the
host would cascade-kill every other process it had launched along with it.
Run as a separate child process instead, the same crash costs exactly one
failed launch attempt. The price is a second binary that has to be built
separately (MSYS2/GCC UCRT64, unrelated to a Python/Node host toolchain) and
a stdin/stdout JSON protocol (`sandbox.py`'s `_build_stdin_payload`/response
handling, `src/main.cpp`'s `JsonOut`/`json_parse.h`) standing in for a plain
function call.

## Install / build requirements

Requires GCC from MSYS2 UCRT64:

```sh
pacman -S mingw-w64-ucrt-x86_64-gcc \
          mingw-w64-ucrt-x86_64-SDL2 \
          mingw-w64-ucrt-x86_64-pkg-config \
          mingw-w64-ucrt-x86_64-qt5-base   # optional, for the qt_qpa check
```

From an MSYS2 UCRT64 terminal, build the sandbox host:

```sh
bash wincage/build.sh
```

This outputs `sandbox_host.exe` into `wincage/`, built with
`-Wall -Wextra -Werror -fstack-protector-strong`. To use a different name:

```sh
OUT_NAME=myhost.exe bash wincage/build.sh
```

If you also want the checker's capability probes, build those separately:

```sh
bash wincage/checker/src/build_tests.sh
```

This outputs `test_sdl2_d3d11.exe`, `test_sdl2_opengl.exe`, and (if Qt is
available) `test_qt_qpa.exe` into `wincage/checker/src/`. Neither binary set
is committed to the repository; both are required at runtime and must be
built first.

`wincage` itself has zero non-stdlib runtime dependencies (`ctypes`,
`asyncio`, `pathlib`, `typing`, `dataclasses`, `enum`, `json`, `os`,
`subprocess`, `threading`, `logging` only).

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

# Terminate and wait for cleanup:
await handle.terminate()  # resolves when CLEANED_UP fires

# Delete a container profile later (e.g. after a corrupted session):
wincage.reset_container("myapp.worker")
```

`launch()` returns a `SandboxHandle` after the child process starts.
Callbacks fire from an asyncio task; call `launch()` from within a running
event loop, or register callbacks on the returned handle at any point before
the event fires.

Run the capability checker before relying on the sandbox for something that
needs a specific graphics/audio API:

```python
from wincage.checker import run_checks, CheckStatus

results = run_checks()

for r in results:
    print(f"{r.name}: {r.status.value}")
    print(f"  {r.message}")
    if r.status == CheckStatus.FAIL:
        print(f"  affects: {', '.join(r.affects)}")
```

`run_checks()` never raises; it returns a `list[CheckResult]` regardless of
outcome, and each check is `PASS`, `FAIL`, or `SKIP` (not built yet run
`wincage/checker/src/build_tests.sh` first). `affects` is not a
built-in list of anything pass your own `affects={"sdl2_d3d11": [...], ...}`
mapping to `run_checks()` if you want each `CheckResult` to carry which of
*your* components a failure impacts; names you don't supply default to `[]`.
The checker only knows which API stack failed, not what depends on it.

### Runtime diagnostic scripts

`wincage/scripts/` has two PowerShell scripts, `Test-AppContainerStatus.ps1`
and `Test-JobObjectStatus.ps1`, for a different question than the checker
answers: not "will sandboxing work on this machine" but "is *this specific,
already-running* process actually confined the way I expect." Both take a
`-Moniker` (the same string passed as `SandboxConfig.moniker`) and search
running processes for a match; pass `-ProcessId` to check one process
directly instead. See each script's comment-based help
(`Get-Help .\Test-AppContainerStatus.ps1 -Full`) for the full detail on what
each check can and cannot prove.

## Public API reference

### `wincage`

| Export | What it is |
|---|---|
| `launch(config)` | Provisions the AppContainer, starts the process, returns a `SandboxHandle`. See `sandbox.py` docstring. |
| `reset_container(moniker)` | Deletes a previously-provisioned AppContainer profile. See `sandbox.py` docstring. |
| `SandboxConfig` | Dataclass of launch parameters (moniker, exe_path, broker_files, resource limits, ...). See `sandbox_config.py`. |
| `BrokerFile` | One file/directory grant entry for `SandboxConfig.broker_files`. See `sandbox_config.py`. |
| `SandboxHandle` | Returned by `launch()`; exposes `.on(event, callback)` and `.terminate()`. See `sandbox.py`. |
| `SandboxEvent` | Enum of handle events: `STARTED`, `EXITED`, `ERROR`, `CLEANED_UP`. See `sandbox_event.py`. |
| `SandboxPayload` | Dataclass passed to event callbacks. See `sandbox_event.py`. |
| `SandboxStage` | Enum identifying which stage of the launch/teardown pipeline an error or payload refers to. See `sandbox_event.py`. |
| `SandboxError` | Raised by `launch()`/`reset_container()` on failure; carries `.stage` and `.suggestions`. See `sandbox_error.py`. |
| `EXE_NAME` | Name of the host executable `launch()` spawns (default `"sandbox_host.exe"`); assignable before the first `launch()` call. See `sandbox.py`. |
| `launch_suspended(exe, args, flags, ...)` | Starts a process suspended, natively via `CreateProcessW` or inside an AppContainer, returns a `SandboxProcess`. See `process.py` docstring. |
| `run_under_job(executable_path, ..., process, job_name, ...)` | Creates a Job Object, assigns a suspended `SandboxProcess` to it, resumes it, returns `(SandboxProcess, WindowsJobObject)`. See `process.py` docstring. |
| `SandboxProcess` | Process handle returned by `launch_suspended()`/`run_under_job()`; exposes `.poll()`, `.terminate()`, `.kill()`, `.wait()`, `.resume()`. See `sandbox_process.py`. |
| `WindowsJobObject` | Win32 Job Object wrapper returned by `run_under_job()`; exposes `.set_memory_limit()`, `.set_cpu_limit()`, `.teardown()`, `.close()`. See `job.py`. |

### `wincage.checker`

| Export | What it is |
|---|---|
| `run_checks(moniker_prefix=..., affects=...)` | Runs every capability probe, never raises. See `checker.py` docstring. |
| `CheckResult` | Dataclass: `name`, `status`, `message`, `affects`. See `results.py`. |
| `CheckStatus` | Enum: `PASS`, `FAIL`, `SKIP`. See `results.py`. |
| `DEFAULT_MONIKER_PREFIX` | Default AppContainer moniker prefix used by `run_checks()` when `moniker_prefix` isn't passed. See `checker.py`. |

## Package layout

```
wincage/                  # sandbox core
├── __init__.py           # public exports (table above)
├── sandbox.py            # launch() / reset_container(), talks to sandbox_host.exe
├── sandbox_config.py     # SandboxConfig, BrokerFile
├── sandbox_error.py      # SandboxError
├── sandbox_event.py      # SandboxEvent, SandboxStage, SandboxPayload
├── job.py                # WindowsJobObject (native, non-container launch path)
├── process.py            # launch_suspended() / run_under_job() (native launch path)
├── sandbox_process.py    # SandboxProcess (native launch path's process handle)
├── win32_types.py        # ctypes structures/constants, Win32 interop only
├── build.sh              # builds sandbox_host.exe
├── src/                  # sandbox_host.exe C++ source
├── scripts/              # runtime diagnostic PowerShell scripts
└── checker/               # nested diagnostic subpackage
    ├── __init__.py       # public exports (table above)
    ├── checker.py        # run_checks()
    ├── results.py        # CheckResult, CheckStatus
    └── src/               # capability-probe test programs + build_tests.sh
```

`job.py`, `process.py`, and `sandbox_process.py` implement a second, native
(non-AppContainer) launch path built directly on a Job Object, for a host
that wants Job Object resource limits without AppContainer confinement.
`launch_suspended`, `run_under_job`, `SandboxProcess`, and `WindowsJobObject`
are all re-exported from `wincage/__init__.py` alongside the AppContainer
path (see the public API reference above).

## Known limitations

- **Windows only.** Both the native helper process and the Python wrapper's
  `ctypes.windll` calls require Win32 AppContainer and Job Object APIs; the
  package will fail to import on non-Windows hosts.
- **DACL grants are permanent on the path.** A `BrokerFile` with
  `mode="grant"` or `mode="secure"` modifies the filesystem ACL and does not
  revert on process exit; `grant` additionally propagates its ACE across the
  existing tree under `path`. Broker only what the sandboxed process
  requires, and prefer `secure` or `inherit` over `grant` when a single file
  is enough.
- **Container profiles are never deleted automatically.** A profile
  provisioned for a moniker persists across launches and reboots by design.
  Call `reset_container()` to remove one; per-moniker ACEs already granted to
  a deleted profile's SID are not cleaned up.
- **Qt's platform plugin fails under a memory cap.** Processes using the Qt
  platform plugin allocate a large heap at startup and abort if the Job
  Object memory limit is hit before the window appears. Pass
  `memory_limit_mb=None` for these processes; the Job Object is still
  created and CPU limits still apply.
- **Raw device I/O is incompatible with AppContainer.** A process that needs
  `DeviceIoControl` against a raw device handle will fail under AppContainer
  confinement; the container's derived SID has no access to the device
  namespace those calls go through. There is no configuration that grants it
  back. Use the native Job-Object-only launch path (`launch_suspended()` /
  `run_under_job()`, see Package layout above) as a fallback for these
  processes; it gives resource limits without AppContainer confinement.
- **`sandbox_host.exe` and the checker's probe binaries are build artifacts,
  not committed source.** Both must be compiled before the corresponding
  package is usable (see Install / build requirements above).

## Security disclaimer

Read this before using `wincage` as a security control rather than as a
resource-limiting and tidiness measure.

AppContainer confinement plus a Job Object meaningfully *reduces* what a
launched process can reach and how much of the machine it can consume. It
does not *eliminate* that risk. A process started through `launch()` still
runs as the launching user's identity, still has whatever paths you named in
`broker_files` granted to it, and still shares the desktop, window station,
audio session, and GPU with everything else that user is running. That is
deliberate, it is what makes graphics and audio keep working under
confinement, and it is also exactly why the boundary is not airtight.

**This package is not a complete sandbox against a genuinely malicious or
untrusted binary.** It is built for confining code you have some reason to
trust: your own executables, a plugin or worker whose failure mode you expect
to be a bug rather than an attack, or a third-party tool you are limiting for
robustness rather than defending against. It has not been designed or
reviewed as a containment boundary for hostile code, and it makes no attempt
to block the many ways a determined process can influence the session it runs
inside.

Known incompatibilities also exist, and they matter here because working
around one usually means weakening confinement. The clearest example is raw
device I/O via `DeviceIoControl`, which breaks under AppContainer; see
**Known limitations** above for the detail and for the Job-Object-only
fallback. Note that the fallback path drops AppContainer confinement
entirely and keeps only the resource limits, so choosing it is a real
reduction in isolation, not a workaround that preserves it.

`wincage` is provided as-is, with no guarantee of total isolation. Running
`wincage.checker.run_checks()` tells you whether an API stack survives
confinement on a given machine; it does not tell you that confinement is
sufficient for your threat model, and nothing in this package does.

If you need a hard security boundary against code you actively distrust, use
a virtual machine or dedicated hardware isolation. Do not rely on this
package alone for that.

## License

MIT. See [LICENSE](LICENSE).
