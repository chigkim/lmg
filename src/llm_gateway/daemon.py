"""The command line: run the gateway in the foreground, or start, stop and check it as a background daemon.

    lmg proxy [run]        serve in this console until Ctrl-C
    lmg proxy start        serve in the background; returns once it is listening
    lmg proxy stop         stop the background gateway and everything it started
    lmg proxy restart      stop, then start
    lmg proxy status       whether it is running, its pid and its log

A daemon is known by its port: its pid and console output are in logs/PORT.pid and logs/PORT.log in this project,
found from this file rather than the working directory, so `stop` and `status` work from any folder. It runs in the
folder `start` was run from, and reads config.toml and .env there.
"""
import argparse
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time

STATE = pathlib.Path(__file__).resolve().parents[2] / "logs"   # src/llm_gateway/daemon.py -> the project
READY = 30                   # seconds `start` waits for the port to answer
WINDOWS = sys.platform == "win32"


def pid_file(port):
    return STATE / f"{port}.pid"


def log_file(port):
    return STATE / f"{port}.log"


def alive(pid):
    """Whether a process with this pid exists. On Windows os.kill would terminate it, so ask the kernel instead."""
    if WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def born(pid):
    """When the process started, in the kernel's own units, so a pid reused by another process is told apart;
    None where that cannot be read."""
    if WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        times = [ctypes.c_ulonglong() for _ in range(4)]
        ok = kernel.GetProcessTimes(handle, *map(ctypes.byref, times))
        kernel.CloseHandle(handle)
        return times[0].value if ok else None
    try:  # Linux: field 22 of /proc/PID/stat, counted after the parenthesised name, which may hold spaces
        return int(pathlib.Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def listening(port):
    """Whether something accepts connections on the port. A bare connect, so the gateway logs no refused request."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return False
    return True


def running(port):
    """The daemon's pid when its pid file names a live process that started when the daemon did, else None;
    a stale pid file is removed."""
    path = pid_file(port)
    try:
        pid, _, started = path.read_text().strip().partition(" ")
        pid = int(pid)
    except (OSError, ValueError):
        return None
    if alive(pid) and started == str(born(pid)):
        return pid
    path.unlink(missing_ok=True)
    return None


def spawn(argv, out):
    """Start argv detached from this console, so it outlives the shell that started it."""
    common = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, close_fds=True)
    if not WINDOWS:
        return subprocess.Popen(argv, start_new_session=True, **common)
    # No window, its own process group, and out of the caller's job object where the job allows it, so closing
    # the terminal (or the tool that ran `start`) does not take the gateway with it.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **common)


def start(args):
    pid = running(args.port)
    if pid:
        print(f"already running on port {args.port}, pid {pid}")
        return 0
    if listening(args.port):
        print(f"port {args.port} is already in use by something this command did not start")
        return 1
    STATE.mkdir(parents=True, exist_ok=True)
    config = str(pathlib.Path(args.config).resolve())
    argv = [sys.executable, "-m", "llm_gateway", "proxy", "run", "--port", str(args.port), "--config", config]
    with open(log_file(args.port), "a", encoding="utf-8") as out:
        out.write(f"\n==== start {time.strftime('%Y-%m-%d %H:%M:%S')} in {os.getcwd()}\n")
        out.flush()
        child = spawn(argv, out)
    pid_file(args.port).write_text(f"{child.pid} {born(child.pid)}")
    deadline = time.monotonic() + READY
    while time.monotonic() < deadline:
        if child.poll() is not None:
            pid_file(args.port).unlink(missing_ok=True)
            print(f"the gateway exited with code {child.returncode}; see {log_file(args.port)}")
            return 1
        if listening(args.port):
            print(f"started on http://127.0.0.1:{args.port}/v1, pid {child.pid}; log {log_file(args.port)}")
            return 0
        time.sleep(0.3)
    kill(child.pid)
    pid_file(args.port).unlink(missing_ok=True)
    print(f"pid {child.pid} was not answering on port {args.port} after {READY}s, so it was stopped; "
          f"see {log_file(args.port)}")
    return 1


def kill(pid):
    """Stop the gateway at PID and everything it started, and wait for it to go."""
    if WINDOWS:
        # /T takes the Claude children with it; Windows has no SIGTERM to let the gateway close them itself.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(pid, signal.SIGTERM)                 # aiohttp shuts down cleanly on SIGTERM
    for _ in range(50):
        if not alive(pid):
            return
        time.sleep(0.2)
    if not WINDOWS:
        os.killpg(pid, signal.SIGKILL)


def stop(args):
    pid = running(args.port)
    if not pid:
        print(f"not running on port {args.port}")
        return 0
    kill(pid)
    pid_file(args.port).unlink(missing_ok=True)
    print(f"stopped pid {pid}")
    return 0


def restart(args):
    stop(args)
    return start(args)


def status(args):
    pid = running(args.port)
    if pid:
        answer = "listening" if listening(args.port) else "NOT answering"
        print(f"running on port {args.port}, pid {pid}, {answer}; log {log_file(args.port)}")
        return 0
    print(f"not running on port {args.port}")
    return 3


def main(argv):
    parser = argparse.ArgumentParser(prog="lmg proxy", description=__doc__.split("\n")[0])
    parser.add_argument("command", nargs="?", default="run", choices=("run", "start", "stop", "restart", "status"))
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--config", default="config.toml", help="settings file (default: config.toml)")
    args = parser.parse_args(argv)
    if args.command == "run":
        from .gateway import serve
        return serve(args.port, args.config)
    return {"start": start, "stop": stop, "restart": restart, "status": status}[args.command](args)
