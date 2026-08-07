# sandbox/scripts

Runtime diagnostic scripts for verifying that a specific, already-launched
process is actually sandboxed under a specific `sandbox` package moniker, not
a build-time capability probe. If you want to know "will sandboxing even work
on this machine" before enabling it for a consuming application, that's
`../../sandbox_checker`, which runs disposable test programs inside a
throwaway AppContainer at build/setup time. These scripts answer a different
question: "is this specific process, right now, actually confined the way I
expect it to be." Use them after launching something through the `sandbox`
package's moniker-based `launch()` to confirm containment for any consuming
application, emulator or otherwise, or while troubleshooting a report that
sandboxing "isn't working."

## Scripts

| Script | Checks | Exit codes |
|---|---|---|
| `Test-AppContainerStatus.ps1` | Which running process(es), if any, have a token AppContainer SID matching `-Moniker` | 0 = at least one match (or the specified `-ProcessId` matches), 1 = no match, 2 = error |
| `Test-JobObjectStatus.ps1` | For process(es) matching `-Moniker`, is each assigned to any Job Object | 0 = at least one matched process is in a job, 1 = no match or none in a job, 2 = error (`-Moniker` finds the process(es) but cannot disambiguate the job itself, see below) |

Both scripts require only `-Moniker`, the same string passed as
`SandboxConfig.moniker` when the process was launched (see the moniker
section of `../README.md`). By default they search every running process for
one confined under that moniker, no PID or process name needed up front:

```powershell
.\Test-AppContainerStatus.ps1 -Moniker "MyApp.worker"
.\Test-JobObjectStatus.ps1 -Moniker "MyApp.worker"
```

Pass `-ProcessId` to skip the search and check one already-known process
directly instead, faster and narrower:

```powershell
.\Test-AppContainerStatus.ps1 -Moniker "MyApp.worker" -ProcessId 18432
.\Test-JobObjectStatus.ps1 -Moniker "MyApp.worker" -ProcessId 18432
```

There is no way to pre-filter the search by process name: a moniker names an
AppContainer profile, not an executable, so nothing ties the two together in
advance. Both scripts enumerate every running process and check each one's
token; processes that cannot be opened (owned by another user, already
exited) are skipped and counted rather than failing the whole run. Opening
another user's process token typically requires an elevated (Run as
Administrator) PowerShell session to include it in the search.

See each script's comment-based help for full details:

```powershell
Get-Help .\Test-AppContainerStatus.ps1 -Full
Get-Help .\Test-JobObjectStatus.ps1 -Full
```

## What each one actually tells you

**`Test-AppContainerStatus.ps1`** derives the AppContainer SID that
`-Moniker` would produce via `DeriveAppContainerSidFromAppContainerName`, the
same Win32 call `../src/container.cpp`'s `AppContainer::provision()` uses
when a profile already exists, converts it to a string SID, and compares
that string against every running process's own token `TokenAppContainerSid`
(also converted to a string SID for the comparison, never raw pointers).
Querying `TokenAppContainerSid` requires the two-call `GetTokenInformation`
pattern: `TOKEN_APPCONTAINER_INFORMATION` is just `{ PSID TokenAppContainer; }`,
one pointer field, but the actual SID bytes that pointer references are
appended after the struct in the same buffer, so the required buffer size
has to be queried first (a call with a null buffer and length 0, which fails
with `ERROR_INSUFFICIENT_BUFFER` but reports the real size in its `retLen`
out-parameter) and only then allocated and re-queried. A fixed pointer-sized
buffer is not enough and fails against any actually-confined process. In
discovery mode this gives a list of every matching process; in
`-ProcessId` mode it gives a definitive three-way result: confirmed match,
AppContainer-confined but under a *different* moniker (both SIDs printed),
or not AppContainer-confined at all.

**`Test-JobObjectStatus.ps1`** reuses that same moniker-matching search to
find which process(es) belong to `-Moniker`, then for each one calls
`IsProcessInJob` with a NULL job handle, which only answers "is this process
in *any* job", not "is it in *our* job with our CPU/memory limits." On
Windows 11, essentially every process is pre-assigned to an OS-managed job by
default, so this check can return `True` even when the sandbox package's own
resource-limiting job was never applied. **A `True` result here is not proof
the app's specific limits are active.** Unlike the AppContainer script,
`-Moniker` cannot resolve this specific ambiguity, it only identifies *which*
process(es) to check, not *which* job they are in: the Job Object
`sandbox_host.exe` creates for a container launch is unnamed
(`../src/job.cpp`, `CreateJobObjectW(nullptr, nullptr)`), so there is no name
to derive from a moniker and open independently. The script prints an
explicit warning to this effect whenever a matched process returns `True`,
and points you at Sysinternals Process Explorer (select the process,
Properties > Job tab) as the definitive way to see the actual applied limit
values, since they are not queryable from outside the process without a
handle to that specific Job Object. This same NULL-handle ambiguity is
documented in `../src/main.cpp`, where it previously caused the host process
to retry job assignment unconditionally.

## Requirements

- Windows PowerShell 5.1 or PowerShell 7+.
- The target process must already be running.
- Administrator privileges if the target process belongs to another user
  session.

These scripts are pure PowerShell plus P/Invoke declared inline via
`Add-Type`, no build step and no dependency on `sandbox_host.exe` or the
Python `sandbox` package itself.
