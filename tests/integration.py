"""Run against a running installation: python tests/integration.py --config PATH.

Requires unrestricted include_ports, IPv6, Docker, and an existing node:24-alpine
image (or --image). Does not pull images. Cleans up its own temporary resources.
"""
import argparse
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forwarder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=forwarder.default_config())
    parser.add_argument("--image", default="node:24-alpine")
    args = parser.parse_args()
    config = forwarder.load_config(args.config)
    if config["include_ports"] or not config["listen_ipv6"] or not config["docker"]:
        parser.error("This check requires all ports, IPv6, and Docker enabled")
    ssh = forwarder.ssh_command(config) + [config["ssh_host"]]
    status_file = args.config.parent / "runtime" / "status.json"

    def remote(arguments):
        return subprocess.check_output(ssh + [shlex.join(arguments)], text=True, timeout=20).strip()

    def state():
        return json.loads(status_file.read_text(encoding="utf-8"))

    def wait_for(check, description):
        deadline = time.monotonic() + max(35, config["poll_interval_seconds"] * 4)
        while time.monotonic() < deadline:
            try:
                if check():
                    print("PASS:", description, flush=True)
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.5)
        raise AssertionError(description)

    def fetch(port, ipv6=False):
        address = "[::1]" if ipv6 else "127.0.0.1"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{address}:{port}/", timeout=2) as response:
            return response.read()

    blocker = socket.socket()
    if sys.platform == "win32":
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    if port in config["exclude_ports"]:
        blocker.close()
        parser.error("The temporary port is excluded; run again")
    code = f'''
import http.server, select, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"localhost-forwarder-check")
    def log_message(self, *args): pass
server = http.server.HTTPServer(("127.0.0.1", {port}), Handler)
print("ready", flush=True)
while True:
    ready, _, _ = select.select([sys.stdin, server.socket], [], [])
    if sys.stdin in ready and not sys.stdin.read(1): break
    if server.socket in ready: server.handle_request()
server.server_close()
'''
    server = None
    name = "localhost-forwarder-check-" + uuid.uuid4().hex[:8]
    container_created = False
    try:
        server = subprocess.Popen(ssh + [shlex.join([config["remote_python"], "-u", "-c", code])],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert server.stdout.readline().strip() == "ready", "Remote server failed to start"
        wait_for(lambda: any(c["port"] == port and c["listen"] == "127.0.0.1"
                             for c in state()["conflicts"]), "occupied Windows port preserved")
        wait_for(lambda: fetch(port, True) == b"localhost-forwarder-check", "automatic IPv6 tunnel")
        blocker.close()
        wait_for(lambda: fetch(port) == b"localhost-forwarder-check", "IPv4 retry after port becomes free")
        server.stdin.close()
        server.wait(timeout=5)
        wait_for(lambda: not any(t["port"] == port for t in state()["tunnels"]),
                 "tunnels removed after guest listener stops")
        container = remote(["docker", "run", "--pull", "never", "--rm", "-d", "--name", name,
                            "-p", "127.0.0.1::8080", args.image, "node", "-e",
                            "require('http').createServer((q,s)=>s.end(require('os').hostname())).listen(8080,'0.0.0.0')"])
        container_created = True
        published = remote(["docker", "port", name, "8080/tcp"])
        docker_port = int(published.rsplit(":", 1)[1])
        wait_for(lambda: fetch(docker_port).decode() == container[:12], "Docker loopback publication forwarded")
        remote(["docker", "stop", "-t", "1", name])
        container_created = False
        wait_for(lambda: not any(t["port"] == docker_port for t in state()["tunnels"]),
                 "Docker tunnels removed after container stops")
    finally:
        blocker.close()
        if server is not None and server.poll() is None:
            if not server.stdin.closed:
                server.stdin.close()
            server.wait(timeout=5)
        if container_created:
            remote(["docker", "rm", "-f", name])


if __name__ == "__main__":
    main()
