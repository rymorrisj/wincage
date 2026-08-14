#include "event.h"
#include <sstream>

// Own PID makes the event name unique per launch, not just per
// (moniker, parent_pid): two concurrent launches of the same target
// process share both, and without this suffix would race on CreateEventW
// for one identical name.
SandboxEvent::SandboxEvent(const std::wstring& moniker, DWORD pid) {
    std::wostringstream oss;
    oss << L"Local\\Sandbox_" << moniker << L"_" << pid << L"_" << GetCurrentProcessId();
    name_ = oss.str();
}

SandboxEvent::~SandboxEvent() {
    if (handle_) {
        CloseHandle(handle_);
        handle_ = nullptr;
    }
}

EventResult SandboxEvent::create() {
    SetLastError(0);
    handle_ = CreateEventW(
        nullptr,
        TRUE,   // manual reset
        FALSE,  // initially not signaled
        name_.c_str()
    );
    if (!handle_) return EventResult::Failed;

    // CreateEventW returns a handle to the EXISTING event on a name
    // collision instead of failing, so two unrelated launches could end up
    // sharing one kernel event and unblocking each other's Python watcher.
    //
    // The PID suffix above should make this unreachable in practice, but
    // this is treated as fatal rather than silently shared. Mirrors
    // job.py's ERROR_ALREADY_EXISTS handling for job names.
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(handle_);
        handle_ = nullptr;
        return EventResult::Failed;
    }
    return EventResult::Created;
}

HRESULT SandboxEvent::signal() {
    if (!handle_) return E_HANDLE;
    if (!SetEvent(handle_)) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    return S_OK;
}
