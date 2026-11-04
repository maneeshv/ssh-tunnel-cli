"""Run with TUNNEL_SSH_INTEGRATION=1 using permission to bind loopback sockets."""

import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests.ssh_harness import SSHHarness
from tunnel_cli.core import Manager, Settings, TunnelError


_PTY_MANAGER = """
from pathlib import Path
import sys
from tunnel_cli.core import Manager, Settings

config, state, remote, local, mode, behavior = sys.argv[1:]
manager = Manager(config_path=Path(config), state_dir=Path(state))
record = manager.prepare(Settings('fixture', int(remote), int(local), mode=mode))
print('PREPARED:' + record['id'], flush=True)
input('START? ')

def started(record):
    print('RUNNING:' + record['id'], flush=True)
    if behavior == 'callback_error':
        raise RuntimeError('test callback failure')

try:
    result = manager.start(record, on_started=started)
except RuntimeError as exc:
    if behavior != 'callback_error':
        raise
    print('CALLBACK_ERROR:' + str(exc), flush=True)
else:
    print('RETURNED:' + result['status'], flush=True)
"""


class _PTYChild:
    """One owned Python child with a controlling terminal and bounded cleanup."""

    def __init__(self, arguments, command=None):
        self.arguments = command or [sys.executable, "-c", _PTY_MANAGER, *map(str, arguments)]
        self.environment = dict(os.environ, PROMPT_TOOLKIT_NO_CPR="1", TERM="xterm-256color", NO_COLOR="1")
        self.output = b""
        self.status = None
        self.fd = None

    def __enter__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(self.arguments[0], self.arguments, self.environment)
        return self

    def send(self, value):
        os.write(self.fd, value)

    def read_until(self, marker, timeout=10):
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"PTY timed out waiting for {marker!r}: {self.output!r}")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                raise AssertionError(f"PTY closed before {marker!r}: {self.output!r}")
            self.output += data
        return self.output

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
            elif time.monotonic() >= deadline:
                raise TimeoutError("PTY child did not exit")
            else:
                time.sleep(0.02)
        return self.status

    def close_terminal(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *exception):
        self.close_terminal()
        try:
            self.wait(timeout=1)
        except TimeoutError:
            os.kill(self.pid, signal.SIGTERM)
            try:
                self.wait()
            except TimeoutError:
                os.kill(self.pid, signal.SIGKILL)
                self.wait()


@unittest.skipUnless(
    os.environ.get("TUNNEL_SSH_INTEGRATION") == "1" and Path("/usr/sbin/sshd").exists(),
    "opt-in isolated system OpenSSH test",
)
class NativeSSHTests(unittest.TestCase):
    def test_clear_inherited_forwards_then_add_reviewed_route(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            independent_port = harness.port()
            result = harness.forward(independent, independent_port)
            self.assertEqual(result.returncode, 0, result.stderr)

            for background in (False, True):
                with self.subTest(background=background):
                    owned, process = harness.launch(f"owned-{int(background)}", background)
                    self.assertFalse(harness.listening(harness.inherited_port))
                    self.assertFalse(harness.local_command_marker.exists())
                    local_port = harness.port()
                    result = harness.forward(owned, local_port)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(harness.exchange(local_port), b"owned-tunnel-round-trip")
                    self.assertEqual(harness.exchange(independent_port), b"owned-tunnel-round-trip")
                    if background:
                        self.assertEqual(process.poll(), 0)
                    else:
                        self.assertIsNone(process.poll())

                    result = harness.control(owned, "exit")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if not background:
                        # macOS OpenSSH may return 255 for a successful control-channel exit.
                        self.assertIn(process.wait(timeout=5), (0, 255))
                    self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                    self.assertFalse(harness.listening(local_port))
                    self.assertEqual(harness.control(independent, "check").returncode, 0)
                    self.assertEqual(harness.exchange(independent_port), b"owned-tunnel-round-trip")

    def test_failed_forward_is_reported_and_owned_master_can_be_stopped(self):
        with SSHHarness() as harness:
            owned, _ = harness.launch("occupied", background=True)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                result = harness.forward(owned, occupied.getsockname()[1])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.control(owned, "exit").returncode, 0)

    def test_foreground_interrupt_leaves_independent_session_running(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            owned, process = harness.launch("foreground")
            local_port = harness.port()
            self.assertEqual(harness.forward(owned, local_port).returncode, 0)
            self.assertEqual(harness.exchange(local_port), b"owned-tunnel-round-trip")
            process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertFalse(harness.listening(local_port))
            self.assertEqual(harness.control(independent, "check").returncode, 0)


@unittest.skipUnless(
    os.environ.get("TUNNEL_SSH_INTEGRATION") == "1" and Path("/usr/sbin/sshd").exists(),
    "opt-in isolated system OpenSSH test",
)
class ManagerSSHTests(unittest.TestCase):
    def prepare(self, harness):
        manager = Manager(config_path=harness.config, state_dir=harness.directory / "state")
        settings = Settings(
            alias="fixture", remote_port=harness.target_port, local_port=harness.port(),
        )
        record = manager.prepare(settings)
        owned = manager.state_dir / f"{record['id']}.sock"
        harness.sockets.append(owned)
        return manager, settings, record, owned

    def test_background_start_stop_isolation_and_prepare_is_inert(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            independent_port = harness.port()
            self.assertEqual(harness.forward(independent, independent_port).returncode, 0)
            manager, settings, record, owned = self.prepare(harness)

            self.assertFalse(owned.exists())
            self.assertFalse(harness.listening(settings.local_port))
            self.assertEqual(manager.records(), [])
            callbacks = []
            started = manager.start(record, on_started=callbacks.append)
            self.assertEqual(started["status"], "Running")
            self.assertTrue(started["successful"])
            self.assertEqual([item["id"] for item in callbacks], [record["id"]])
            self.assertEqual(manager.records()[0]["status"], "Running")
            self.assertEqual(harness.exchange(settings.local_port), b"owned-tunnel-round-trip")
            self.assertFalse(harness.listening(harness.inherited_port))
            self.assertFalse(harness.local_command_marker.exists())
            self.assertIsInstance(manager.logs(record["id"]), str)

            stopped = manager.stop(record["id"])
            self.assertEqual(stopped["status"], "Stopped")
            self.assertEqual(manager.records()[0]["status"], "Stopped")
            self.assertFalse(harness.listening(settings.local_port))
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.exchange(independent_port), b"owned-tunnel-round-trip")

    def test_remote_app_down_does_not_mark_ssh_disconnected(self):
        with SSHHarness() as harness:
            manager, settings, record, _ = self.prepare(harness)
            manager.start(record)
            self.assertEqual(harness.exchange(settings.local_port), b"owned-tunnel-round-trip")
            harness.server.shutdown()
            harness.server.server_close()
            harness.server = None
            with socket.create_connection(("127.0.0.1", settings.local_port), timeout=3) as client:
                client.sendall(b"app-is-now-down")
                try:
                    response = client.recv(1024)
                except ConnectionResetError:
                    response = b""
                self.assertEqual(response, b"")
            self.assertEqual(manager.records()[0]["status"], "Running")
            self.assertEqual(manager.stop(record["id"])["status"], "Stopped")

    def test_forward_bind_race_cleans_up_master_and_records_failure(self):
        with SSHHarness() as harness:
            manager, settings, record, owned = self.prepare(harness)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", settings.local_port))
                occupied.listen()
                # Simulate a port becoming occupied after the preflight check.
                with patch("tunnel_cli.core.available_port", return_value=True):
                    with self.assertRaises(TunnelError):
                        manager.start(record)
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                failed = manager.records()[0]
                self.assertEqual(failed["status"], "Failed")
                self.assertFalse(failed["successful"])
                self.assertTrue(failed["last_error"])

    def test_corrupt_registry_does_not_stop_live_connection_or_overwrite_data(self):
        with SSHHarness() as harness:
            manager, settings, record, _ = self.prepare(harness)
            manager.start(record)
            registry = manager.state_dir / "registry.json"
            valid = registry.read_bytes()
            corrupt = b'{"version": 1, "tunnels": ['
            registry.write_bytes(corrupt)
            with self.assertRaises(TunnelError):
                manager.stop(record["id"])
            self.assertEqual(registry.read_bytes(), corrupt)
            self.assertEqual(harness.exchange(settings.local_port), b"owned-tunnel-round-trip")
            registry.write_bytes(valid)
            self.assertEqual(manager.stop(record["id"])["status"], "Stopped")

    def test_foreground_manager_interrupt_stops_only_owned_connection(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            manager = Manager(config_path=harness.config, state_dir=harness.directory / "state")
            settings = Settings(
                alias="fixture", remote_port=harness.target_port,
                local_port=harness.port(), mode="foreground",
            )
            record = manager.prepare(settings)
            owned = manager.state_dir / f"{record['id']}.sock"
            harness.sockets.append(owned)
            timer = None

            def interrupt_after_start(started):
                nonlocal timer
                self.assertEqual(harness.exchange(settings.local_port), b"owned-tunnel-round-trip")
                timer = threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGINT))
                timer.start()

            try:
                stopped = manager.start(record, on_started=interrupt_after_start)
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
            self.assertEqual(stopped["status"], "Stopped")
            self.assertEqual(manager.records()[0]["status"], "Stopped")
            self.assertFalse(harness.listening(settings.local_port))
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.control(independent, "check").returncode, 0)

    def pty_arguments(self, harness, mode, behavior="normal"):
        state = harness.directory / "pty-state"
        port = harness.port()
        return [harness.config, state, harness.target_port, port, mode, behavior]

    def prepared_pty(self, child, harness, arguments):
        output = child.read_until(b"START? ")
        tunnel_id = re.search(rb"PREPARED:([0-9a-f]{12})", output).group(1).decode()
        state = Path(arguments[1])
        owned = state / f"{tunnel_id}.sock"
        harness.sockets.append(owned)
        self.assertFalse(owned.exists())
        self.assertFalse(harness.listening(arguments[3]))
        return Manager(config_path=harness.config, state_dir=state), tunnel_id, owned

    def test_pty_native_host_key_prompt_then_background_survives_terminal_close(self):
        with SSHHarness() as harness:
            unknown_hosts = harness.directory / "new_known_hosts"
            harness.config.write_text(
                harness.config.read_text()
                .replace(str(harness.directory / "known_hosts"), str(unknown_hosts))
                .replace("StrictHostKeyChecking yes", "StrictHostKeyChecking ask")
                .replace("BatchMode yes", "BatchMode no")
            )
            arguments = self.pty_arguments(harness, "background")
            with _PTYChild(arguments) as child:
                manager, tunnel_id, owned = self.prepared_pty(child, harness, arguments)
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
                self.assertEqual(manager.records()[0]["status"], "Running")
                self.assertTrue(unknown_hosts.exists())
                self.assertEqual(manager.stop(tunnel_id)["status"], "Stopped")
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)

    def test_pty_ctrl_c_stops_foreground_and_preserves_independent_session(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            arguments = self.pty_arguments(harness, "foreground")
            with _PTYChild(arguments) as child:
                manager, _, owned = self.prepared_pty(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"RUNNING:")
                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                child.send(b"\x03")
                child.read_until(b"RETURNED:Stopped")
                self.assertEqual(child.wait(), 0)
                self.assertEqual(manager.records()[0]["status"], "Stopped")
                self.assertFalse(harness.listening(arguments[3]))
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)
                self.assertEqual(harness.control(independent, "check").returncode, 0)

    def test_pty_foreground_callback_failure_cleans_up_connection(self):
        with SSHHarness() as harness:
            arguments = self.pty_arguments(harness, "foreground", "callback_error")
            with _PTYChild(arguments) as child:
                manager, _, owned = self.prepared_pty(child, harness, arguments)
                child.send(b"\n")
                child.read_until(b"CALLBACK_ERROR:test callback failure")
                self.assertEqual(child.wait(), 0)
                self.assertEqual(manager.records()[0]["status"], "Stopped")
                self.assertFalse(harness.listening(arguments[3]))
                self.assertNotEqual(harness.control(owned, "check").returncode, 0)

    def test_cli_pty_create_list_and_confirm_stop(self):
        with SSHHarness() as harness:
            state = harness.directory / "cli-state"
            local_port = harness.port()
            command = [sys.executable, "-m", "tunnel_cli.cli", "--config", str(harness.config),
                       "--state-dir", str(state)]
            try:
                with _PTYChild([], command=command) as child:
                    child.read_until(b"What would you like to do?")
                    child.send(b"\r")
                    child.read_until(b"Choose a machine")
                    child.send(b"fixt\r")
                    child.read_until(b"Remote port")
                    child.send(f"{harness.target_port}\r".encode())
                    child.read_until(b"Local port")
                    child.send(f"\x15{local_port}\r".encode())
                    child.read_until(b"Destination")
                    child.send(b"\r")
                    child.read_until(b"How should it run?")
                    child.send(b"\r")
                    child.read_until(b"Edit settings")
                    self.assertIn(b"Ready to start", child.output)
                    self.assertIn(b"Exact commands", child.output)
                    self.assertFalse(harness.listening(local_port))
                    self.assertEqual(list(state.glob("*.sock")), [])
                    self.assertEqual(Manager(harness.config, state).records(), [])
                    child.send(b"\r")
                    child.read_until(b"Return to shell")
                    self.assertIn(b"Tunnel running in background", child.output)
                    self.assertIn(b"application health not checked", child.output)
                    child.send(b"\r")
                    self.assertEqual(child.wait(), 0)

                self.assertEqual(harness.exchange(local_port), b"owned-tunnel-round-trip")
                manager = Manager(harness.config, state)
                record = manager.records()[0]
                self.assertEqual(record["settings"]["local_port"], local_port)
                listing = subprocess.run(command + ["list"], capture_output=True, text=True, timeout=5)
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn(record["id"], listing.stdout)
                self.assertIn(f"127.0.0.1:{local_port}", listing.stdout)
                self.assertIn("Running", listing.stdout)

                with _PTYChild([], command=command + ["stop", record["id"]]) as child:
                    child.read_until(f"Stop fixture on local port {local_port}?".encode())
                    self.assertEqual(harness.exchange(local_port), b"owned-tunnel-round-trip")
                    child.send(b"\x1b[B\r")
                    child.read_until(b"Tunnel stopped.")
                    self.assertEqual(child.wait(), 0)
                self.assertFalse(harness.listening(local_port))
                self.assertEqual(manager.records()[0]["status"], "Stopped")
            finally:
                harness.sockets.extend(state.glob("*.sock"))


if __name__ == "__main__":
    unittest.main()
