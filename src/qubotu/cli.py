"""Command line: run the daemon, or install it as a login service."""

import argparse
import fcntl
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import config

LABEL = "com.qubotu.daemon"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def acquire_lock(wait: bool = False) -> object:
    """One daemon per data folder. A second one would mark the first one's runs interrupted and
    run its own scheduler against the same database. With `wait`, block until the other one stops
    instead of exiting (the login service does this, so launchd doesn't restart it in a loop)."""
    lock = open(config.HOME / "daemon.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        pid = lock.read().strip() or "unknown"
        if not wait:
            sys.exit(f"Qubotu is already running for {config.HOME} (pid {pid}).")
        print(f"Another Qubotu (pid {pid}) is running for {config.HOME}; waiting for it to stop.", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def serve(port: int, open_browser: bool, wait: bool = False) -> None:
    import uvicorn

    from .web import create_app

    config.ensure_dirs()
    lock = acquire_lock(wait)  # held for the life of the process
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.HOST, port=port, log_level="warning", timeout_graceful_shutdown=3)
    )
    handle_exit = server.handle_exit

    def on_exit(sig, frame):
        # Uvicorn waits for open connections before shutting down, and the browser's
        # live-update stream never closes on its own. End those streams first.
        app.state.engine.bus.close()
        handle_exit(sig, frame)

    server.handle_exit = on_exit
    if open_browser:
        webbrowser.open(f"http://{config.HOST}:{port}")
    print(f"Qubotu is running at http://{config.HOST}:{port}  (Ctrl-C to stop)")
    server.run()


def service_install(port: int) -> None:
    config.ensure_dirs()
    exe = shutil.which("qubotu")
    program = [exe, "serve"] if exe else [sys.executable, "-m", "qubotu", "serve"]
    plist = {
        "Label": LABEL,
        "ProgramArguments": [*program, "--port", str(port), "--wait"],
        # launchd starts with a bare PATH; keep the current one so codex (and node, if codex
        # was installed through npm) can be found.
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "QUBOTU_HOME": str(config.HOME)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(config.LOG_PATH),
        "StandardErrorPath": str(config.LOG_PATH),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"Installed {PLIST}")
    print(f"Qubotu now starts at login: http://{config.HOST}:{port}  (log: {config.LOG_PATH})")


def service_uninstall() -> None:
    if PLIST.exists():
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        PLIST.unlink()
        print("Removed the Qubotu login service.")
    else:
        print("The Qubotu login service is not installed.")


def main() -> None:
    from .version import current_version

    parser = argparse.ArgumentParser(prog="qubotu", description="Persistent personal agents, running locally.")
    parser.add_argument("--version", action="version", version=f"qubotu {current_version()}")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="Run the daemon in the foreground (default).")
    p_serve.add_argument("--port", type=int, default=config.PORT)
    p_serve.add_argument("--open", action="store_true", help="Open the UI in the browser.")
    p_serve.add_argument("--wait", action="store_true", help="If Qubotu is already running, wait for it to stop.")
    p_service = sub.add_parser("service", help="Manage the login service (macOS launchd).")
    p_service.add_argument("action", choices=["install", "uninstall"])
    p_service.add_argument("--port", type=int, default=config.PORT)
    args = parser.parse_args()

    if args.command == "service":
        if args.action == "install":
            service_install(args.port)
        else:
            service_uninstall()
    else:
        serve(getattr(args, "port", config.PORT), getattr(args, "open", False), getattr(args, "wait", False))


if __name__ == "__main__":
    main()
