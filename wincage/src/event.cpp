#include "event.h"
#include <sstream>

// Own PID makes the event name unique per launch; two concurrent launches of
// the same target would otherwise share (moniker, parent_pid) and race on CreateEventW.
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
    if (!handle_) {
        last_create_error_ = GetLastError();
        return EventResult::Failed;
    }

    // CreateEventW returns the EXISTING event on a name collision instead of
    // failing, which could let two unrelated launches share one kernel event and
    // unblock each other's watcher. The PID suffix above should make this
    // unreachable, but it's treated as fatal rather than silently shared.
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        last_create_error_ = ERROR_ALREADY_EXISTS;
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
