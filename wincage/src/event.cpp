#include "event.h"
#include <sstream>

// Own PID is included so the event name is unique per sandbox_host.exe
// launch, not just per (moniker, parent_pid) pair.
//
// Two concurrent launches of the same emulator by the same user share
// both moniker and parent_pid. Without this suffix they would build the
// identical event name and race on CreateEventW.
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
    // collision (ERROR_ALREADY_EXISTS) rather than failing. Two unrelated
    // launches would then both hold a handle to ONE kernel event, so
    // signaling it for one launch's exit would incorrectly unblock the
    // other's Python watcher too.
    //
    // The per-launch PID suffix above should make this practically
    // unreachable, but treat it as fatal rather than silently sharing the
    // object. Mirrors job.py's ERROR_ALREADY_EXISTS handling for the
    // equivalent job-naming case.
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
