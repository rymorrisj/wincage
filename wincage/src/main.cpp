#include <windows.h>
#include <sddl.h>
#include <string>
#include <vector>
#include <iostream>
#include <sstream>
#include <stdexcept>

#include "container.h"
#include "job.h"
#include "watchdog.h"
#include "event.h"

#include "json_parse.h"

static std::string hex32(DWORD v) {
    std::ostringstream oss;
    oss << "0x" << std::hex << v;
    return oss.str();
}

static std::wstring to_wide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    // Guards the `n - 1` below. On failure n is 0, and std::wstring(-1, ...)
    // reinterprets that as a huge size_t allocation request.
    //
    // dwFlags is 0, so malformed UTF-8 is silently replaced with U+FFFD
    // rather than failing. This is not an input validity check.
    //
    // Throwing here happens before any Win32 resource is allocated, so
    // main()'s catch has nothing to leak.
    if (n <= 0) {
        throw std::runtime_error(
            "MultiByteToWideChar (size query) failed (" + hex32(GetLastError()) + ")");
    }
    std::wstring w(n - 1, L'\0');
    if (MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, w.data(), n) == 0) {
        throw std::runtime_error(
            "MultiByteToWideChar (convert) failed (" + hex32(GetLastError()) + ")");
    }
    return w;
}

// Narrowing counterpart to to_wide(). Used for error messages that embed
// a path.
//
// sandbox.py json.loads()es the raw bytes, so a non-ASCII path must
// survive the trip back to Python as valid UTF-8 instead of being
// mangled by a wchar-to-char truncation.
static std::string to_utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1,
                                nullptr, 0, nullptr, nullptr);
    if (n <= 1) return {};
    std::string s(static_cast<size_t>(n), '\0');   // room for the NUL
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1,
                        s.data(), n, nullptr, nullptr);
    s.resize(static_cast<size_t>(n) - 1);          // drop the NUL
    return s;
}

static std::string sid_to_string(PSID sid) {
    LPWSTR str = nullptr;
    if (!ConvertSidToStringSidW(sid, &str)) return "";
    std::wstring ws(str);
    LocalFree(str);
    return std::string(ws.begin(), ws.end());
}

static void emit_error(const std::string& stage,
                       const std::string& message) {
    std::cout << JsonOut()
        .set("stage",           std::string("error"))
        .set("error_stage",     stage)
        .set("error",           message)
        .set("sid",             std::string(""))
        .set("pid",             0LL)
        .set("event_name",      std::string(""))
        .dump() << "\n";
    std::cout.flush();
}

static DWORD access_to_mask(const std::wstring& access) {
    // An unrecognised access string falls through to read-only rather than
    // failing.
    if (access == L"rw") return 0x0012019F; // FILE_GENERIC_READ | FILE_GENERIC_WRITE
    if (access == L"x")  return 0x000000A0; // FILE_TRAVERSE | FILE_READ_ATTRIBUTES
    return 0x00120089;                      // FILE_GENERIC_READ
}

struct BrokerFile {
    std::wstring path;
    std::wstring access; // "r", "rw", or "x"
    std::wstring mode;   // "secure", "inherit", or "grant"
};

struct LaunchConfig {
    std::wstring moniker;
    std::wstring exe_path;
    std::vector<std::wstring> args;
    std::wstring working_dir;
    std::vector<BrokerFile> broker_files;
    JobConfig job_config;
    DWORD parent_pid;
    bool breakaway;
};

static LaunchConfig parse_config(const JVal& j) {
    LaunchConfig cfg;
    cfg.moniker     = to_wide(j.at("moniker").get<std::string>());
    cfg.exe_path    = to_wide(j.at("exe_path").get<std::string>());
    cfg.working_dir = to_wide(j.value("working_dir", std::string{}));
    cfg.parent_pid  = j.at("parent_pid").get<DWORD>();
    cfg.breakaway   = j.value("breakaway", false);

    for (auto& a : j.at("args").arr) {
        cfg.args.push_back(to_wide(a.get<std::string>()));
    }

    for (auto& f : j.at("broker_files").arr) {
        BrokerFile bf;
        bf.path   = to_wide(f.at("path").get<std::string>());
        bf.access = to_wide(f.at("access").get<std::string>());
        bf.mode   = to_wide(f.at("mode").get<std::string>());
        cfg.broker_files.push_back(std::move(bf));
    }

    auto& jc = j.at("job_config");
    cfg.job_config.cpu_max_rate        = jc.at("cpu_max_rate").get<DWORD>();
    cfg.job_config.cpu_min_rate        = jc.at("cpu_min_rate").get<DWORD>();
    // Mandatory like skip_memory_limit: at() throws on absence rather than
    // defaulting, so a payload that predates this field fails loudly instead of
    // silently applying a CPU cap the descriptor asked to skip.
    cfg.job_config.skip_cpu_limit      = jc.at("skip_cpu_limit").get<bool>();
    cfg.job_config.skip_memory_limit   = jc.at("skip_memory_limit").get<bool>();
    SIZE_T mb = jc.at("memory_limit_mb").get<SIZE_T>();
    cfg.job_config.memory_limit_bytes  = mb * 1024 * 1024;

    return cfg;
}

// Quotes one argument per the CommandLineToArgvW escaping rules, ported
// from CPython's subprocess.list2cmdline. process.py relies on the same
// rules for wincage's native launch path.
//
// Without this, an argument containing '"' or ending in an odd run of
// '\' breaks out of its quoted region and is re-read as separate
// arguments by the child's own argv parser.
//
// The trailing-backslash handling is deliberately asymmetric: backslashes
// immediately before the closing quote are doubled so they cannot escape
// it, but trailing backslashes in an unquoted argument are left as is.
static std::wstring quote_arg(const std::wstring& arg) {
    std::wstring result;
    bool needquote = arg.empty()
                   || arg.find(L' ')  != std::wstring::npos
                   || arg.find(L'\t') != std::wstring::npos;
    if (needquote) result += L'"';

    size_t bs_count = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++bs_count;
        } else if (c == L'"') {
            result.append(bs_count * 2, L'\\');
            bs_count = 0;
            result += L"\\\"";
        } else {
            if (bs_count) {
                result.append(bs_count, L'\\');
                bs_count = 0;
            }
            result += c;
        }
    }

    if (bs_count) result.append(bs_count, L'\\');
    if (needquote) {
        result.append(bs_count, L'\\');
        result += L'"';
    }
    return result;
}

static std::wstring build_cmdline(const std::wstring& exe,
                                  const std::vector<std::wstring>& args) {
    std::wstring result = quote_arg(exe);
    for (auto& a : args) {
        result += L' ';
        result += quote_arg(a);
    }
    return result;
}

static std::vector<wchar_t> build_env_block(const std::vector<std::wstring>& extra_vars) {
    std::vector<wchar_t> block;
    wchar_t* parent = GetEnvironmentStringsW();
    if (parent) {
        for (wchar_t* p = parent; *p; ) {
            size_t len = wcslen(p) + 1;
            block.insert(block.end(), p, p + len);
            p += len;
        }
        FreeEnvironmentStringsW(parent);
    }
    for (auto& var : extra_vars) {
        block.insert(block.end(), var.begin(), var.end());
        block.push_back(L'\0');
    }
    block.push_back(L'\0'); // double-null terminator
    return block;
}

// Owns a PROC_THREAD_ATTRIBUTE_LIST for the lifetime of one CreateProcessW
// call.
//
// build() checks every step, but get() still returns the partial list
// after a failure. Callers MUST honour build()'s return value: launching
// with a partially built list means the AppContainer SID is silently
// absent from the child's token, while the host reports a sandboxed
// start.
//
// `sc` and `handles` are referenced by the attribute list itself and must
// stay alive and unmodified until CreateProcessW returns.
class ProcThreadAttrList {
public:
    ProcThreadAttrList() = default;
    ~ProcThreadAttrList() { reset(); }
    ProcThreadAttrList(const ProcThreadAttrList&) = delete;
    ProcThreadAttrList& operator=(const ProcThreadAttrList&) = delete;

    bool build(SECURITY_CAPABILITIES& sc,
               std::vector<HANDLE>& handles,
               std::string& err) {
        reset();

        DWORD  count = handles.empty() ? 1 : 2;
        SIZE_T size  = 0;
        // First call is expected to fail with ERROR_INSUFFICIENT_BUFFER; its
        // only job is reporting the required buffer size.
        InitializeProcThreadAttributeList(nullptr, count, 0, &size);
        buf_.assign(size, 0);

        auto* list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(buf_.data());
        if (!InitializeProcThreadAttributeList(list, count, 0, &size)) {
            err = "InitializeProcThreadAttributeList failed ("
                + hex32(GetLastError()) + ")";
            return false;
        }
        list_        = list;
        initialised_ = true;

        if (!UpdateProcThreadAttribute(
                list_, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                &sc, sizeof(sc), nullptr, nullptr)) {
            err = "UpdateProcThreadAttribute (SECURITY_CAPABILITIES) failed ("
                + hex32(GetLastError()) + ")";
            return false;
        }

        if (!handles.empty()) {
            if (!UpdateProcThreadAttribute(
                    list_, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handles.data(), handles.size() * sizeof(HANDLE),
                    nullptr, nullptr)) {
                err = "UpdateProcThreadAttribute (HANDLE_LIST) failed ("
                    + hex32(GetLastError()) + ")";
                return false;
            }
        }
        return true;
    }

    LPPROC_THREAD_ATTRIBUTE_LIST get() const { return list_; }

private:
    void reset() {
        if (initialised_) {
            DeleteProcThreadAttributeList(list_);
            initialised_ = false;
        }
        list_ = nullptr;
    }

    std::vector<BYTE>            buf_;
    LPPROC_THREAD_ATTRIBUTE_LIST list_        = nullptr;
    bool                         initialised_ = false;
};

static int run_reset(const std::string& moniker_utf8) {
    std::wstring moniker = to_wide(moniker_utf8);
    HRESULT hr = AppContainer::reset(moniker);
    if (FAILED(hr)) {
        std::cerr << "DeleteAppContainerProfile failed: 0x"
                  << std::hex << hr << "\n";
        return 1;
    }
    return 0;
}

static int run_launch(const LaunchConfig& cfg) {
    AppContainer container(cfg.moniker);
    auto cr = container.provision();
    if (cr == ContainerResult::Failed) {
        emit_error("CONTAINER_PROVISION",
                   "CreateAppContainerProfile failed");
        return 1;
    }

    if (FAILED(container.grant_window_station())) {
        emit_error("CONTAINER_PROVISION", "grant_window_station failed");
        return 1;
    }

    // Every broker_files entry is mandatory. A silently skipped grant leaves
    // the child sealed inside the AppContainer with no access to its own
    // media, saves, or config. That surfaces much later as an opaque
    // in-child failure, so any failure here aborts the launch and reports
    // on the DACL_GRANT stage.
    std::vector<HANDLE>       inherit_handles;
    std::vector<std::wstring> sandbox_env_vars;

    auto close_inherit_handles = [&inherit_handles]() {
        for (HANDLE h : inherit_handles) CloseHandle(h);
        inherit_handles.clear();
    };

    auto fail_dacl = [&](const std::string& what,
                         const std::wstring& path,
                         const std::string& code) -> int {
        close_inherit_handles();
        emit_error("DACL_GRANT",
                   what + " failed for '" + to_utf8(path) + "' (" + code + ")");
        return 1;
    };

    for (size_t i = 0; i < cfg.broker_files.size(); i++) {
        const BrokerFile& bf   = cfg.broker_files[i];
        DWORD             mask = access_to_mask(bf.access);

        if (bf.mode == L"secure") {
            HRESULT hr = container.secure_existing_file(bf.path, mask);
            if (FAILED(hr))
                return fail_dacl("secure_existing_file", bf.path,
                                 hex32(static_cast<DWORD>(hr)));

        } else if (bf.mode == L"inherit") {
            SECURITY_ATTRIBUTES sa = {};
            sa.nLength        = sizeof(sa);
            sa.bInheritHandle = TRUE;
            HANDLE h = CreateFileW(
                bf.path.c_str(), mask,
                FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr
            );
            if (h == INVALID_HANDLE_VALUE)
                return fail_dacl("CreateFileW (inherit)", bf.path,
                                 hex32(GetLastError()));
            std::wostringstream oss;
            oss << L"SANDBOX_HANDLE_" << i << L"="
                << static_cast<unsigned long long>(
                       reinterpret_cast<uintptr_t>(h));
            sandbox_env_vars.push_back(oss.str());
            inherit_handles.push_back(h);

        } else if (bf.mode == L"grant") {
            HRESULT hr = container.grant_directory(bf.path, mask);
            if (FAILED(hr))
                return fail_dacl("grant_directory", bf.path,
                                 hex32(static_cast<DWORD>(hr)));

        } else {
            // Unrecognised mode: none of the branches above would apply any
            // grant, so the process would launch with a grant silently missing.
            // Fail rather than proceed. sandbox_config.py constrains mode to a
            // Literal, so reaching here means the two sides have drifted.
            return fail_dacl("unrecognised broker_file mode '"
                                 + to_utf8(bf.mode) + "'",
                             bf.path, "CONFIG");
        }
    }

    SandboxEvent evt(cfg.moniker, cfg.parent_pid);
    if (evt.create() == EventResult::Failed) {
        close_inherit_handles();
        emit_error("PROCESS_CREATE", "CreateEventW failed");
        return 1;
    }

    // `sc` is referenced by the attribute list and must outlive every
    // CreateProcessW call below, so it lives here.
    SECURITY_CAPABILITIES sc = {};
    sc.AppContainerSid = container.sid();

    std::vector<wchar_t> env_block;
    bool use_custom_env = !sandbox_env_vars.empty();
    if (use_custom_env) {
        env_block = build_env_block(sandbox_env_vars);
    }
    LPVOID  env_ptr     = use_custom_env
                          ? static_cast<LPVOID>(env_block.data())
                          : nullptr;
    LPCWSTR working_dir = cfg.working_dir.empty() ? nullptr
                                                  : cfg.working_dir.c_str();

    // ProcThreadAttrList rebuilds and re-checks the attribute list on every
    // call, so the breakaway retry below reuses this exact path instead of a
    // separate unchecked copy of it.
    auto create_sandboxed = [&](bool breakaway,
                                PROCESS_INFORMATION& out_pi,
                                std::string& err) -> bool {
        ProcThreadAttrList attrs;
        if (!attrs.build(sc, inherit_handles, err)) return false;

        STARTUPINFOEXW si = {};
        si.StartupInfo.cb          = sizeof(si);
        si.StartupInfo.dwFlags     = STARTF_USESHOWWINDOW;
        si.StartupInfo.wShowWindow = SW_SHOWNORMAL;
        si.lpAttributeList         = attrs.get();

        DWORD create_flags = CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT;
        if (use_custom_env) create_flags |= CREATE_UNICODE_ENVIRONMENT;
        if (breakaway)      create_flags |= CREATE_BREAKAWAY_FROM_JOB;

        std::wstring cmdline = build_cmdline(cfg.exe_path, cfg.args);
        out_pi = PROCESS_INFORMATION{};

        if (!CreateProcessW(
                cfg.exe_path.c_str(),
                cmdline.data(),
                nullptr, nullptr,
                inherit_handles.empty() ? FALSE : TRUE,
                create_flags,
                env_ptr,
                working_dir,
                &si.StartupInfo,
                &out_pi)) {
            err = std::string("CreateProcessW")
                + (breakaway ? " (breakaway)" : "")
                + " failed (" + hex32(GetLastError()) + ")";
            return false;
        }
        return true;
    };

    PROCESS_INFORMATION pi = {};
    std::string         create_err;

    if (!create_sandboxed(cfg.breakaway, pi, create_err)) {
        close_inherit_handles();
        emit_error("PROCESS_CREATE", create_err);
        return 1;
    }

    auto kill_process = [&pi]() {
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        pi = PROCESS_INFORMATION{};
    };

    // Limits are applied before assignment so the process can never run outside
    // its caps once resumed.
    JobObject job;
    if (FAILED(job.create())) {
        kill_process();
        close_inherit_handles();
        emit_error("JOB_ASSIGN", "JobObject::create failed");
        return 1;
    }

    HRESULT hr_apply_limits = job.apply_limits(cfg.job_config);
    if (FAILED(hr_apply_limits)) {
        kill_process();
        close_inherit_handles();
        // Embeds the HRESULT so an out-of-range cpu_min_rate/cpu_max_rate
        // (E_INVALIDARG) reads as distinct from a SetInformationJobObject
        // Win32 failure.
        emit_error("JOB_ASSIGN",
                   "JobObject::apply_limits failed ("
                       + hex32(static_cast<DWORD>(hr_apply_limits)) + ")");
        return 1;
    }

    // ERROR_ACCESS_DENIED from AssignProcessToJobObject signals that the
    // process sits in a job forbidding a second assignment. That is the one
    // condition the breakaway relaunch clears. Mirrors the error-5 retry
    // trigger in job.py::add_process.
    //
    // Do not widen this to IsProcessInJob(h, nullptr, ...): that is true
    // for essentially every process on Windows 11. The retry would then
    // fire unconditionally, and every launch would create, terminate, and
    // re-create the child even when the first assignment would have
    // succeeded.
    //
    // cfg.breakaway is re-checked because a retry would use identical
    // flags and cannot help. Aborting beats launching the same process
    // twice.
    HRESULT hr_assign = job.assign(pi.hProcess);
    if (hr_assign == HRESULT_FROM_WIN32(ERROR_ACCESS_DENIED) && !cfg.breakaway) {
        kill_process();
        if (!create_sandboxed(true, pi, create_err)) {
            close_inherit_handles();
            emit_error("PROCESS_CREATE", create_err);
            return 1;
        }
        hr_assign = job.assign(pi.hProcess);
    }
    if (FAILED(hr_assign)) {
        kill_process();
        close_inherit_handles();
        emit_error("JOB_ASSIGN",
                   "JobObject::assign failed ("
                       + hex32(static_cast<DWORD>(hr_assign)) + ")");
        return 1;
    }

    // Close our inheritable copies. The child holds inherited duplicates.
    close_inherit_handles();

    // A ResumeThread failure leaves the target permanently suspended.
    // stage="started" has not been emitted yet, so this is fatal here
    // rather than reported as a successful start. Matches
    // SandboxProcess.resume() on the native path.
    DWORD resume_result = ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    if (resume_result == static_cast<DWORD>(-1)) {
        std::string err = "ResumeThread failed (" + hex32(GetLastError()) + ")";
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hProcess);
        emit_error("PROCESS_CREATE", err);
        return 1;
    }

    // Hands Python a handle to this exact process instead of a pid it
    // would have to reopen later. The target is a deliberate crash
    // boundary, so a bare pid can be reused by an unrelated process in
    // the window before Python opens it. A duplicated handle has no such
    // window. It is a direct reference to this process object, not a
    // lookup by an identifier Windows can recycle.
    //
    // Access is scoped to what SandboxProcess and WindowsJobObject
    // actually call on it: terminate, poll/wait, and job assignment.
    HANDLE parent_for_dup = OpenProcess(PROCESS_DUP_HANDLE, FALSE, cfg.parent_pid);
    if (!parent_for_dup) {
        std::string err = "OpenProcess(parent, PROCESS_DUP_HANDLE) failed ("
            + hex32(GetLastError()) + ")";
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hProcess);
        emit_error("PROCESS_CREATE", err);
        return 1;
    }

    HANDLE dup_target_handle = nullptr;
    BOOL dup_ok = DuplicateHandle(
        GetCurrentProcess(), pi.hProcess,
        parent_for_dup, &dup_target_handle,
        PROCESS_TERMINATE | PROCESS_SET_QUOTA
            | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
        FALSE, 0);
    DWORD dup_error = GetLastError();
    CloseHandle(parent_for_dup);
    if (!dup_ok) {
        std::string err = "DuplicateHandle to parent failed (" + hex32(dup_error) + ")";
        TerminateProcess(pi.hProcess, 0);
        CloseHandle(pi.hProcess);
        emit_error("PROCESS_CREATE", err);
        return 1;
    }

    std::string evt_name = to_utf8(evt.name());

    std::cout << JsonOut()
        .set("sid",            sid_to_string(container.sid()))
        .set("pid",            static_cast<long long>(pi.dwProcessId))
        .set("process_handle", static_cast<long long>(reinterpret_cast<INT_PTR>(dup_target_handle)))
        .set("event_name",     evt_name)
        .set("stage",          std::string("started"))
        .dump() << "\n";
    std::cout.flush();

    // The parent handle is opened once, before the thread starts. The wait
    // is then on this exact process object rather than re-resolving
    // parent_pid on every poll: a PID Windows could reassign to an
    // unrelated process.
    //
    // "started" was already reported to Python above, so a watchdog setup
    // failure cannot abort the launch. The child is running and Python
    // depends on it. Setup failure degrades to a direct wait on the
    // child, losing only prompt teardown when the parent dies.
    HANDLE parent_handle = OpenProcess(SYNCHRONIZE, FALSE, cfg.parent_pid);
    HANDLE done_event    = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    bool watchdog_usable = (parent_handle != nullptr) && (done_event != nullptr);
    if (!parent_handle) {
        std::cerr << "sandbox_host: OpenProcess(parent_pid=" << cfg.parent_pid
                  << ") failed (" << hex32(GetLastError())
                  << "); parent-death detection disabled for this launch.\n";
    }
    if (!done_event) {
        std::cerr << "sandbox_host: CreateEventW(done_event) failed ("
                  << hex32(GetLastError())
                  << "); parent-death detection disabled for this launch.\n";
    }
    if (!watchdog_usable && parent_handle) {
        // Watchdog would otherwise take ownership of this handle; since it
        // won't be started, close it here instead.
        CloseHandle(parent_handle);
        parent_handle = nullptr;
    }

    Watchdog watchdog(parent_handle, done_event);
    if (watchdog_usable) watchdog.start();

    DWORD wait_result;
    if (watchdog_usable) {
        HANDLE wait_handles[2] = { pi.hProcess, done_event };
        wait_result = WaitForMultipleObjects(2, wait_handles, FALSE, INFINITE);
    } else {
        wait_result = WaitForSingleObject(pi.hProcess, INFINITE);
    }

    watchdog.stop();

    if (watchdog_usable && wait_result == WAIT_FAILED) {
        // Neither "child exited" nor "parent died" was observed, so do not let
        // this fall through and be misread as either.
        std::cerr << "sandbox_host: WaitForMultipleObjects failed ("
                  << hex32(GetLastError())
                  << "); falling back to a direct wait on the child process.\n";
        wait_result = WaitForSingleObject(pi.hProcess, INFINITE);
    }

    DWORD exit_code = 0;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);

    if (done_event) CloseHandle(done_event);

    // Unblocks the Python stdout reader.
    std::cout << JsonOut()
        .set("stage",     std::string("exited"))
        .set("exit_code", static_cast<long long>(exit_code))
        .dump() << "\n";
    std::cout.flush();

    // Unblocks the Python named-event watcher.
    evt.signal();

    return static_cast<int>(exit_code);
}

int main(int argc, char* argv[]) {
    if (argc == 3 && std::string(argv[1]) == "--reset") {
        // This mode has no JSON stdout protocol to report through, so a throw
        // out of run_reset() is caught locally and reported to stderr rather
        // than reaching std::terminate().
        try {
            return run_reset(argv[2]);
        } catch (const std::exception& ex) {
            std::cerr << "sandbox_host --reset: " << ex.what() << "\n";
            return 1;
        }
    }

    std::string input;
    {
        std::ostringstream oss;
        oss << std::cin.rdbuf();
        input = oss.str();
    }

    if (input.empty()) {
        emit_error("CONFIG_VALIDATION", "No JSON config received on stdin");
        return 1;
    }

    try {
        JVal j = json_parse(input);
        LaunchConfig cfg = parse_config(j);
        return run_launch(cfg);
    } catch (const std::exception& ex) {
        emit_error("CONFIG_VALIDATION",
                   std::string("JSON parse error: ") + ex.what());
        return 1;
    }
}
