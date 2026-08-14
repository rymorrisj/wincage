#include "container.h"
#include <sddl.h>
#include <aclapi.h>
#include <stdexcept>
#include <vector>

namespace {

// True if acl already carries an ACCESS_ALLOWED ACE for sid granting at
// least access_mask, with both OBJECT_INHERIT_ACE and CONTAINER_INHERIT_ACE
// set. grant_directory uses this to skip its recursive tree walk once a
// prior launch has already propagated the grant.
bool has_full_inheritable_ace(PACL acl, PSID sid, DWORD access_mask) {
    if (!acl || !sid) return false;

    ACL_SIZE_INFORMATION size_info = {};
    if (!GetAclInformation(acl, &size_info, sizeof(size_info), AclSizeInformation))
        return false;

    constexpr DWORD kNeededFlags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE;

    for (DWORD i = 0; i < size_info.AceCount; ++i) {
        LPVOID ace_ptr = nullptr;
        if (!GetAce(acl, i, &ace_ptr)) continue;

        auto* header = static_cast<ACE_HEADER*>(ace_ptr);
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE) continue;

        auto* ace = reinterpret_cast<ACCESS_ALLOWED_ACE*>(ace_ptr);
        PSID ace_sid = reinterpret_cast<PSID>(&ace->SidStart);
        if (!EqualSid(ace_sid, sid)) continue;

        if ((header->AceFlags & kNeededFlags) != kNeededFlags) continue;
        if ((ace->Mask & access_mask) != access_mask) continue;

        return true;
    }
    return false;
}

}  // namespace

AppContainer::AppContainer(const std::wstring& moniker)
    : moniker_(moniker) {}

AppContainer::~AppContainer() {
    if (sid_) {
        FreeSid(sid_);
        sid_ = nullptr;
    }
    // Intentionally never calls DeleteAppContainerProfile. Profile is stable
    // across launches for the same emulator moniker.
}

ContainerResult AppContainer::provision() {
    HRESULT hr = CreateAppContainerProfile(
        moniker_.c_str(),
        moniker_.c_str(),
        moniker_.c_str(),
        nullptr, 0,
        &sid_
    );

    if (SUCCEEDED(hr)) {
        return ContainerResult::Created;
    }

    if (hr == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)) {
        hr = DeriveAppContainerSidFromAppContainerName(moniker_.c_str(), &sid_);
        if (SUCCEEDED(hr)) {
            return ContainerResult::AlreadyExists;
        }
    }

    return ContainerResult::Failed;
}

HRESULT AppContainer::grant_window_station() {
    if (!sid_) return E_POINTER;

    auto grant_obj = [this](HANDLE obj, DWORD mask) -> HRESULT {
        SECURITY_INFORMATION si = DACL_SECURITY_INFORMATION;
        DWORD needed = 0;
        GetUserObjectSecurity(obj, &si, nullptr, 0, &needed);

        std::vector<BYTE> sd_buf(needed);
        if (!GetUserObjectSecurity(obj, &si,
                reinterpret_cast<PSECURITY_DESCRIPTOR>(sd_buf.data()),
                needed, &needed))
            return HRESULT_FROM_WIN32(GetLastError());

        BOOL dacl_present = FALSE, dacl_defaulted = FALSE;
        PACL existing_acl = nullptr;
        if (!GetSecurityDescriptorDacl(
                reinterpret_cast<PSECURITY_DESCRIPTOR>(sd_buf.data()),
                &dacl_present, &existing_acl, &dacl_defaulted))
            return HRESULT_FROM_WIN32(GetLastError());

        EXPLICIT_ACCESS_W ea = {};
        ea.grfAccessPermissions = mask;
        ea.grfAccessMode        = GRANT_ACCESS;
        ea.grfInheritance       = NO_INHERITANCE;
        ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
        ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
        ea.Trustee.ptstrName    = reinterpret_cast<LPWSTR>(sid_);

        PACL new_acl = nullptr;
        DWORD err = SetEntriesInAclW(1, &ea, existing_acl, &new_acl);
        if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

        SECURITY_DESCRIPTOR new_sd;
        InitializeSecurityDescriptor(&new_sd, SECURITY_DESCRIPTOR_REVISION);
        SetSecurityDescriptorDacl(&new_sd, TRUE, new_acl, FALSE);

        BOOL ok = SetUserObjectSecurity(obj, &si, &new_sd);
        LocalFree(new_acl);
        return ok ? S_OK : HRESULT_FROM_WIN32(GetLastError());
    };

    // Neither handle below is closed. GetProcessWindowStation and
    // GetThreadDesktop return the caller's existing objects, not new
    // references, so closing them would tear down this process's own station.
    HWINSTA hwinsta = GetProcessWindowStation();
    if (!hwinsta) return HRESULT_FROM_WIN32(GetLastError());
    HRESULT hr = grant_obj(hwinsta, 0x0000037F); // WINSTA_ALL_ACCESS
    if (FAILED(hr)) return hr;

    HDESK hdesk = GetThreadDesktop(GetCurrentThreadId());
    if (!hdesk) return HRESULT_FROM_WIN32(GetLastError());
    return grant_obj(hdesk, 0x000001FF); // DESKTOP_ALL_ACCESS
}

HRESULT AppContainer::secure_existing_file(const std::wstring& path, DWORD access_mask) {
    if (!sid_) return E_POINTER;

    PACL existing_acl = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;

    DWORD err = GetNamedSecurityInfoW(
        path.c_str(),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        nullptr, nullptr,
        &existing_acl, nullptr,
        &sd
    );
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    EXPLICIT_ACCESS_W ea = {};
    ea.grfAccessPermissions = access_mask;
    ea.grfAccessMode        = GRANT_ACCESS;
    ea.grfInheritance       = NO_INHERITANCE;
    ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName    = reinterpret_cast<LPWSTR>(sid_);

    PACL new_acl = nullptr;
    err = SetEntriesInAclW(1, &ea, existing_acl, &new_acl);
    if (sd) LocalFree(sd);
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    err = SetNamedSecurityInfoW(
        const_cast<LPWSTR>(path.c_str()),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        nullptr, nullptr,
        new_acl, nullptr
    );
    if (new_acl) LocalFree(new_acl);

    return HRESULT_FROM_WIN32(err);
}

HRESULT AppContainer::grant_directory(const std::wstring& path, DWORD access_mask) {
    // TODO: per-container ACEs accumulate on shared grant dirs and are never
    // removed, including when the container profile is deleted.
    if (!sid_) return E_POINTER;

    PACL existing_acl = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;

    DWORD err = GetNamedSecurityInfoW(
        path.c_str(),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        nullptr, nullptr,
        &existing_acl, nullptr,
        &sd
    );
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    // A prior launch may have already granted and propagated this exact ACE
    // (same SID, same access_mask, same inheritance flags) to path and
    // everything under it.
    //
    // TreeSetNamedSecurityInfoW below walks and rewrites every
    // file/directory ACL under path unconditionally. That's expensive on
    // large trees, so skip it once the root node shows the grant already
    // took.
    if (has_full_inheritable_ace(existing_acl, sid_, access_mask)) {
        if (sd) LocalFree(sd);
        return S_OK;
    }

    EXPLICIT_ACCESS_W ea = {};
    ea.grfAccessPermissions = access_mask;
    ea.grfAccessMode        = GRANT_ACCESS;
    ea.grfInheritance       = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE;
    ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName    = reinterpret_cast<LPWSTR>(sid_);

    PACL new_acl = nullptr;
    err = SetEntriesInAclW(1, &ea, existing_acl, &new_acl);
    if (sd) LocalFree(sd);
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    // A plain SetNamedSecurityInfoW only writes the DACL on this directory
    // node. Windows applies OBJECT_INHERIT_ACE/CONTAINER_INHERIT_ACE to
    // files created AFTER this call, but never retroactively to
    // files/subfolders that already exist under path (saves or config
    // dropped in before the AppContainer grant first ran, for example).
    //
    // TreeSetNamedSecurityInfoW instead:
    // - Walks the existing tree and propagates the new inheritable ACE to
    //   what's already there.
    // - Uses TREE_SEC_INFO_SET, which preserves any other explicit ACEs
    //   already present on child objects instead of clobbering them.
    err = TreeSetNamedSecurityInfoW(
        const_cast<LPWSTR>(path.c_str()),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        nullptr, nullptr,
        new_acl, nullptr,
        TREE_SEC_INFO_SET,
        nullptr,
        ProgressInvokeNever,
        nullptr
    );
    if (new_acl) LocalFree(new_acl);

    return HRESULT_FROM_WIN32(err);
}

HRESULT AppContainer::reset(const std::wstring& moniker) {
    return DeleteAppContainerProfile(moniker.c_str());
}
