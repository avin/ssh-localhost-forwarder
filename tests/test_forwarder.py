import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import discovery
import forwarder


class DiscoveryTests(unittest.TestCase):
    def test_listeners_prefer_ipv4_loopback_and_support_ipv6(self):
        output = "\n".join([
            "LISTEN 0 4096 [::]:8080 [::]:*",
            "LISTEN 0 4096 192.0.2.1:8080 0.0.0.0:*",
            "LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*",
            "LISTEN 0 4096 [::1]:9000 [::]:*",
            "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*",
        ])
        self.assertEqual(discovery.parse_listeners(output), {
            8080: "127.0.0.1", 9000: "::1", 53: "127.0.0.53"})

    def test_docker_published_ranges_exclude_unpublished_and_udp(self):
        ports = {}
        discovery.add_docker_ports(ports,
            "5432/tcp, 0.0.0.0:8000-8002->80-82/tcp, [::]:8000-8002->80-82/tcp, "
            "127.0.0.1:9000->90/tcp, 0.0.0.0:5353->53/udp")
        self.assertEqual(ports, {8000: "127.0.0.1", 8001: "127.0.0.1",
                                 8002: "127.0.0.1", 9000: "127.0.0.1"})

    def test_docker_failure_keeps_non_docker_listeners(self):
        with patch("discovery.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "LISTEN 0 128 0.0.0.0:8000 0.0.0.0:*", ""),
            FileNotFoundError("docker"),
        ]):
            result = discovery.discover()
        self.assertEqual(result["ports"], {8000: "127.0.0.1"})
        self.assertEqual(len(result["warnings"]), 1)


class ConfigurationTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8-sig")
            return forwarder.load_config(path)

    def test_host_is_required_and_options_are_validated(self):
        for value in ({}, {"ssh_host": "-bad"}, {"ssh_host": "two words"},
                      {"ssh_host": "test", "exclude_ports": [True]},
                      {"ssh_host": "test", "poll_interval_seconds": 0},
                      {"ssh_host": "test", "unexpected": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)
        self.assertEqual(self.load({"ssh_host": "another-host"})["ssh_host"], "another-host")

    def test_custom_host_and_port_filters_reach_remote_discovery(self):
        config = self.load({"ssh_host": "custom-alias", "include_ports": [5432, 8080],
                            "exclude_ports": [8080], "docker": False})
        response = subprocess.CompletedProcess([], 0, json.dumps({
            "ports": {"22": "127.0.0.1", "5432": "127.0.0.1", "8080": "::1"}}), "")
        with patch("forwarder.subprocess.run", return_value=response) as run:
            ports, warnings = forwarder.discover_remote(config, ["ssh"])
        self.assertEqual(ports, {5432: "127.0.0.1"})
        self.assertEqual(warnings, [])
        self.assertEqual(run.call_args.args[0], ["ssh", "custom-alias", "python3 - --no-docker"])

    def test_ipv6_can_be_disabled_and_tunnel_target_is_quoted(self):
        desired = forwarder.desired_tunnels({5432: "::1"}, False)
        self.assertEqual(desired, {(5432, "127.0.0.1"): (socket.AF_INET, "::1")})
        self.assertEqual(forwarder.forward_spec("::1", 5432, "::1"), "[::1]:5432:[::1]:5432")

    def test_busy_local_port_is_detected(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            self.assertFalse(forwarder.can_bind(socket.AF_INET, "127.0.0.1", blocker.getsockname()[1]))


if __name__ == "__main__":
    unittest.main()
