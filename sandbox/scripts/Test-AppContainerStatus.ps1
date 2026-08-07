<#
.SYNOPSIS
    Finds and/or confirms which running process(es) are AppContainer-confined
    under a specific sandbox package moniker.

.DESCRIPTION
    Test-AppContainerStatus derives the AppContainer SID that -Moniker would
    produce, using the same Win32 call the sandbox package itself uses
    (DeriveAppContainerSidFromAppContainerName, see ../src/container.cpp,
    AppContainer::provision()), then compares it against running processes'
    actual token AppContainer SID (TokenAppContainerSid).

    Two modes:

      Discovery mode (default, no -ProcessId): enumerates every running
      process, opens each one's token, and reports every process whose
      AppContainer SID matches -Moniker's expected SID: PID, name, path.
      Processes that cannot be opened (access denied, already exited between
      enumeration and check) are skipped and counted, not treated as errors,
      that is expected for processes owned by other users or protected by the
      OS. There is no reliable way to pre-filter this enumeration by process
      name: a moniker names an AppContainer profile, not an executable, and
      nothing ties the two together in advance, so every running process has
      to be checked. The per-process check is cheap (OpenProcess,
      OpenProcessToken, one GetTokenInformation call), so a full enumeration
      is the correct, simple approach for a diagnostic tool even on a system
      with a few hundred processes.

      Single-process mode (-ProcessId supplied): skips enumeration entirely
      and checks only that one process, faster and narrower when you already
      know which PID you want confirmed. Reports a definitive three-way
      result: confirmed match, AppContainer-confined but under a *different*
      moniker (both SIDs printed), or not AppContainer-confined at all.

.PARAMETER Moniker
    The sandbox package moniker to search or verify against, the same string
    passed as SandboxConfig.moniker when the process was launched (see the
    moniker section of ../README.md).

.PARAMETER ProcessId
    Optional. The process ID (PID) of a specific running process to check
    directly, skipping discovery across all running processes.

.EXAMPLE
    .\Test-AppContainerStatus.ps1 -Moniker "MyApp.worker"

    Enumerates all running processes and reports every one confined under the
    AppContainer profile for moniker "MyApp.worker".

.EXAMPLE
    .\Test-AppContainerStatus.ps1 -Moniker "MyApp.worker" -ProcessId 18432

    Checks only PID 18432 against moniker "MyApp.worker", no enumeration.

.OUTPUTS
    None. Writes a human-readable result to the host and sets $LASTEXITCODE.

.NOTES
    Exit codes:
      0  Discovery mode: at least one running process matched -Moniker.
         Single-process mode: the specified process matched -Moniker.
      1  Discovery mode: no running process matched -Moniker.
         Single-process mode: the specified process is either not
         AppContainer-confined at all, or confined under a different moniker.
         The output distinguishes the two cases.
      2  The check could not be completed (moniker SID could not be derived,
         the specified -ProcessId was not found, or another error). See the
         error message for detail.

    Opening another user's process token typically requires an elevated
    (Run as Administrator) PowerShell session; processes that cannot be
    opened are skipped in discovery mode rather than failing the whole run.
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

if (-not ('SandboxDiagnostics.AppContainerNative' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace SandboxDiagnostics
{
    public static class AppContainerNative
    {
        public const int PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        public const int TOKEN_QUERY = 0x0008;
        public const int TokenAppContainerSid = 31;

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(int dwDesiredAccess, bool bInheritHandle, int dwProcessId);

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
        if (-not [SandboxDiagnostics.AppContainerNative]::ConvertSidToStringSidW($Sid, [ref]$strPtr)) {
            return $null
        }
        return [System.Runtime.InteropServices.Marshal]::PtrToStringUni($strPtr)
    }
    finally {
        if ($strPtr -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.AppContainerNative]::LocalFree($strPtr) | Out-Null
        }
    }
}

function Get-ExpectedMonikerSidString {
    param([string]$MonikerName)

    $derivedSid = [IntPtr]::Zero
    try {
        $hr = [SandboxDiagnostics.AppContainerNative]::DeriveAppContainerSidFromAppContainerName($MonikerName, [ref]$derivedSid)
        if ($hr -ne 0) {
            throw "DeriveAppContainerSidFromAppContainerName failed for moniker '$MonikerName' (HRESULT 0x$($hr.ToString('X8')))."
        }
        return ConvertTo-SidString -Sid $derivedSid
    }
    finally {
        if ($derivedSid -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.AppContainerNative]::FreeSid($derivedSid) | Out-Null
        }
    }
}

# Queries a single process's token AppContainer SID as a string, or $null if
# the process is not AppContainer-confined. Throws on any failure to open the
# process/token; callers in discovery mode catch this and skip-and-count
# rather than aborting the whole enumeration.
function Get-ProcessAppContainerSidString {
    param([int]$TargetPid)

    $hProcess = [IntPtr]::Zero
    $hToken = [IntPtr]::Zero
    $infoBuffer = [IntPtr]::Zero

    try {
        $hProcess = [SandboxDiagnostics.AppContainerNative]::OpenProcess(
            [SandboxDiagnostics.AppContainerNative]::PROCESS_QUERY_LIMITED_INFORMATION,
            $false,
            $TargetPid)
        if ($hProcess -eq [IntPtr]::Zero) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        if (-not [SandboxDiagnostics.AppContainerNative]::OpenProcessToken(
                $hProcess, [SandboxDiagnostics.AppContainerNative]::TOKEN_QUERY, [ref]$hToken)) {
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
        [SandboxDiagnostics.AppContainerNative]::GetTokenInformation(
            $hToken,
            [SandboxDiagnostics.AppContainerNative]::TokenAppContainerSid,
            [IntPtr]::Zero,
            0,
            [ref]$requiredLength) | Out-Null

        if ($requiredLength -le 0) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        $infoBuffer = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($requiredLength)
        $actualLength = 0
        $gotInfo = [SandboxDiagnostics.AppContainerNative]::GetTokenInformation(
            $hToken,
            [SandboxDiagnostics.AppContainerNative]::TokenAppContainerSid,
            $infoBuffer,
            $requiredLength,
            [ref]$actualLength)

        if (-not $gotInfo) {
            throw (New-Object System.ComponentModel.Win32Exception([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))
        }

        # First IntPtr.Size bytes of the buffer are the TokenAppContainer
        # field; on a confined process it points at the SID bytes appended
        # right after it in this same allocation, on an unconfined process
        # it is IntPtr.Zero. Convert to a string SID before this function
        # returns and its finally block frees infoBuffer.
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
            [SandboxDiagnostics.AppContainerNative]::CloseHandle($hToken) | Out-Null
        }
        if ($hProcess -ne [IntPtr]::Zero) {
            [SandboxDiagnostics.AppContainerNative]::CloseHandle($hProcess) | Out-Null
        }
    }
}

try {
    $expectedSidString = Get-ExpectedMonikerSidString -MonikerName $Moniker

    if ($PSBoundParameters.ContainsKey('ProcessId')) {
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) {
            throw "No running process with PID $ProcessId was found."
        }

        try {
            $actualSidString = Get-ProcessAppContainerSidString -TargetPid $ProcessId
        }
        catch {
            throw "Failed to open process $ProcessId ($($proc.ProcessName)): $($_.Exception.Message)"
        }

        if ($null -eq $actualSidString) {
            Write-Host "Process $($proc.ProcessName) (PID $ProcessId) is NOT running under any AppContainer." -ForegroundColor Yellow
            exit 1
        }
        elseif ($actualSidString -eq $expectedSidString) {
            Write-Host "Confirmed: process $($proc.ProcessName) (PID $ProcessId) is running under the AppContainer profile for moniker '$Moniker'." -ForegroundColor Green
            exit 0
        }
        else {
            Write-Host "Process $($proc.ProcessName) (PID $ProcessId) IS running under an AppContainer, but NOT the one for moniker '$Moniker'." -ForegroundColor Yellow
            Write-Host "  Actual token SID:            $actualSidString"
            Write-Host "  Expected SID for '$Moniker': $expectedSidString"
            exit 1
        }
    }

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
            $matches.Add([pscustomobject]@{
                PID  = $proc.Id
                Name = $proc.ProcessName
                Path = $proc.Path
            })
        }
    }

    Write-Host ""
    if ($matches.Count -gt 0) {
        Write-Host "Found $($matches.Count) process(es) confirmed under moniker '$Moniker' (skipped $skipped inaccessible process(es)):" -ForegroundColor Green
        $matches | Format-Table -AutoSize | Out-Host
        exit 0
    }
    else {
        Write-Host "No running process is confined under moniker '$Moniker' (skipped $skipped inaccessible process(es))." -ForegroundColor Yellow
        Write-Host "If the target process runs under another user session, re-run this script elevated to include it in the search." -ForegroundColor DarkGray
        exit 1
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 2
}
