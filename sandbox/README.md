# sandbox

Launches a Windows executable inside an AppContainer with a Job Object for CPU and
memory limits. Implemented as a separate C++ host process (`sandbox_host.exe` by
default) driven by a thin Python wrapper over stdin/stdout JSON, not as in-process
`ctypes` calls. Built to eventually be extracted into its own standalone package,
the same status as `smart_media_detector`, so it is kept mostly free of
Peach 1UP-specific imports (see "Standalone-package intent" below for the current
state of that goal, including where it still falls short).

## What it does

`launch(config: SandboxConfig) -> SandboxHandle` provisions a per-moniker
AppContainer profile, grants it access to whatever files the caller named in
`broker_files`, starts the target executable suspended inside that container under
a Job Object with CPU/memory limits, resumes it, and returns a handle with an
event-callback interface (`STARTED`, `EXITED`, `ERROR`, `CLEANED_UP`) for tracking
it asynchronously. `reset_container(moniker)` deletes a previously-provisioned
AppContainer profile.

## Why a separate C++ helper process, not in-process ctypes

The AppContainer/Job Object provisioning code (`CreateAppContainerProfile`,
`SetNamedSecurityInfoW`/`TreeSetNamedSecurityInfoW` DACL grants, building a
`SECURITY_CAPABILITIES` attribute list for `CreateProcessW`) runs in a child
process, `sandbox_host.exe`, rather than directly in the backend via `ctypes`.

This is a crash-fault containment boundary, not a style preference. A hard fault in
that code (a bad pointer into a `PSID`, a malformed attribute list passed to
`CreateProcessW`, or any other native Win32 misuse) crashes whatever process it
runs in. Windows 11 places essentially every process inside a job by default (see
`src/main.cpp`'s `IsProcessInJob` comment), so if this logic ran in-process in the
backend, a crash there would take the backend down, and a `KILL_ON_JOB_CLOSE` job
containing the backend would cascade-kill every currently running emulator along
with it. Run as a separate child process instead, the same crash costs exactly one
failed launch attempt: the backend and every other running emulator are unaffected.

The price for that containment is real and is paid deliberately: a second binary
that has to be built (and, eventually, code-signed) separately from the Python
backend, an MSYS2/GCC UCRT64 build dependency that has nothing to do with the rest
of this project's Python/Node toolchain, and a stdin/stdout JSON protocol
(`sandbox.py`'s `_build_stdin_payload`/response handling, `src/main.cpp`'s
`JsonOut`/`json_parse.h`) standing in for a plain function call. See
`dev_docs/DECISIONS.md`'s 2026-08-06 entry for the full record of that tradeoff.

## Design rationale

**AppContainer over a dedicated low-privilege account**

Running a process under a separate Windows account loses the launching user's audio
session, GPU adapter selection, and desktop window station access, all three fail
silently. AppContainer confines the process to a derived SID without changing identity,
so those subsystems continue to work.

**AppContainer over Job Objects alone**

Job Objects provide resource limits (CPU rate, memory cap, kill-on-close). They add
no security containment. AppContainer adds filesystem, network, and inter-process
isolation on top of those limits.

**Regular AppContainer, not LPAC**

Less Privileged AppContainer (LPAC) removes `ALL APPLICATION PACKAGES` from the
token. This breaks OpenGL ICD loading from DriverStore and reproduces the same silent
failures as the low-privilege account approach. Regular AppContainer is used instead.

## Building

Requires GCC from MSYS2 UCRT64:

```sh
pacman -S mingw-w64-ucrt-x86_64-gcc
```

From an MSYS2 UCRT64 terminal, run `build.sh` from the package directory:

```sh
bash build.sh
```

Outputs `sandbox_host.exe` into the package directory, built with
`-Wall -Wextra -Werror -fstack-protector-strong`, so any new compiler warning fails
the build rather than shipping silently. To use a different name:

```sh
OUT_NAME=myhost.exe bash build.sh
```

## Usage

```python
import sandbox

# Override the host executable name before the first call to launch().
# Default is "sandbox_host.exe", located next to this package's __init__.py.
sandbox.EXE_NAME = "myhost.exe"

from sandbox import (
    launch, reset_container,
    SandboxConfig, BrokerFile,
    SandboxEvent, SandboxPayload,
    SandboxError, SandboxStage,
)

config = SandboxConfig(
    moniker="myapp.worker",       # stable identifier, reused across launches
    exe_path="C:/apps/worker.exe",
    args=["--headless"],
    working_dir="C:/data/jobs",
    broker_files=[
        BrokerFile(path="C:/data/jobs", access="rw", mode="grant"),
    ],
    cpu_max_rate=60,
    cpu_min_rate=5,
    memory_limit_mb=512,
)

handle = launch(config)   # synchronous; raises SandboxError on failure

handle.on(SandboxEvent.EXITED, lambda p: print(f"exited: {p.exit_code}"))
handle.on(SandboxEvent.ERROR,  lambda p: print(f"error: {p.error}"))

# Terminate and wait for cleanup:
await handle.terminate()  # resolves when CLEANED_UP fires
```

`launch()` returns a `SandboxHandle` after the child process starts. Callbacks are
fired from an asyncio task, call `launch()` from within a running event loop, or
register callbacks on the returned handle at any point before the event fires.

To delete a container profile (e.g. after a corrupted session):

```python
reset_container("myapp.worker")
```

## Moniker

The `moniker` field in `SandboxConfig` names the AppContainer profile. It must be
stable across launches for the same logical process, the profile is created once and
reused. There is no required prefix or format, any non-empty string is valid; a
consumer embedding this package picks whatever naming convention suits its own
application (`sandbox_checker`, for example, namespaces its diagnostic profiles as
`f"{moniker_prefix}.{check_name}"`, with the prefix supplied by its own caller so two
embedding applications running the checker never collide on the same profile name).
Choose something that uniquely identifies the process role within your application.

If left unset the default is `""`, which will raise a `SandboxError` at
`CONFIG_VALIDATION` stage: moniker is required.

## Broker files

`broker_files` is how the sandboxed process is given access to anything outside the
AppContainer's own storage. Each `BrokerFile` has three fields, all required, and all
plain values, there is no indirection (settings lookups, environment-variable
resolution, path templating) inside this package itself; a caller resolves a real
absolute path before constructing a `BrokerFile`:

| Field    | Values                              | Meaning                                                                    |
| -------- | ------------------------------------ | --------------------------------------------------------------------------- |
| `path`   | absolute path                       | The file or directory to broker.                                            |
| `access` | `"r"`, `"rw"`, `"x"`                | Read, read/write, or traverse-only (`FILE_TRAVERSE | FILE_READ_ATTRIBUTES`). |
| `mode`   | `"grant"`, `"secure"`, `"inherit"`  | How access is handed over. See below.                                       |

- **`grant`** applies an inheritable ACE for the container SID to a directory and
  propagates it across the existing tree (`grant_directory` in `src/container.cpp`),
  so files already present are covered as well as files created later. A prior
  launch's grant is detected and skipped on repeat launches rather than re-walking
  the whole tree every time.
- **`secure`** applies a single non-inheriting ACE to one existing file
  (`secure_existing_file`). Use this when only one specific file should be
  reachable, not its whole directory.
- **`inherit`** opens the file in the host and passes the inheritable handle to the
  child instead of touching any ACL. The handle value is exposed to the child in the
  environment variable `SANDBOX_HANDLE_<i>`, where `<i>` is the entry's index in
  `broker_files`. The target executable has to know to read it.

`sandbox_host.exe` processes `broker_files` in order, before the target process is
created. Every entry is mandatory: if any grant fails, the launch aborts with a
`SandboxError` at the `DACL_GRANT` stage (and any handles already opened for earlier
`inherit` entries are closed) rather than starting a process that cannot reach its
own data.

A consumer with a richer descriptor model (Peach 1UP's own
`EmulatorDescriptor.container_broker_files`, for example, supports a `path_key`
indirection resolved against per-emulator derived paths, the settings store, or
environment variables) resolves that down to a plain `BrokerFile(path=..., ...)`
before calling `launch()`. None of that resolution logic lives in this package;
`BrokerFile` only ever sees the final, concrete path.

## Configuration reference

| Field             | Default  | Notes                                                                  |
| ------------------ | -------- | ---------------------------------------------------------------------- |
| `moniker`         | required | AppContainer profile name. Stable per process role.                    |
| `exe_path`        | required | Absolute path to the target executable.                                |
| `args`            | `[]`     | Command-line arguments.                                                |
| `working_dir`     | `None`   | Working directory. `None` inherits from parent process.                |
| `broker_files`    | `[]`     | `BrokerFile(path, access, mode)` entries. See Broker files below.      |
| `breakaway`       | `False`  | `True` adds `CREATE_BREAKAWAY_FROM_JOB` so the target escapes the host's own job before being assigned to its sandbox job. The host retries with this set automatically when job assignment fails with `ERROR_ACCESS_DENIED`. |
| `cpu_max_rate`    | `50`     | Max CPU rate, percent (1-100). Applied via `MIN_MAX_RATE`.             |
| `cpu_min_rate`    | `5`      | Floor CPU rate. Prevents audio starvation under sustained load.        |
| `skip_cpu_limit`  | `False`  | `True` leaves CPU rate control off entirely. `cpu_max_rate`/`cpu_min_rate` stay populated and validated either way; the flag governs application, not validity.     |
| `memory_limit_mb` | `None`   | `None` disables the cap. Enforced per-process (`JOB_OBJECT_LIMIT_PROCESS_MEMORY`), matching the non-container Job-Object-only launch path elsewhere in this codebase. See Known Constraints. |

## Standalone-package intent

The extractable core is this directory (`sandbox/`) plus the sibling
`sandbox_checker/` diagnostic package, `win32_types.py` and `sandbox_process.py`
(both already inside this directory) confirmed as in-scope rather than
adapter-only. Neither package reaches into `eras.yaml`, the emulator catalog, or
Peach 1UP's settings/database stack directly; `job.py`'s `WindowsJobObject` and
`process.py`'s `launch_suspended`/`run_under_job` all take already-resolved numbers
(memory limit, CPU percentages, a fully-built `SandboxConfig`) rather than reading
Peach 1UP config themselves. The Peach 1UP-specific adapter,
`platform/windows/process/launcher.py`, sits one directory up, outside this package:
it resolves `eras.yaml`/emulator-catalog data and calls `launch_suspended`/
`run_under_job` as its two delegation points into this package. That split, generic
core here, adapter one level up, is the intended shape for extraction, not an
accident of file layout.

- **`backend.*` imports are not yet zero.** `job.py`, `process.py`, `sandbox.py`,
  and `sandbox_process.py` all import `backend.core.logger.get_logger` for their
  module loggers, four call sites total (confirmed by grep). This is the exact
  pattern `smart_media_detector` already closed for its own two `backend.core.logger`
  imports: swap each for stdlib `logging.getLogger(__name__)`, which still produces
  a logger name (`backend.service.utils.platform.windows.sandbox.<module>`) that
  `setup_logging()` in `backend/core/logger.py` picks up automatically for file
  handlers, at the cost of the same console-echo difference `smart_media_detector`'s
  README documents for its own equivalent change. Not yet done here.
- **Docstrings and comments still say "Peach 1UP" and reference `eras.yaml`/
  `launcher.py` by name** in `job.py`, `process.py`, `win32_types.py`, and
  `sandbox.py`, even where the code itself takes pre-resolved parameters and has no
  functional dependency on any of those. Cosmetic naming residue from before the
  generic/adapter split, not a functional coupling, worth generalizing before
  extraction so the package reads as its own thing rather than as an
  extracted-in-place fragment of Peach 1UP.
- **No packaging scaffolding existed before this pass.** See `pyproject.toml` in
  this directory (added alongside this README) for the current state.
- **`sandbox_host.exe` is a build artifact, not committed source.** It has to be
  compiled (`build.sh`, MSYS2 UCRT64 GCC) before either package is usable, and is
  not itself part of the pip-installable package; an extraction plan needs to
  decide how the compiled binary ships (bundled build step, separate release
  asset, or a required build-from-source step for consumers).

### Extraction readiness checklist

- [ ] Zero `backend.*` imports anywhere under `sandbox/`/`sandbox_checker/`
  (excluding this checklist item's own grep target). Four `backend.core.logger`
  imports remain; see above.
- [x] Generic/adapter split already correct on disk: `job.py`/`process.py` take
  pre-resolved values, all `eras.yaml`/emulator-catalog/settings coupling lives in
  `platform/windows/process/launcher.py`, one directory outside this package.
- [x] `BrokerFile`/`SandboxConfig` are plain dataclasses with no indirection
  (settings keys, `path_key` templating) of their own; a consumer's richer
  descriptor model resolves down to concrete values before calling `launch()`.
- [ ] Cosmetic Peach 1UP naming in docstrings/comments (see above). Not started.
- [x] Packaging scaffolding (`pyproject.toml`, version metadata). Added alongside
  this README.
- [ ] `sandbox_host.exe` distribution story (bundled build vs. release asset vs.
  build-from-source requirement). Not decided.

## Dependencies

- **GCC (MSYS2 UCRT64)**: `pacman -S mingw-w64-ucrt-x86_64-gcc`
- **json_parse.h**: bundled single-header JSON parser, no external dependencies

## Known constraints

- **Windows only.** The host process uses Win32 AppContainer and Job Object APIs.
  The Python wrapper calls `ctypes.windll` at runtime and will fail on non-Windows hosts.
- **DACL grants are permanent on the path.** The `grant` and `secure` broker modes
  modify the filesystem ACL and do not revert on process exit. `grant` additionally
  propagates its ACE across the existing tree under `path`. Broker only what the
  sandboxed process requires, and prefer `secure` or `inherit` over `grant` when a
  single file is enough.
- **Container profiles are never deleted automatically.** A profile provisioned for a
  moniker persists across launches and reboots by design. Call `reset_container` to
  remove one. Per-moniker ACEs granted to a deleted profile's SID are not cleaned up.
- **Qt platform plugin fails under memory caps.** Processes that use the Qt platform
  plugin allocate a large heap at startup and abort if the Job Object memory limit is
  hit before the window appears. Pass `memory_limit_mb=None` for these processes; the
  Job Object is still created and CPU limits still apply.
