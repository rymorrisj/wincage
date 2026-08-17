"""
This is a manual test suit for JobObjects specifically

Launches a sandboxed process that just sleeps for 60 seconds, for 
time to inspect its Job Object limits by hand. Can use any process
monitoring/analyzing application you choose. I use Microsoft's
Process Explorer and Process Monitor

Run from the repo root after `pip install -e .`:
    python test_job_inspect.py
"""

import asyncio

import wincage


async def main() -> None:
    config = wincage.SandboxConfig(
        moniker="wincage.test.job_inspect",
        exe_path=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        args=["-NoProfile", "-Command", "Start-Sleep -Seconds 60"],
        cpu_max_rate=20,
        cpu_min_rate=5,
        memory_limit_mb=100,
    )

    pid_box = {}
    done = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    def on_started(payload):
        pid_box["pid"] = payload.pid
        print(f"Started, pid={payload.pid}")
        print("Go inspect it in Process Explorer now, you have ~60 seconds.")
        print("Configured: cpu_max_rate=20, memory_limit_mb=100")

    def on_cleaned_up(payload):
        print("CLEANED_UP, process is gone now.")
        main_loop.call_soon_threadsafe(done.set)

    handle = wincage.launch(config)
    handle.on(wincage.SandboxEvent.STARTED, on_started)
    handle.on(wincage.SandboxEvent.CLEANED_UP, on_cleaned_up)

    await done.wait()


if __name__ == "__main__":
    asyncio.run(main())
