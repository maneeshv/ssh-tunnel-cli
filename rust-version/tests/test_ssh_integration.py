"""Opt-in Rust lifecycle checks against an isolated loopback SSH server.

Python is used only for the existing development fixture and controlling PTYs.
The implementation under test runs entirely in Rust.
"""

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests.ssh_harness import SSHHarness
from tests.test_ssh_integration import _PTYChild

PROBE = ROOT / "rust-version/target/debug/examples/lifecycle_probe"
CLI = ROOT / "rust-version/target/debug/tunnel-rs"


class RustPTYChild(_PTYChild):
    def __enter__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 96, 0, 0))
            os.execve(self.arguments[0], self.arguments, self.environment)
        return self


@unittest.skipUnless(CLI.exists(), "requires cargo build")
class RustPromptTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="trs-ui-", dir="/tmp"))
        self.config = self.directory / "config"
        self.config.write_text("Host alpha\n HostName 127.0.0.1\nHost zebra\n HostName 127.0.0.1\n")
        self.command = [str(CLI), "--config", str(self.config), "--state-dir", str(self.directory / "state")]

    def tearDown(self):
        subprocess.run(["trash", str(self.directory)], check=True, capture_output=True)

    def transition(self, child, keys, marker):
        child.output = b""
        child.send(keys)
        return child.read_until(marker)

    def test_type_to_filter_and_ctrl_c(self):
        with RustPTYChild([], command=self.command) as child:
            child.read_until(b"What would you like to do?")
            self.transition(child, b"\r", b"Choose a machine")
            self.transition(child, b"zebra\r", b"Remote port")
            self.assertIn(b"zebra", child.output)
            self.transition(child, b"\x03", b"Cancelled")
            self.assertEqual(child.wait(), 130)
        self.assertEqual(list((self.directory / "state").glob("*.sock")), [])

    def test_escape_back_preserves_answer(self):
        with RustPTYChild([], command=self.command) as child:
            child.read_until(b"What would you like to do?")
            self.transition(child, b"\r", b"Choose a machine")
            self.transition(child, b"\r", b"Remote port")
            self.transition(child, b"8765\r", b"Local port")
            output = self.transition(child, b"\x1b", b"Remote port")
            self.assertIn(b"8765", output)
            self.transition(child, b"\r", b"Local port")
            self.transition(child, b"\x03", b"Cancelled")
            self.assertEqual(child.wait(), 130)

    def test_no_matching_host_cannot_submit(self):
        with RustPTYChild([], command=self.command) as child:
            child.read_until(b"What would you like to do?")
            self.transition(child, b"\r", b"Choose a machine")
            self.transition(child, b"zzznomatch", b"zzznomatch")
            child.send(b"\r")
            # Let the terminal render an invalid selection; it must stay at the picker.
            time.sleep(0.15)
            child.send(b"\x03")
            child.read_until(b"Cancelled")
            self.assertNotIn(b"Remote port", child.output)
            self.assertEqual(child.wait(), 130)

    def test_escape_from_edit_returns_to_review(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            local_port = listener.getsockname()[1]
        with RustPTYChild([], command=self.command) as child:
            child.read_until(b"What would you like to do?")
            self.transition(child, b"\r", b"Choose a machine")
            self.transition(child, b"\r", b"Remote port")
            self.transition(child, b"8765\r", b"Local port")
            self.transition(child, b"\x7f" * 4 + f"{local_port}\r".encode(), b"Destination")
            self.transition(child, b"\r", b"How should it run?")
            self.transition(child, b"\r", b"Edit settings")
            self.transition(child, b"\x1b[B\r", b"Edit which setting?")
            output = self.transition(child, b"\x1b", b"Edit settings")
            self.assertIn(b"Ready to start", output)
            self.assertNotIn(b"How should it run?", output)
            self.transition(child, b"\x03", b"Cancelled")
            self.assertEqual(child.wait(), 130)
        self.assertEqual(list((self.directory / "state").glob("*.sock")), [])


@unittest.skipUnless(
    os.environ.get("TUNNEL_SSH_INTEGRATION") == "1" and PROBE.exists() and CLI.exists() and Path("/usr/sbin/sshd").exists(),
    "opt-in isolated system OpenSSH test; requires cargo build --examples and cargo build",
)
class RustSSHTests(unittest.TestCase):
    def wait_cli(self, child):
        # Drain the prompt's final cursor restoration before waiting for PTY exit.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not select.select([child.fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            try:
                data = os.read(child.fd, 65536)
            except OSError:
                data = b""
            if not data:
                return child.wait()
            child.output += data
        self.fail(f"CLI did not close its terminal: {child.output!r}")

    def arguments(self, harness, mode="background", behavior="normal"):
        return [harness.config, harness.directory / "rs", harness.target_port,
                harness.port(), mode, behavior]

    def child(self, arguments):
        return RustPTYChild([], command=[str(PROBE), "start", *map(str, arguments)])

    def probe(self, arguments, operation="records", tunnel_id="", check=True):
        result = subprocess.run(
            [str(PROBE), operation, str(arguments[0]), str(arguments[1]), tunnel_id],
            capture_output=True, text=True, timeout=10,
        )
        if not check:
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepared(self, child, harness, arguments):
        output = child.read_until(b"START? ")
        tunnel_id = re.search(rb"PREPARED:([0-9a-f]{12})", output).group(1).decode()
        owned = arguments[1] / f"{tunnel_id}.sock"
        harness.sockets.append(owned)
        self.assertFalse(owned.exists())
        self.assertFalse(harness.listening(arguments[3]))
        self.assertEqual(self.probe(arguments), [])
        return tunnel_id, owned

    def start_background(self, harness):
        arguments = self.arguments(harness)
        with self.child(arguments) as child:
            tunnel_id, owned = self.prepared(child, harness, arguments)
            child.send(b"\n")
            child.read_until(b"RETURNED:Running")
            self.assertEqual(child.wait(), 0)
        return arguments, tunnel_id, owned

    @staticmethod
    def ask_host_key(harness):
        unknown_hosts = harness.directory / "new_known_hosts"
        harness.config.write_text(
            harness.config.read_text()
            .replace(str(harness.directory / "known_hosts"), str(unknown_hosts))
            .replace("StrictHostKeyChecking yes", "StrictHostKeyChecking ask")
            .replace("BatchMode yes", "BatchMode no")
        )
        return unknown_hosts

    def test_background_traffic_ownership_inherited_config_and_inert_prepare(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            independent_port = harness.port()
            self.assertEqual(harness.forward(independent, independent_port).returncode, 0)
            arguments, tunnel_id, owned = self.start_background(harness)
            record = self.probe(arguments)[0]
            self.assertEqual(record["status"], "Running")
            self.assertTrue(record["successful"])
            self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
            self.assertFalse(harness.listening(harness.inherited_port))
            self.assertFalse(harness.local_command_marker.exists())
            self.assertIsInstance(self.probe(arguments, "logs", tunnel_id), str)
            self.assertEqual(self.probe(arguments, "stop", tunnel_id)["status"], "Stopped")
            self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
            self.assertFalse(harness.listening(arguments[3]))
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.exchange(independent_port), b"owned-tunnel-round-trip")

    def test_native_host_key_prompt_after_start_and_background_survives_terminal_close(self):
        with SSHHarness() as harness:
            unknown_hosts = self.ask_host_key(harness)
            arguments = self.arguments(harness)
            with self.child(arguments) as child:
                tunnel_id, _ = self.prepared(child, harness, arguments)
                self.assertFalse(unknown_hosts.exists())
                self.assertNotIn(b"Are you sure", child.output)
                child.send(b"\n")
                child.read_until(b"Are you sure you want to continue connecting")
                self.assertFalse(harness.listening(arguments[3]))
                child.send(b"yes\n")
                child.read_until(b"RETURNED:Running")
                self.assertEqual(child.wait(), 0)
                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                child.close_terminal()
                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                self.assertTrue(unknown_hosts.exists())
                self.assertEqual(self.probe(arguments, "stop", tunnel_id)["status"], "Stopped")

    def test_foreground_ctrl_c_stops_only_owned_connection(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            arguments = self.arguments(harness, "foreground")
            with self.child(arguments) as child:
                _, owned = self.prepared(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"RUNNING:")
                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                child.send(b"\x03")
                child.read_until(b"RETURNED:Stopped")
                self.assertEqual(child.wait(), 0)
                self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
                self.assertFalse(harness.listening(arguments[3]))
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                self.assertEqual(harness.control(independent, "check").returncode, 0)

    def test_foreground_callback_error_cleans_up(self):
        with SSHHarness() as harness:
            arguments = self.arguments(harness, "foreground", "callback_error")
            with self.child(arguments) as child:
                _, owned = self.prepared(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"EXPECTED_ERROR:test callback failure")
                self.assertEqual(child.wait(), 0)
                self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
                self.assertFalse(harness.listening(arguments[3]))
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)

    def test_ctrl_c_during_native_authentication_cancels_and_releases_state_lock(self):
        with SSHHarness() as harness:
            unknown_hosts = self.ask_host_key(harness)
            arguments = self.arguments(harness)
            with self.child(arguments) as child:
                _, owned = self.prepared(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"Are you sure you want to continue connecting")
                child.send(b"\x03")
                child.read_until(b"CANCELLED:")
                self.assertEqual(child.wait(), 130)
                self.assertFalse(unknown_hosts.exists())
                self.assertFalse(harness.listening(arguments[3]))
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                self.assertEqual(self.probe(arguments)[0]["status"], "Failed")
                record = self.probe(arguments)[0]
                self.assertEqual(record["status"], "Failed")
                self.assertFalse(record["successful"])

    def test_port_taken_during_auth_cleans_up_and_records_failure(self):
        with SSHHarness() as harness:
            self.ask_host_key(harness)
            arguments = self.arguments(harness, behavior="expected_error")
            with self.child(arguments) as child:
                _, owned = self.prepared(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"Are you sure you want to continue connecting")
                with socket.socket() as occupied:
                    occupied.bind(("127.0.0.1", arguments[3]))
                    occupied.listen()
                    child.send(b"yes\n")
                    child.read_until(b"EXPECTED_ERROR:")
                    self.assertEqual(child.wait(), 0)
                    self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                record = self.probe(arguments)[0]
                self.assertEqual(record["status"], "Failed")
                self.assertFalse(record["successful"])
                self.assertTrue(record["last_error"])

    def test_ctrl_c_during_stop_finishes_shutdown_and_releases_lock(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            arguments, tunnel_id, owned = self.start_background(harness)
            checked = harness.control(owned, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            # This PID comes from our live, fixture-owned master, never persisted user state.
            master_pid = int(re.search(r"pid=(\d+)", checked.stderr).group(1))
            os.kill(master_pid, signal.SIGSTOP)
            resumed = False
            try:
                command = [str(PROBE), "stop", str(arguments[0]), str(arguments[1]), tunnel_id]
                with RustPTYChild([], command=command) as child:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        processes = subprocess.run(
                            ["/bin/ps", "-axo", "ppid=,command="],
                            capture_output=True, text=True, check=True,
                        ).stdout.splitlines()
                        if any(line.strip().startswith(f"{child.pid} ") and "/usr/bin/ssh" in line
                               for line in processes):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("Stop never launched a native control command")
                    child.send(b"\x03")
                    time.sleep(0.05)
                    os.kill(master_pid, signal.SIGCONT)
                    resumed = True
                    child.read_until(b"Cancelled after stopping")
                    self.assertEqual(self.wait_cli(child), 130)
            finally:
                if not resumed:
                    os.kill(master_pid, signal.SIGCONT)
            self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
            self.assertFalse(harness.listening(arguments[3]))
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.control(independent, "check").returncode, 0)

    def test_remote_application_failure_does_not_mean_ssh_disconnected(self):
        with SSHHarness() as harness:
            arguments, tunnel_id, _ = self.start_background(harness)
            self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
            harness.server.shutdown()
            harness.server.server_close()
            harness.server = None
            with socket.create_connection(("127.0.0.1", arguments[3]), timeout=3) as client:
                client.sendall(b"app-is-now-down")
                try:
                    response = client.recv(1024)
                except ConnectionResetError:
                    response = b""
                self.assertEqual(response, b"")
            self.assertEqual(self.probe(arguments)[0]["status"], "Running")
            self.assertEqual(self.probe(arguments, "stop", tunnel_id)["status"], "Stopped")

    def test_start_and_stop_restore_default_signal_handlers(self):
        for behavior in ("hold_after_start", "hold_after_stop"):
            with self.subTest(behavior=behavior), SSHHarness() as harness:
                arguments = self.arguments(harness, behavior=behavior)
                with self.child(arguments) as child:
                    tunnel_id, _ = self.prepared(child, harness, arguments)
                    child.send(b"\n")
                    child.read_until(b"HOLDING")
                    if behavior == "hold_after_stop":
                        self.assertIn(b"STOPPED", child.output)
                    os.kill(child.pid, signal.SIGTERM)
                    self.assertEqual(child.wait(), -signal.SIGTERM)
                if behavior == "hold_after_start":
                    self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                    self.probe(arguments, "stop", tunnel_id)
                self.assertFalse(harness.listening(arguments[3]))

    def test_corrupt_registry_is_preserved_and_cannot_stop_connection(self):
        with SSHHarness() as harness:
            arguments, tunnel_id, _ = self.start_background(harness)
            registry = arguments[1] / "registry.json"
            valid = registry.read_bytes()
            corrupt = b'{"version":1,"tunnels":['
            registry.write_bytes(corrupt)
            result = self.probe(arguments, "stop", tunnel_id, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("preserved", result.stderr)
            self.assertEqual(registry.read_bytes(), corrupt)
            self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
            registry.write_bytes(valid)
            self.assertEqual(self.probe(arguments, "stop", tunnel_id)["status"], "Stopped")

    def test_cli_wizard_background_return_list_and_confirmed_stop(self):
        with SSHHarness() as harness:
            arguments = self.arguments(harness)
            command = [str(CLI),
                       "--config", str(harness.config), "--state-dir", str(arguments[1])]
            try:
                with RustPTYChild([], command=command) as child:
                    child.read_until(b"What would you like to do?")
                    child.send(b"\r")
                    child.read_until(b"Choose a machine")
                    child.send(b"\r")
                    child.read_until(b"Remote port")
                    child.send(f"{harness.target_port}\r".encode())
                    child.read_until(b"Local port")
                    child.send(b"\x7f" * len(str(harness.target_port)) + f"{arguments[3]}\r".encode())
                    child.read_until(b"Destination")
                    child.send(b"\r")
                    child.read_until(b"How should it run?")
                    child.send(b"\r")
                    child.read_until(b"Edit settings")
                    self.assertIn(b"Ready to start", child.output)
                    self.assertIn(b"Exact commands", child.output)
                    self.assertFalse(harness.listening(arguments[3]))
                    self.assertEqual(list(arguments[1].glob("*.sock")), [])
                    self.assertEqual(self.probe(arguments), [])
                    child.send(b"\r")
                    child.read_until(b"Return to shell")
                    self.assertIn(b"Tunnel running in background", child.output)
                    self.assertIn(b"application health not checked", child.output)
                    child.send(b"\r")
                    self.assertEqual(self.wait_cli(child), 0)

                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                record = self.probe(arguments)[0]
                self.assertEqual(record["settings"]["local_port"], arguments[3])
                listing = subprocess.run(command + ["list"], capture_output=True, text=True, timeout=10)
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn(record["id"], listing.stdout)
                self.assertIn(f"127.0.0.1:{arguments[3]}", listing.stdout)
                self.assertIn("Running", listing.stdout)

                with RustPTYChild([], command=command + ["stop", record["id"]]) as child:
                    child.read_until(f"Stop fixture on local port {arguments[3]}?".encode())
                    self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                    child.send(b"\x1b[B\r")
                    child.read_until(b"Tunnel stopped.")
                    self.assertEqual(self.wait_cli(child), 0)
                self.assertFalse(harness.listening(arguments[3]))
                self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
            finally:
                harness.sockets.extend(arguments[1].glob("*.sock"))


if __name__ == "__main__":
    unittest.main()
