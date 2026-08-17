#pragma once
#include <windows.h>
#include <string>

enum class EventResult {
    Created,
    Failed,
};

class SandboxEvent {
public:
    explicit SandboxEvent(const std::wstring& moniker, DWORD pid);
    ~SandboxEvent();

    SandboxEvent(const SandboxEvent&) = delete;
    SandboxEvent& operator=(const SandboxEvent&) = delete;

    EventResult create();
    // The GetLastError() behind a create() == Failed result. EventResult itself
    // can't carry it, and ERROR_ALREADY_EXISTS is not automatically the value
    // GetLastError() would still hold once main.cpp inspects it after create() returns.
    DWORD last_create_error() const { return last_create_error_; }
    HRESULT signal();
    const std::wstring& name() const { return name_; }
    HANDLE handle() const { return handle_; }

private:
    std::wstring name_;
    HANDLE handle_ = nullptr;
    DWORD last_create_error_ = 0;
};
