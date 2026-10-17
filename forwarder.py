"""Automatic localhost TCP forwarding to a configurable Linux SSH host."""
import argparse
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time

DEFAULTS = {
    "poll_interval_seconds": 5, "connect_timeout_seconds": 5,
    "remote_python": "python3", "ssh_path": "ssh", "docker": True,
    "listen_ipv6": True, "include_ports": [], "exclude_ports": [],
}
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def default_config():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "SshLocalhostForwarder" / "config.json"


def load_config(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = data.keys() - (DEFAULTS.keys() | {"ssh_host"})
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    config = DEFAULTS | data
    host = config.get("ssh_host")
    if not isinstance(host, str) or not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError("ssh_host must be a nonempty SSH alias or user@host")
    for key in ("poll_interval_seconds", "connect_timeout_seconds"):
        value = config[key]
        if type(value) not in (int, float) or not 1 <= value <= 300:
            raise ValueError(key + " must be between 1 and 300 seconds")
    for key in ("ssh_path", "remote_python"):
        if not isinstance(config[key], str) or not config[key] or "\n" in config[key]:
            raise ValueError(key + " must be a nonempty executable path")
    for key in ("docker", "listen_ipv6"):
        if type(config[key]) is not bool:
            raise ValueError(key + " must be a boolean")
    for key in ("include_ports", "exclude_ports"):
        if not isinstance(config[key], list) or any(type(p) is not int or not 1 <= p <= 65535 for p in config[key]):
            raise ValueError(key + " must be a list of port numbers (1-65535)")
    return config


def ssh_command(config):
    executable = shutil.which(config["ssh_path"])
    if not executable:
        raise ValueError("SSH executable not found: " + config["ssh_path"])
    return [executable, "-o", "BatchMode=yes", "-o",
            "ConnectTimeout=" + str(int(config["connect_timeout_seconds"])),
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]


def discover_remote(config, command):
    source = Path(__file__).with_name("discovery.py").read_text(encoding="utf-8")
    remote = [config["remote_python"], "-"]
    if not config["docker"]:
        remote.append("--no-docker")
    result = subprocess.run(command + [config["ssh_host"], shlex.join(remote)], input=source,
                            capture_output=True, text=True, timeout=config["connect_timeout_seconds"] + 25,
                            creationflags=CREATE_FLAGS)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "SSH discovery failed")
    data = json.loads(result.stdout)
    ports = {}
    for raw_port, address in data["ports"].items():
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid remote port")
        ipaddress.ip_address(address)
        if (not config["include_ports"] or port in config["include_ports"]) and port not in config["exclude_ports"]:
            ports[port] = address
    return ports, data.get("warnings", [])


def desired_tunnels(ports, ipv6):
    addresses = [(socket.AF_INET, "127.0.0.1")]
    if ipv6:
        addresses.append((socket.AF_INET6, "::1"))
    return {(port, address): (family, target)
            for port, target in ports.items() for family, address in addresses}


def forward_spec(address, port, target):
    address = f"[{address}]" if ":" in address else address
    target = f"[{target}]" if ":" in target else target
    return f"{address}:{port}:{target}:{port}"


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def can_bind(family, address, port):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((address, port))
        return True
    except OSError:
        return False


def acquire_lock(path):
    handle = path.open("a+b")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def write_state(runtime, state):
    temporary = runtime / "status.tmp"
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(runtime / "status.json")


def protect_process_tree():
    """Keep a noninherited job handle open until exit; Windows owns cleanup."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # Closing this handle ourselves would also terminate the current process.
    # The OS closes it on normal exit, a crash, or TerminateProcess instead.
    return job


def run(config_path, config):
    runtime = config_path.parent / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if (runtime / "paused").exists():
        return
    lock = acquire_lock(runtime / "instance.lock")
    if lock is None:
        print("Already running for this configuration")
        return
    handler = RotatingFileHandler(runtime / "forward.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    tunnels = {}
    conflicts = set()
    last_error = None
    last_warnings = []
    stop_file = runtime / "stop"
    stop_file.unlink(missing_ok=True)
    logging.info("Started; SSH host=%s", config["ssh_host"])
    try:
        process_job = protect_process_tree()
        command = ssh_command(config)
        while not stop_file.exists():
            error = None
            try:
                ports, warnings = discover_remote(config, command)
                if warnings != last_warnings:
                    for warning in warnings:
                        logging.warning("%s", warning)
                last_warnings = warnings
                desired = desired_tunnels(ports, config["listen_ipv6"])
                for key in list(tunnels):
                    process, target = tunnels[key]
                    if key not in desired or desired[key][1] != target or process.poll() is not None:
                        stop_process(process)
                        del tunnels[key]
                        logging.info("Removed %s:%s", key[1], key[0])
                new_conflicts = set()
                for key, (family, target) in desired.items():
                    if key in tunnels:
                        continue
                    port, address = key
                    if not can_bind(family, address, port):
                        new_conflicts.add(key)
                        if key not in conflicts:
                            logging.warning("Windows port occupied: %s:%s", address, port)
                        continue
                    # Bound stderr file size; existing children share this file.
                    ssh_log = runtime / "ssh.log"
                    if ssh_log.exists() and ssh_log.stat().st_size > 1_000_000:
                        with ssh_log.open("wb"):
                            pass
                    with ssh_log.open("ab") as errors:
                        process = subprocess.Popen(
                            command + ["-o", "ExitOnForwardFailure=yes", "-N", "-T", "-L",
                                       forward_spec(address, port, target), config["ssh_host"]],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=errors, creationflags=CREATE_FLAGS)
                    tunnels[key] = (process, target)
                    logging.info("Tunnel %s:%s -> %s:%s", address, port, target, port)
                conflicts = new_conflicts
                if last_error:
                    logging.info("Connection restored")
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
                error = str(exc)
                if error != last_error:
                    logging.warning("Discovery unavailable; retrying: %s", error)
                for process, _ in tunnels.values():
                    stop_process(process)
                tunnels.clear()
                conflicts.clear()
            last_error = error
            write_state(runtime, {
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "ssh_host": config["ssh_host"],
                "error": error, "warnings": last_warnings,
                "tunnels": [{"port": port, "listen": address, "target": target, "pid": process.pid}
                            for (port, address), (process, target) in tunnels.items() if process.poll() is None],
                "conflicts": [{"port": port, "listen": address} for port, address in sorted(conflicts)],
            })
            deadline = time.monotonic() + config["poll_interval_seconds"]
            while not stop_file.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
    finally:
        for process, _ in tunnels.values():
            stop_process(process)
        write_state(runtime, {"stopped": True})
        logging.info("Stopped")
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("action", choices=["run", "check", "status", "stop"])
    args = parser.parse_args()
    path = args.config.expanduser().resolve()
    runtime = path.parent / "runtime"
    if args.action == "status":
        print((runtime / "status.json").read_text(encoding="utf-8"))
    elif args.action == "stop":
        if not runtime.exists():
            parser.error("No runtime directory; the forwarder has not been started")
        (runtime / "stop").touch()
        print("Stop requested; check status for confirmation")
    else:
        config = load_config(path)
        if args.action == "check":
            ports, warnings = discover_remote(config, ssh_command(config))
            print(json.dumps({"ssh_host": config["ssh_host"], "ports": ports, "warnings": warnings}, indent=2))
        else:
            run(path, config)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
