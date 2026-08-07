"""
Win32 ctypes structures and constants for wincage's Job Object isolation layer.

All structs map directly to the identically-named Win32 types.  Constants are
prefixed with a single underscore to mark them as internal to the isolation
subsystem.
"""

import ctypes
import ctypes.wintypes

# Win32 CREATE_SUSPENDED flag, avoids a hard dependency on pywin32 at module import time.
_CREATE_SUSPENDED = 0x00000004

# CREATE_BREAKAWAY_FROM_JOB: child process escapes the parent's Job Object so
# it can be cleanly assigned to our own.  Used when the launcher is already
# inside a Windows Job Object (common on Windows 11).
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

# ResumeThread failure sentinel. MSDN documents this as "(DWORD) -1"; with
# ResumeThread's restype declared as the unsigned DWORD it actually returns
# (see the kernel32 function signatures below), ctypes surfaces that bit
# pattern as 0xFFFFFFFF, not Python's -1. Comparing against this constant
# instead of -1 keeps that comparison correct now that the ctypes-default
# signed-int restype is no longer silently reinterpreting it.
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
        # Win32's ULONG_PTR Affinity is a pointer-sized bitmask value, not an
        # actual pointer to a ULONG. c_size_t is the correct ctypes mapping
        # for ULONG_PTR; POINTER(ULONG) previously declared it as a real
        # pointer type, which is the wrong shape even though no code in this
        # package reads or writes this field today.
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
    """Maps to the Win32 structure of the same name (hard-cap variant).

    ``CpuRate`` occupies the first DWORD of the union field.  When
    ``_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP`` is set in ``ControlFlags``,
    this field holds the per-scheduling-interval CPU budget expressed as
    cycles per 10,000 across all logical processors (10,000 == 100%).
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
    """Maps to the Win32 STARTUPINFOW structure."""
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
    """Maps to the Win32 PROCESS_INFORMATION structure."""
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
# An undeclared ctypes foreign function defaults to argtypes=None (arguments
# converted by ctypes' best guess from the Python type) and restype=c_int
# (32-bit signed). HANDLE is pointer-sized; on Win64 a c_int restype
# silently truncates a returned HANDLE to its low 32 bits. This has worked
# by accident so far only because Windows guarantees kernel object handle
# values fit in 32 bits (a documented WOW64-interop requirement), not
# because the calls were actually declared correctly.
#
# ctypes.windll.kernel32 is a process-wide singleton (LibraryLoader caches
# one WinDLL instance per name), and each named function is itself cached as
# an attribute on first access. Declaring argtypes/restype here, once, at
# import time therefore fixes every ctypes.windll.kernel32.X(...) call site
# across this package (sandbox/job.py, sandbox/process.py,
# sandbox/sandbox_process.py, sandbox/sandbox.py) without changing any of
# those call sites, as long as this module has been imported first.
# sandbox/sandbox.py does not otherwise depend on win32_types, so it imports
# this module explicitly for that side effect.
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
