#pragma once
#include <windows.h>
#include <thread>

// Watches a single already-open parent-process handle and signals
// done_event if that handle fires before stop() is called.
//
// The handle is opened once by the caller before start() runs and is never
// re-resolved from a PID, so it cannot be fooled by Windows recycling that
// PID onto an unrelated process between checks. Blocking on the handle (via
// WaitForMultipleObjects against the parent handle and an internal cancel
// event) instead of polling OpenProcess on a timer also means stop()
// returns as soon as the cancel event is signaled, not after a sleep
// interval elapses.
class Watchdog {
public:
    // Takes ownership of parent_handle (closed by the destructor); pass
    // nullptr if no usable parent handle is available; start() then
    // becomes a no-op. done_event is signaled but NOT owned; the caller
    // manages its lifetime.
    Watchdog(HANDLE parent_handle, HANDLE done_event);
    ~Watchdog();

    Watchdog(const Watchdog&) = delete;
    Watchdog& operator=(const Watchdog&) = delete;

    void start();
    void stop();

private:
    void monitor_loop();

    HANDLE parent_handle_;          // owned; may be nullptr
    HANDLE done_event_;             // not owned; caller manages lifetime
    HANDLE cancel_event_ = nullptr; // owned; signaled by stop()
    std::thread thread_;
};
