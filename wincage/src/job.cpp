#include "job.h"
#include <string>

JobObject::~JobObject() {
    // Closing this triggers JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, which kills
    // any process still in the job, e.g. the target if the parent died first.
    if (handle_) {
        CloseHandle(handle_);
        handle_ = nullptr;
    }
}

HRESULT JobObject::create() {
    handle_ = CreateJobObjectW(nullptr, nullptr);
    if (!handle_) {
        return HRESULT_FROM_WIN32(GetLastError());
    }

    // Children inherit job membership and are killed when the job closes.
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION eli = {};
    eli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

    if (!SetInformationJobObject(handle_,
            JobObjectExtendedLimitInformation,
            &eli, sizeof(eli))) {
        return HRESULT_FROM_WIN32(GetLastError());
    }

    return S_OK;
}

HRESULT JobObject::apply_limits(const JobConfig& cfg) {
    if (!handle_) return E_HANDLE;

    if (!cfg.skip_cpu_limit) {
        // sandbox.py::_validate() already enforces this range, but the WORD
        // packing below has no bounds check of its own: an out-of-range value
        // reaching here would truncate into a wrong CPU cap instead of failing.
        //
        // _validate() also rejects cpu_min_rate > cpu_max_rate. That is not
        // re-checked here.
        if (cfg.cpu_min_rate < 1 || cfg.cpu_min_rate > 100 ||
            cfg.cpu_max_rate < 1 || cfg.cpu_max_rate > 100) {
            return E_INVALIDARG;
        }

        JOBOBJECT_CPU_RATE_CONTROL_INFORMATION cpu = {};
#ifdef JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE
        // MIN_MAX_RATE requires Windows 10+ SDK headers.
        // Pack MinRate (low word) and MaxRate (high word) into the CpuRate
        // field. MinGW UCRT64 headers expose only CpuRate in the union.
        cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE |
                           JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE;
        WORD min_w = static_cast<WORD>(cfg.cpu_min_rate * 100);
        WORD max_w = static_cast<WORD>(cfg.cpu_max_rate * 100);
        cpu.CpuRate = (static_cast<DWORD>(max_w) << 16) | static_cast<DWORD>(min_w);
#else
        // JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE is not available in this MinGW
        // installation (requires Windows 10+ SDK headers). Fall back to HARD_CAP.
        // MinRate will not be enforced in this build; only MaxRate (cpu_max_rate) applies.
#pragma message("sandbox: JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE unavailable; HARD_CAP fallback active, MinRate scheduling floor will not be enforced")
        cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE |
                           JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP;
        cpu.CpuRate = cfg.cpu_max_rate * 100;
#endif

        if (!SetInformationJobObject(handle_,
                JobObjectCpuRateControlInformation,
                &cpu, sizeof(cpu))) {
            return HRESULT_FROM_WIN32(GetLastError());
        }
    }

    if (!cfg.skip_memory_limit && cfg.memory_limit_bytes > 0) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION eli = {};
        // This call replaces the whole extended-limit structure, so create()'s
        // flags must be repeated here or they are lost.
        //
        // PROCESS_MEMORY to match job.py's set_memory_limit()
        // on the Job-Object-only launch path. memory_limit_mb caps each
        // process individually. JOB_MEMORY would cap the job's cumulative
        // usage instead
        eli.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_PROCESS_MEMORY |
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        eli.ProcessMemoryLimit = cfg.memory_limit_bytes;

        if (!SetInformationJobObject(handle_,
                JobObjectExtendedLimitInformation,
                &eli, sizeof(eli))) {
            return HRESULT_FROM_WIN32(GetLastError());
        }
    }

    return S_OK;
}

HRESULT JobObject::assign(HANDLE process) {
    if (!handle_) return E_HANDLE;
    if (!AssignProcessToJobObject(handle_, process)) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    return S_OK;
}
