"""
Win32 ctypes structures and constants for wincage's Job Object layer.

- Structs map directly to the identically-named Win32 types.
- Constants are prefixed with a single underscore since they're private
  to this package.
- These values are normally available through pywin32; they're defined
  here directly instead, to avoid that dependency.

Refs:
https://pypi.org/project/pywin32/
https://learn.microsoft.com/en-us/windows/win32/ProcThread/processes-and-threads
"""

import ctypes
import ctypes.wintypes

# Avoids a hard dependency on pywin32 at module import time.
_CREATE_SUSPENDED = 0x00000004

# Lets the child process escape the launcher's own Job Object so it can be
# reassigned to ours; the launcher is often already in a job on Windows 11.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# LimitFlags values used in JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# ControlFlags values used in JOBOBJECT_CPU_RATE_CONTROL_INFORMATION.ControlFlags
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE        = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP      = 0x4   # requires Windows 8.1+
_JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE  = 0x10  # requires Windows 10 version 1607 (build 14393)+

# GetExitCodeProcess sentinel, process has not yet exited.
_STILL_ACTIVE = 259

# ResumeThread's documented failure value "(DWORD) -1" surfaces through
# this unsigned-DWORD restype as 0xFFFFFFFF, not Python's -1; compare here.
_RESUME_THREAD_FAILED = 0xFFFFFFFF

# STARTUPINFO dwFlags / wShowWindow values for foreground placement hints.
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOWNORMAL = 1


# ---------------------------------------------------------------------------
# Windows API structures
# ---------------------------------------------------------------------------

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        # ULONG_PTR is a pointer-sized bitmask, not an actual pointer to a
        # ULONG, so c_size_t is the correct mapping here, not POINTER(ULONG).
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.wintypes.ULARGE_INTEGER),
        ("WriteOperationCount", ctypes.wintypes.ULARGE_INTEGER),
        ("OtherOperationCount", ctypes.wintypes.ULARGE_INTEGER),
        ("ReadTransferCount", ctypes.wintypes.ULARGE_INTEGER),
        ("WriteTransferCount", ctypes.wintypes.ULARGE_INTEGER),
        ("OtherTransferCount", ctypes.wintypes.ULARGE_INTEGER),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    """Hard-cap variant only. CpuRate is the first DWORD of the union;
    with ``_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP`` set, it holds the
    per-interval CPU budget in units of 1/10,000 across all logical
    processors (10,000 == 100%).
    """
    _fields_ = [
        ("ControlFlags", ctypes.wintypes.DWORD),
        ("CpuRate",      ctypes.wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.wintypes.LARGE_INTEGER),
        ("TotalKernelTime", ctypes.wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", ctypes.wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", ctypes.wintypes.LARGE_INTEGER),
        ("TotalPageFaultCount", ctypes.wintypes.DWORD),
        ("TotalProcesses", ctypes.wintypes.DWORD),
        ("ActiveProcesses", ctypes.wintypes.DWORD),
        ("TotalTerminatedProcesses", ctypes.wintypes.DWORD),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ThreadID", ctypes.wintypes.DWORD),
        ("th32OwnerProcessID", ctypes.wintypes.DWORD),
        ("tpBasePri", ctypes.wintypes.LONG),
        ("tpDeltaPri", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb",              ctypes.wintypes.DWORD),
        ("lpReserved",      ctypes.wintypes.LPWSTR),
        ("lpDesktop",       ctypes.wintypes.LPWSTR),
        ("lpTitle",         ctypes.wintypes.LPWSTR),
        ("dwX",             ctypes.wintypes.DWORD),
        ("dwY",             ctypes.wintypes.DWORD),
        ("dwXSize",         ctypes.wintypes.DWORD),
        ("dwYSize",         ctypes.wintypes.DWORD),
        ("dwXCountChars",   ctypes.wintypes.DWORD),
        ("dwYCountChars",   ctypes.wintypes.DWORD),
        ("dwFillAttribute", ctypes.wintypes.DWORD),
        ("dwFlags",         ctypes.wintypes.DWORD),
        ("wShowWindow",     ctypes.wintypes.WORD),
        ("cbReserved2",     ctypes.wintypes.WORD),
        ("lpReserved2",     ctypes.c_char_p),
        ("hStdInput",       ctypes.wintypes.HANDLE),
        ("hStdOutput",      ctypes.wintypes.HANDLE),
        ("hStdError",       ctypes.wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess",    ctypes.wintypes.HANDLE),
        ("hThread",     ctypes.wintypes.HANDLE),
        ("dwProcessId", ctypes.wintypes.DWORD),
        ("dwThreadId",  ctypes.wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# kernel32 function signatures
# ---------------------------------------------------------------------------
#
# An undeclared ctypes function defaults to restype=c_int (32-bit signed).
# HANDLE is pointer-sized, so on Win64 that would truncate a returned
# HANDLE to its low 32 bits. This has only worked by luck: Windows
# guarantees kernel handles fit in 32 bits (a WOW64-interop requirement),
# not because these calls were declared correctly.
#
# ctypes.windll.kernel32 is a process-wide singleton and each function is
# cached on first access, so declaring argtypes/restype here, once, fixes
# every call site across the package (job.py, process.py,
# sandbox_process.py, sandbox.py) as long as this module is imported
# first. sandbox.py doesn't otherwise need win32_types; it imports it only
# for that side effect.
_kernel32 = ctypes.windll.kernel32
_wt = ctypes.wintypes

_kernel32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
_kernel32.OpenProcess.restype = _wt.HANDLE

_kernel32.OpenEventW.argtypes = [_wt.DWORD, _wt.BOOL, _wt.LPCWSTR]
_kernel32.OpenEventW.restype = _wt.HANDLE

_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, _wt.LPCWSTR]
_kernel32.CreateJobObjectW.restype = _wt.HANDLE

_kernel32.CloseHandle.argtypes = [_wt.HANDLE]
_kernel32.CloseHandle.restype = _wt.BOOL

_kernel32.TerminateProcess.argtypes = [_wt.HANDLE, _wt.UINT]
_kernel32.TerminateProcess.restype = _wt.BOOL

_kernel32.TerminateJobObject.argtypes = [_wt.HANDLE, _wt.UINT]
_kernel32.TerminateJobObject.restype = _wt.BOOL

_kernel32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
_kernel32.AssignProcessToJobObject.restype = _wt.BOOL

_kernel32.IsProcessInJob.argtypes = [_wt.HANDLE, _wt.HANDLE, ctypes.POINTER(_wt.BOOL)]
_kernel32.IsProcessInJob.restype = _wt.BOOL

_kernel32.QueryInformationJobObject.argtypes = [
    _wt.HANDLE, _wt.DWORD, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_wt.DWORD),
]
_kernel32.QueryInformationJobObject.restype = _wt.BOOL

_kernel32.SetInformationJobObject.argtypes = [
    _wt.HANDLE, _wt.DWORD, ctypes.c_void_p, _wt.DWORD,
]
_kernel32.SetInformationJobObject.restype = _wt.BOOL

_kernel32.GetExitCodeProcess.argtypes = [_wt.HANDLE, ctypes.POINTER(_wt.DWORD)]
_kernel32.GetExitCodeProcess.restype = _wt.BOOL

_kernel32.WaitForSingleObject.argtypes = [_wt.HANDLE, _wt.DWORD]
_kernel32.WaitForSingleObject.restype = _wt.DWORD

_kernel32.ResumeThread.argtypes = [_wt.HANDLE]
_kernel32.ResumeThread.restype = _wt.DWORD

_kernel32.CreateProcessW.argtypes = [
    _wt.LPCWSTR, _wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    _wt.BOOL, _wt.DWORD, ctypes.c_void_p, _wt.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
]
_kernel32.CreateProcessW.restype = _wt.BOOL

_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = _wt.DWORD

_kernel32.SetLastError.argtypes = [_wt.DWORD]
_kernel32.SetLastError.restype = None
