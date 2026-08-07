<#
.SYNOPSIS
    Finds running process(es) confined under a sandbox package moniker and
    reports whether each is assigned to any Windows Job Object.

.DESCRIPTION
    Test-JobObjectStatus first finds which running process(es) belong to
    -Moniker (the same AppContainer-SID matching Test-AppContainerStatus.ps1
    performs, see ../src/container.cpp's AppContainer::provision() for the
    underlying DeriveAppContainerSidFromAppContainerName mechanism), then, for
    each matched process, calls IsProcessInJob with a NULL job handle to
    determine whether it is a member of any Job Object at all.

    IMPORTANT AMBIGUITY: a NULL job handle only answers "is this process in ANY
    job", not "is it in OUR job with our specific CPU/memory limits applied."
    On Windows 11, essentially every process is pre-assigned to an OS-managed job
    by default (see this package's own src/main.cpp, IsProcessInJob comment, and
    SECURITY.md). That means this check can report True even when the sandbox
    package's own resource-limiting job was never applied to the process. A True
    result is NOT proof that the app's CPU/memory limits are active, only that
    the process sits in some job, which may or may not be the sandbox's own.

    -Moniker CANNOT resolve this specific ambiguity, it only identifies WHICH
    process(es) to check, not which job they are in. The Job Object that
    sandbox_host.exe creates for a container launch is unnamed (src/job.cpp,
    CreateJobObjectW(nullptr, nullptr)), so there is no name to derive from a
    moniker and open independently with OpenJobObject; this is true regardless
    of moniker value. (A separate, named-Job-Object launch path also exists in
    this package for non-containerized native launches, but its job names are
    constructed by the consuming application, PID-suffixed, and never derived
    from a moniker either, so no moniker-based lookup is possible on that path.)
    There is no supported way to query the CPU/memory limit values of a
    specific Job Object from outside the process that holds the job handle.
    When a matched process reports True, cross check the actual applied
    limits with Sysinternals Process Explorer (select the process,
    Properties > Job tab) for a definitive answer.

    Two modes:

      Discovery mode (default, no -ProcessId): enumerates every running
      process to find which one(s) are confined under -Moniker, using the
      same full-enumeration approach as Test-AppContainerStatus.ps1 (there is
      no reliable way to pre-filter by process name, a moniker names an
      AppContainer profile, not an executable), skipping and counting
      processes that cannot be opened rather than aborting. Reports Job
      Object status for every matched process.

      Single-process mode (-ProcessId supplied): skips discovery entirely and
      reports Job Object status for that one PID directly, faster and
      narrower when you already know which process you want checked. Moniker
      membership is not verified in this mode.

.PARAMETER Moniker
    The sandbox package moniker to search for (discovery mode) or to display
    for context (single-process mode). Cannot disambiguate Job Object status
    itself, see DESCRIPTION.

.PARAMETER ProcessId
    Optional. The process ID (PID) of a specific running process to check
    directly, skipping moniker-based discovery.

.EXAMPLE
    .\Test-JobObjectStatus.ps1 -Moniker "MyApp.worker"

    Finds all running processes confined under moniker "MyApp.worker" and
    reports Job Object status for each.

.EXAMPLE
    .\Test-JobObjectStatus.ps1 -Moniker "MyApp.worker" -ProcessId 18432

    Checks Job Object status for PID 18432 directly, no discovery step.

.OUTPUTS
    None. Writes a human-readable result to the host and sets $LASTEXITCODE.

.NOTES
    Exit codes:
      0  Discovery mode: at least one matched process IS in a Job Object.
         Single-process mode: the specified process IS in a Job Object.
         (See the ambiguity warning above, this does not by itself confirm
         the sandbox's own resource limits are active.)
      1  Discovery mode: no process matched -Moniker, or none of the matched
         processes are in a Job Object.
         Single-process mode: the specified process is NOT in any Job Object.
      2  The check could not be completed (moniker SID could not be derived,
         the specified -ProcessId was not found, or another error). See the
         error message for detail.

    Opening another user's process typically requires an elevated (Run as
    Administrator) PowerShell session; processes that cannot be opened are
    skipped in discovery mode rather than failing the whole run.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Moniker,

    [Parameter(Mandatory = $false)]
    [int]$ProcessId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not ('SandboxDiagnostics.JobObjectNative' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace SandboxDiagnostics
{
    public static class JobObjectNative
    {
        public const int PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        public const int TOKEN_QUERY = 0x0008;
        public const int TokenAppContainerSid = 31;

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(int dwDesiredAccess, bool bInheritHandle, int dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool IsProcessInJob(IntPtr processHandle, IntPtr jobHandle, out bool result);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern bool OpenProcessToken(IntPtr processHandle, int desiredAccess, out IntPtr tokenHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern bool GetTokenInformation(
            IntPtr tokenHandle,
            int tokenInformationClass,
            IntPtr tokenInformation,
            int tokenInformationLength,
            out int returnLength);

        [DllImport("userenv.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern int DeriveAppContainerSidFromAppContainerName(
            string appContainerName,
            out IntPtr appContainerSid);

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool ConvertSidToStringSidW(IntPtr sid, out IntPtr stringSid);

        [DllImport("advapi32.dll")]
        public static extern IntPtr FreeSid(IntPtr pSid);

        [DllImport("kernel32.dll")]
        public static extern IntPtr LocalFree(IntPtr hMem);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);
    }
}
'@
}

function ConvertTo-SidString {
    param([IntPtr]$Sid)

    if ($Sid -eq [IntPtr]::Zero) { return $null }

    $strPtr = [IntPtr]::Zero
    try {
        if (-not [SandboxDiagnostics.JobObjectNative]::ConvertSidToStringSidW($Sid, [ref]$strPtr)) {
            return $null
        }
        return [System.Runtime.InteropServices.Marshal]::PtrToStringUni($strPtr)
    }
    finally {
        if ($strPtr -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.JobObjectNative]::LocalFree($strPtr) | Out-Null
        }
    }
}

function Get-ExpectedMonikerSidString {
    param([string]$MonikerName)

    $derivedSid = [IntPtr]::Zero
    try {
        $hr = [SandboxDiagnostics.JobObjectNative]::DeriveAppContainerSidFromAppContainerName($MonikerName, [ref]$derivedSid)
        if ($hr -ne 0) {
            throw "DeriveAppContainerSidFromAppContainerName failed for moniker '$MonikerName' (HRESULT 0x$($hr.ToString('X8')))."
        }
        return ConvertTo-SidString -Sid $derivedSid
    }
    finally {
        if ($derivedSid -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.JobObjectNative]::FreeSid($derivedSid) | Out-Null
        }
    }
}

# Queries a single process's token AppContainer SID as a string, or $null if
# the process is not AppContainer-confined. Throws on any failure to open the
# process/token; the discovery loop below catches this and skip-and-counts
# rather than aborting the whole enumeration.
function Get-ProcessAppContainerSidString {
    param([int]$TargetPid)

    $hProcess = [IntPtr]::Zero
    $hToken = [IntPtr]::Zero
    $infoBuffer = [IntPtr]::Zero

    try {
        $hProcess = [SandboxDiagnostics.JobObjectNative]::OpenProcess(
            [SandboxDiagnostics.JobObjectNative]::PROCESS_QUERY_LIMITED_INFORMATION,
            $false,
            $TargetPid)
        if ($hProcess -eq [IntPtr]::Zero) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        if (-not [SandboxDiagnostics.JobObjectNative]::OpenProcessToken(
                $hProcess, [SandboxDiagnostics.JobObjectNative]::TOKEN_QUERY, [ref]$hToken)) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        # TOKEN_APPCONTAINER_INFORMATION is { PSID TokenAppContainer; }, one
        # pointer field, but GetTokenInformation's required buffer also has to
        # hold the SID bytes that pointer references, appended after the
        # struct. A fixed IntPtr.Size buffer is only big enough for the
        # pointer itself and fails with ERROR_INSUFFICIENT_BUFFER (122) for
        # any process that actually has a non-null AppContainer SID, so the
        # size has to come from a first probing call rather than be assumed.
        $requiredLength = 0
        [SandboxDiagnostics.JobObjectNative]::GetTokenInformation(
            $hToken,
            [SandboxDiagnostics.JobObjectNative]::TokenAppContainerSid,
            [IntPtr]::Zero,
            0,
            [ref]$requiredLength) | Out-Null

        if ($requiredLength -le 0) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        $infoBuffer = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($requiredLength)
        $actualLength = 0
        $gotInfo = [SandboxDiagnostics.JobObjectNative]::GetTokenInformation(
            $hToken,
            [SandboxDiagnostics.JobObjectNative]::TokenAppContainerSid,
            $infoBuffer,
            $requiredLength,
            [ref]$actualLength)

        if (-not $gotInfo) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        $sidPtr = [System.Runtime.InteropServices.Marshal]::ReadIntPtr($infoBuffer)
        if ($sidPtr -eq [IntPtr]::Zero) {
            return $null
        }
        return ConvertTo-SidString -Sid $sidPtr
    }
    finally {
        if ($infoBuffer -ne [IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::FreeHGlobal($infoBuffer)
        }
        if ($hToken -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.JobObjectNative]::CloseHandle($hToken) | Out-Null
        }
        if ($hProcess -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.JobObjectNative]::CloseHandle($hProcess) | Out-Null
        }
    }
}

# Opens targetPid and returns $true/$false for IsProcessInJob(NULL). Throws
# on failure to open the process.
function Test-ProcessInAnyJob {
    param([int]$TargetPid)

    $hProcess = [IntPtr]::Zero
    try {
        $hProcess = [SandboxDiagnostics.JobObjectNative]::OpenProcess(
            [SandboxDiagnostics.JobObjectNative]::PROCESS_QUERY_LIMITED_INFORMATION,
            $false,
            $TargetPid)
        if ($hProcess -eq [IntPtr]::Zero) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        $inJob = $false
        if (-not [SandboxDiagnostics.JobObjectNative]::IsProcessInJob($hProcess, [IntPtr]::Zero, [ref]$inJob)) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }
        return $inJob
    }
    finally {
        if ($hProcess -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.JobObjectNative]::CloseHandle($hProcess) | Out-Null
        }
    }
}

function Write-JobAmbiguityWarning {
    Write-Host ""
    Write-Host "WARNING: 'InJob = True' only confirms membership in SOME job, not in the sandbox's own" -ForegroundColor Yellow
    Write-Host "resource-limiting job. On Windows 11, nearly every process is placed in an OS-managed job" -ForegroundColor Yellow
    Write-Host "by default, so this does NOT by itself confirm the app's specific CPU/memory limits are" -ForegroundColor Yellow
    Write-Host "active. -Moniker cannot disambiguate this: the sandbox package's own Job Object for a" -ForegroundColor Yellow
    Write-Host "container launch is unnamed, so there is no name to look up by moniker. For a definitive" -ForegroundColor Yellow
    Write-Host "answer, open Sysinternals Process Explorer, select the process, and check Properties > Job" -ForegroundColor Yellow
    Write-Host "tab for the actual applied limit values." -ForegroundColor Yellow
}

try {
    if ($PSBoundParameters.ContainsKey('ProcessId')) {
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) {
            throw "No running process with PID $ProcessId was found."
        }

        try {
            $inJob = Test-ProcessInAnyJob -TargetPid $ProcessId
        }
        catch {
            throw "Failed to check Job Object status for process $ProcessId ($($proc.ProcessName)): $($_.Exception.Message)"
        }

        if ($inJob) {
            Write-Host "Process $($proc.ProcessName) (PID $ProcessId) IS assigned to a Job Object." -ForegroundColor Green
            Write-JobAmbiguityWarning
            exit 0
        }
        else {
            Write-Host "Process $($proc.ProcessName) (PID $ProcessId) is NOT assigned to any Job Object." -ForegroundColor Yellow
            exit 1
        }
    }

    $expectedSidString = Get-ExpectedMonikerSidString -MonikerName $Moniker
    Write-Host "Searching running processes for moniker '$Moniker' (expected SID $expectedSidString)..."

    $allProcesses = Get-Process
    $matches = New-Object System.Collections.Generic.List[object]
    $skipped = 0

    foreach ($proc in $allProcesses) {
        try {
            $sidString = Get-ProcessAppContainerSidString -TargetPid $proc.Id
        }
        catch {
            $skipped++
            continue
        }

        if ($sidString -eq $expectedSidString) {
            $matches.Add($proc)
        }
    }

    Write-Host ""
    if ($matches.Count -eq 0) {
        Write-Host "No running process is confined under moniker '$Moniker' (skipped $skipped inaccessible process(es))." -ForegroundColor Yellow
        Write-Host "If the target process runs under another user session, re-run this script elevated to include it in the search." -ForegroundColor DarkGray
        exit 1
    }

    Write-Host "Found $($matches.Count) process(es) confirmed under moniker '$Moniker' (skipped $skipped inaccessible process(es)). Checking Job Object status for each:" -ForegroundColor Green

    $anyInJob = $false
    $results = New-Object System.Collections.Generic.List[object]
    foreach ($proc in $matches) {
        try {
            $inJob = Test-ProcessInAnyJob -TargetPid $proc.Id
        }
        catch {
            $inJob = $null
        }
        if ($inJob -eq $true) { $anyInJob = $true }
        $results.Add([pscustomobject]@{
            PID   = $proc.Id
            Name  = $proc.ProcessName
            InJob = if ($null -eq $inJob) { '<error>' } else { $inJob }
        })
    }
    $results | Format-Table -AutoSize | Out-Host

    if ($anyInJob) {
        Write-JobAmbiguityWarning
        exit 0
    }
    else {
        Write-Host "None of the matched processes are assigned to any Job Object." -ForegroundColor Yellow
        exit 1
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 2
}
