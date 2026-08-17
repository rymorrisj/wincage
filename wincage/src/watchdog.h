#pragma once
#include <windows.h>
#include <thread>

// Watches a single already-open parent-process handle and signals done_event
// if that handle fires before stop() is called.
//
// The handle is never re-resolved from a PID, so it can't be fooled by PID
// reuse. WaitForMultipleObjects against the handle plus an internal cancel
// event, instead of polling OpenProcess on a timer, also makes stop() return
// as soon as it's signaled rather than after a sleep interval elapses.
class Watchdog {
public:
    // Takes ownership of parent_handle (closed by the destructor); pass
    // nullptr if none is available, and start() becomes a no-op.
    //
    // done_event is signaled but NOT owned; the caller manages its lifetime.
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
