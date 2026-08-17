#include "container.h"
#include <sddl.h>
#include <aclapi.h>
#include <cwchar>
#include <stdexcept>
#include <vector>

namespace {

// True if acl already carries an ACCESS_ALLOWED ACE for sid granting at least
// access_mask with both OBJECT_INHERIT_ACE and CONTAINER_INHERIT_ACE set.
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

// The container gets enough access to create/draw its own window and receive
// its own input, but not hooks, journal record/playback, switch desktop,
// clipboard, or screen capture, since those reach every process on the desktop.
constexpr DWORD kDesktopAccessMask =
    DESKTOP_CREATEWINDOW | DESKTOP_CREATEMENU | DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS;
constexpr DWORD kWindowStationAccessMask =
    WINSTA_ENUMDESKTOPS | WINSTA_READATTRIBUTES | WINSTA_ACCESSGLOBALATOMS;

// An ADS on path itself, so no separate storage location is needed and it
// won't appear in a normal directory listing.
const wchar_t* kGrantMarkerSuffix = L":wincage.pending";

bool grant_marker_present(const std::wstring& path) {
    return GetFileAttributesW((path + kGrantMarkerSuffix).c_str()) != INVALID_FILE_ATTRIBUTES;
}

void write_grant_marker(const std::wstring& path) {
    HANDLE h = CreateFileW((path + kGrantMarkerSuffix).c_str(), GENERIC_WRITE, 0,
                            nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
}

void clear_grant_marker(const std::wstring& path) {
    DeleteFileW((path + kGrantMarkerSuffix).c_str());
}

DWORD apply_ace_to_node(const std::wstring& path, EXPLICIT_ACCESS_W& ea) {
    PACL existing_acl = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;
    DWORD err = GetNamedSecurityInfoW(
        path.c_str(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        nullptr, nullptr, &existing_acl, nullptr, &sd);
    if (err != ERROR_SUCCESS) return err;

    PACL new_acl = nullptr;
    err = SetEntriesInAclW(1, &ea, existing_acl, &new_acl);
    if (sd) LocalFree(sd);
    if (err != ERROR_SUCCESS) return err;

    err = SetNamedSecurityInfoW(
        const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION, nullptr, nullptr, new_acl, nullptr);
    if (new_acl) LocalFree(new_acl);
    return err;
}

// TreeSetNamedSecurityInfoW can't be told to stop at a junction or symlink,
// so it would propagate this ACE across it
DWORD grant_tree_skip_reparse_points(const std::wstring& path, EXPLICIT_ACCESS_W& ea) {
    DWORD err = apply_ace_to_node(path, ea);
    if (err != ERROR_SUCCESS) return err;

    WIN32_FIND_DATAW find_data = {};
    HANDLE find_handle = FindFirstFileW((path + L"\\*").c_str(), &find_data);
    if (find_handle == INVALID_HANDLE_VALUE) {
        err = GetLastError();
        // An empty directory reports ERROR_FILE_NOT_FOUND here, not success.
        return (err == ERROR_FILE_NOT_FOUND) ? ERROR_SUCCESS : err;
    }

    do {
        const wchar_t* name = find_data.cFileName;
        if (wcscmp(name, L".") == 0 || wcscmp(name, L"..") == 0) continue;

        // A junction/symlink here can point anywhere on disk, so crossing
        // it would extend this grant beyond what the caller asked to broker.
        if (find_data.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) continue;

        std::wstring child_path = path + L"\\" + name;
        err = (find_data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            ? grant_tree_skip_reparse_points(child_path, ea)
            : apply_ace_to_node(child_path, ea);
    } while (err == ERROR_SUCCESS && FindNextFileW(find_handle, &find_data));

    if (err == ERROR_SUCCESS) {
        DWORD last_err = GetLastError();
        if (last_err != ERROR_NO_MORE_FILES) err = last_err;
    }
    FindClose(find_handle);
    return err;
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
    // across launches for the same moniker.
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

        if (!dacl_present) {
            // A null DACL means everyone already has full access; replacing
            // it with just our grant would lock everyone else out.
            return E_UNEXPECTED;
        }

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

    // Neither handle below is closed: GetProcessWindowStation and GetThreadDesktop
    // return the caller's existing objects. Closing them would tear down this process's own station.
    HWINSTA hwinsta = GetProcessWindowStation();
    if (!hwinsta) return HRESULT_FROM_WIN32(GetLastError());
    HRESULT hr = grant_obj(hwinsta, kWindowStationAccessMask);
    if (FAILED(hr)) return hr;

    HDESK hdesk = GetThreadDesktop(GetCurrentThreadId());
    if (!hdesk) return HRESULT_FROM_WIN32(GetLastError());
    return grant_obj(hdesk, kDesktopAccessMask);
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

    // A prior launch may have already granted this exact ACE (same SID, mask,
    // inheritance flags) to the whole tree; skip the expensive walk once the
    // root node shows it already took.
    //
    // A marker from a prior interrupted walk means the root's ACE alone
    // doesn't prove the full tree is granted, so skip the fast path then.
    bool already_granted = !grant_marker_present(path)
        && has_full_inheritable_ace(existing_acl, sid_, access_mask);
    if (sd) LocalFree(sd);
    if (already_granted) return S_OK;

    EXPLICIT_ACCESS_W ea = {};
    ea.grfAccessPermissions = access_mask;
    ea.grfAccessMode        = GRANT_ACCESS;
    ea.grfInheritance       = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE;
    ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName    = reinterpret_cast<LPWSTR>(sid_);

    // Written before the walk starts and cleared only on success, so a
    // crash mid-walk leaves it behind for the check above to catch.
    write_grant_marker(path);
    err = grant_tree_skip_reparse_points(path, ea);
    if (err == ERROR_SUCCESS) clear_grant_marker(path);

    return HRESULT_FROM_WIN32(err);
}

HRESULT AppContainer::reset(const std::wstring& moniker) {
    return DeleteAppContainerProfile(moniker.c_str());
}

HRESULT AppContainer::derive_sid() {
    return DeriveAppContainerSidFromAppContainerName(moniker_.c_str(), &sid_);
}

namespace {

// True if acl carries any ACCESS_ALLOWED ACE for sid, regardless of mask or
// inheritance flags.
bool has_ace_for_sid(PACL acl, PSID sid) {
    if (!acl || !sid) return false;

    ACL_SIZE_INFORMATION size_info = {};
    if (!GetAclInformation(acl, &size_info, sizeof(size_info), AclSizeInformation))
        return false;

    for (DWORD i = 0; i < size_info.AceCount; ++i) {
        LPVOID ace_ptr = nullptr;
        if (!GetAce(acl, i, &ace_ptr)) continue;

        auto* header = static_cast<ACE_HEADER*>(ace_ptr);
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE) continue;

        auto* ace = reinterpret_cast<ACCESS_ALLOWED_ACE*>(ace_ptr);
        PSID ace_sid = reinterpret_cast<PSID>(&ace->SidStart);
        if (EqualSid(ace_sid, sid)) return true;
    }
    return false;
}

}  // namespace

HRESULT AppContainer::revoke_existing_file(const std::wstring& path) {
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
    // A path that's already gone has nothing left to revoke.
    if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) return S_OK;
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    EXPLICIT_ACCESS_W ea = {};
    // grfAccessPermissions is left zero: SetEntriesInAclW ignores it when
    // grfAccessMode is REVOKE_ACCESS, it removes every ACE for the trustee.
    ea.grfAccessMode        = REVOKE_ACCESS;
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

HRESULT AppContainer::revoke_directory(const std::wstring& path) {
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
    if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND) return S_OK;
    if (err != ERROR_SUCCESS) return HRESULT_FROM_WIN32(err);

    // A marker left behind by an interrupted walk, grant's or revoke's, means
    // the root's current ACE state doesn't prove the whole tree matches it,
    // so the fast path below must not trust it.
    bool already_revoked = !grant_marker_present(path)
        && !has_ace_for_sid(existing_acl, sid_);
    if (sd) LocalFree(sd);
    if (already_revoked) return S_OK;

    EXPLICIT_ACCESS_W ea = {};
    ea.grfAccessMode        = REVOKE_ACCESS;
    ea.grfInheritance       = NO_INHERITANCE;
    ea.Trustee.TrusteeForm  = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType  = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName    = reinterpret_cast<LPWSTR>(sid_);

    // grant_tree_skip_reparse_points is generic over the ACE it applies;
    // REVOKE_ACCESS above is what makes this a revoke, not a grant.
    write_grant_marker(path);
    err = grant_tree_skip_reparse_points(path, ea);
    if (err == ERROR_SUCCESS) clear_grant_marker(path);

    return HRESULT_FROM_WIN32(err);
}
