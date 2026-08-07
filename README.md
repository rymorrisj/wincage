# wincage

Windows Job Object + AppContainer sandboxing utility for launching processes under host-level isolation. Zero third-party dependencies.

## What's in this repo

### sandbox/
The core library. Sandboxed process launching via a two-layer isolation model:

- **Job Objects** — CPU/memory limits, kill-on-close semantics, process-tree containment
- **AppContainer** — low-privilege process isolation, enforced via scoped DACL grants rather than broad directory-tree access

Launches route through a separate native helper process (`sandbox_host.exe`, built from the bundled C++ source) rather than in-process ctypes calls. This is a deliberate crash-fault containment boundary: a hard fault in ACL/SID handling costs one launch, not the parent process plus everything else it's managing.

Key modules:
- `job.py` — Job Object mechanics
- `process.py` — launch orchestration
- `sandbox.py` — AppContainer setup, DACL grants
- `sandbox_process.py` — Python-side broker protocol to the native helper
- `win32_types.py` — ctypes structures/signatures for the Win32 APIs in use

`sandbox/scripts/` — PowerShell diagnostics (`Test-AppContainerStatus.ps1`, `Test-JobObjectStatus.ps1`) for verifying a *currently running* process's isolation status by moniker, no PID required.

**Known limitation:** 

- AppContainer is incompatible with processes requiring raw device I/O (`DeviceIoControl`). Job-Object-only isolation is available as a fallback.

### sandbox_checker/
Preflight compatibility probe. Answers "will AppContainer actually work on this host" *before* anything real launches, by running short native test programs inside a real AppContainer and reporting whether specific API stacks (D3D11, OpenGL, Qt/QPA) remain reachable when confined.

`run_checks()` returns a structured `list[CheckResult]`. Never raises. Meant to be called programmatically, at startup or on first-run, to drive isolation config decisions automatically rather than requiring a human to interpret output.

## Why these live together

`sandbox_checker` depends on `sandbox` to actually launch its test executables under AppContainer, and exists to verify `sandbox`'s guarantees hold on a given host before anything depends on them. Versioned and released together so the checker can't drift from the launcher it's checking.

## Requirements
- Windows only
- Native helper (`sandbox_host.exe`) and `sandbox_checker` test executables built via MSYS2 UCRT64 — see each subfolder's build instructions
- Zero third-party Python dependencies

## Status
Alpha development
