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
    HRESULT signal();
    const std::wstring& name() const { return name_; }
    HANDLE handle() const { return handle_; }

private:
    std::wstring name_;
    HANDLE handle_ = nullptr;
};
