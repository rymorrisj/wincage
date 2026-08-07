#include "job.h"
#include <string>

JobObject::~JobObject() {
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

    // Children inherit job membership and are killed when job closes.
    // BREAKAWAY_OK: allows child processes of the emulator to escape this job
    // by spawning with CREATE_BREAKAWAY_FROM_JOB.  This is NOT required for the
    // host's breakaway-retry logic (Python launcher.py or the C++ retry in
    // main.cpp) — those retry paths govern sandbox_host.exe escaping ITS OWN
    // parent job, not the emulator's children escaping this one.  BREAKAWAY_OK
    // here weakens job containment; candidate for removal if no emulator
    // sub-process is documented to require it.
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION eli = {};
    eli.BasicLimitInformation.LimitFlags =
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
        JOB_OBJECT_LIMIT_BREAKAWAY_OK;

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
        // Defense-in-depth: sandbox.py::_validate() already enforces this
        // exact 1-100 range before the JSON payload reaches this process,
        // but the WORD packing below has no bounds check of its own. An
        // out-of-range value here (corrupted config, future caller bypassing
        // sandbox.launch()) would silently produce a wrong CPU cap via
        // truncation instead of a loud failure, so it is rejected explicitly
        // rather than trusting the upstream validator alone.
        if (cfg.cpu_min_rate < 1 || cfg.cpu_min_rate > 100 ||
            cfg.cpu_max_rate < 1 || cfg.cpu_max_rate > 100) {
            return E_INVALIDARG;
        }

        JOBOBJECT_CPU_RATE_CONTROL_INFORMATION cpu = {};
#ifdef JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE
        // MIN_MAX_RATE requires Windows 10+ SDK headers.
        // Pack MinRate (low word) and MaxRate (high word) into the CpuRate field;
        // MinGW UCRT64 headers expose only CpuRate in the union.
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
        // BREAKAWAY_OK: see create() comment because the same caveat applies when
        // memory limits are re-applied via apply_limits().
        //
        // PROCESS_MEMORY, not JOB_MEMORY: this must match job.py's
        // set_memory_limit(), which caps memory_limit_mb per-process via
        // JOB_OBJECT_LIMIT_PROCESS_MEMORY on the non-container Job-Object-only
        // launch path. JOB_MEMORY caps the job's cumulative usage across every
        // process it contains instead of any single one, a different meaning
        // for the same config value; nothing here documented that divergence
        // as intentional, so the container path is aligned to the same
        // per-process semantics rather than left to drift.
        eli.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_PROCESS_MEMORY |
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
            JOB_OBJECT_LIMIT_BREAKAWAY_OK;
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
