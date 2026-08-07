#pragma once
#include <windows.h>

struct JobConfig {
    DWORD cpu_max_rate;
    DWORD cpu_min_rate;
    SIZE_T memory_limit_bytes;
    bool skip_cpu_limit;
    bool skip_memory_limit;
};

class JobObject {
public:
    JobObject() = default;
    ~JobObject();

    JobObject(const JobObject&) = delete;
    JobObject& operator=(const JobObject&) = delete;

    HRESULT create();
    HRESULT apply_limits(const JobConfig& cfg);
    HRESULT assign(HANDLE process);

private:
    HANDLE handle_ = nullptr;
};
