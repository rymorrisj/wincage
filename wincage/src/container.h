#pragma once
#include <windows.h>
#include <string>
#include <userenv.h>

enum class ContainerResult {
    Created,
    AlreadyExists,
    Failed,
};

class AppContainer {
public:
    explicit AppContainer(const std::wstring& moniker);
    ~AppContainer();

    AppContainer(const AppContainer&) = delete;
    AppContainer& operator=(const AppContainer&) = delete;

    ContainerResult provision();
    HRESULT grant_window_station();
    HRESULT secure_existing_file(const std::wstring& path, DWORD access_mask);
    HRESULT grant_directory(const std::wstring& path, DWORD access_mask);

    // Computes sid_ from moniker_ without provisioning a profile. revoke has no
    // reason to create a profile just to compute the SID it's removing ACEs for.
    HRESULT derive_sid();
    HRESULT revoke_existing_file(const std::wstring& path);
    HRESULT revoke_directory(const std::wstring& path);

    PSID sid() const { return sid_; }

    static HRESULT reset(const std::wstring& moniker);

private:
    std::wstring moniker_;
    PSID sid_ = nullptr;
};
