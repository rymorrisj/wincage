#include "watchdog.h"

Watchdog::Watchdog(HANDLE parent_handle, HANDLE done_event)
    : parent_handle_(parent_handle), done_event_(done_event) {
    cancel_event_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
}

Watchdog::~Watchdog() {
    stop();
    if (parent_handle_) CloseHandle(parent_handle_);
    if (cancel_event_) CloseHandle(cancel_event_);
}

void Watchdog::start() {
    // No usable parent handle or no cancel event to block on with it: stay
    // idle rather than spin up a thread that could never observe a cancel.
    if (!parent_handle_ || !cancel_event_) return;
    thread_ = std::thread(&Watchdog::monitor_loop, this);
}

void Watchdog::stop() {
    if (cancel_event_) SetEvent(cancel_event_);
    if (thread_.joinable()) {
        thread_.join();
    }
}

void Watchdog::monitor_loop() {
    HANDLE handles[2] = { parent_handle_, cancel_event_ };
    DWORD result = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
    if (result == WAIT_OBJECT_0) {
        // parent_handle_ signaled: the parent process has exited.
        SetEvent(done_event_);
    }
    // WAIT_OBJECT_0 + 1 (cancel_event_) or WAIT_FAILED: normal shutdown or
    // an unexpected wait error. Either way don't signal done_event; the
    // caller's own wait on the child process handle remains authoritative.
}
