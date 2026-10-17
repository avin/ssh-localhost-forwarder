"""Runs on a Linux SSH host, using only the Python standard library."""
import ipaddress
import json
import re
import subprocess
import sys


def loopback(address):
    address = address.strip("[]")
    if ":" not in address:
        address = address.split("%", 1)[0]
    return {"*": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(address, address)


def priority(address):
    if address == "127.0.0.1":
        return 0
    if ipaddress.ip_address(address).is_loopback and ":" not in address:
        return 1
    return 2 if ":" not in address else 3


def put(ports, port, address):
    port = int(port)
    address = loopback(address)
    ipaddress.ip_address(address)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    if port not in ports or priority(address) < priority(ports[port]):
        ports[port] = address


def parse_listeners(output):
    ports = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        address, port = fields[3].rsplit(":", 1)
        put(ports, port, address)
    return ports


def add_docker_ports(ports, output):
    pattern = r"(\[[^\]]+\]|[\d.]+):(\d+(?:-\d+)?)->\d+(?:-\d+)?/tcp"
    for address, published in re.findall(pattern, output):
        bounds = [int(value) for value in published.split("-")]
        first, last = bounds[0], bounds[-1]
        if not 1 <= first <= last <= 65535:
            raise ValueError("Invalid Docker port range")
        for port in range(first, last + 1):
            put(ports, port, address)


def discover(use_docker=True):
    listeners = subprocess.run(["ss", "-H", "-lnt"], capture_output=True,
                               text=True, check=True, timeout=10)
    ports = parse_listeners(listeners.stdout)
    warnings = []
    if use_docker:
        try:
            docker = subprocess.run(["docker", "ps", "--format", "{{.Ports}}"],
                                    capture_output=True, text=True, check=True, timeout=10)
            add_docker_ports(ports, docker.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append("Docker discovery unavailable: " + str(exc))
    return {"ports": ports, "warnings": warnings}


if __name__ == "__main__":
    print(json.dumps(discover(use_docker=sys.argv[1:] != ["--no-docker"])))
