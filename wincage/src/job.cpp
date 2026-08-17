#include "job.h"

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
        if (cfg.cpu_min_rate < 1 || cfg.cpu_min_rate > 100 ||
            cfg.cpu_max_rate < 1 || cfg.cpu_max_rate > 100) {
            return E_INVALIDARG;
        }

        JOBOBJECT_CPU_RATE_CONTROL_INFORMATION cpu = {};
#ifdef JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE
        // MIN_MAX_RATE requires Windows 10+ SDK headers; pack MinRate (low word) and
        // MaxRate (high word) into CpuRate, since MinGW UCRT64 headers expose only that field.
        cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE |
                           JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE;
        WORD min_w = static_cast<WORD>(cfg.cpu_min_rate * 100);
        WORD max_w = static_cast<WORD>(cfg.cpu_max_rate * 100);
        cpu.CpuRate = (static_cast<DWORD>(max_w) << 16) | static_cast<DWORD>(min_w);
#else
        // JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE isn't available in this MinGW
        // installation, so fall back to HARD_CAP; MinRate will not be enforced in this build.
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
        // KILL_ON_JOB_CLOSE flag must be repeated here or it's lost.
        //
        // PROCESS_MEMORY caps each process individually, matching job.py's native
        // path. JOB_MEMORY would cap the job's cumulative usage instead.
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
