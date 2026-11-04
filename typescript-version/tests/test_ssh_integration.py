"""Opt-in TypeScript lifecycle checks against an isolated loopback SSH server.

Python is used only for the existing development fixture and controlling PTYs.
The implementation under test runs entirely in Node.
"""

import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests.ssh_harness import SSHHarness
from tests.test_ssh_integration import _PTYChild

NODE = shutil.which("node")
CORE = (ROOT / "typescript-version/dist/src/core.js").as_uri()

PTY_MANAGER = """
import { createInterface } from 'node:readline/promises';
const [core, config, state, remote, local, mode, behavior] = process.argv.slice(1);
const { Manager, TunnelCancelled } = await import(core);
const manager = new Manager(config, state);
const record = manager.prepare({alias:'fixture', remote_port:Number(remote),
  local_port:Number(local), destination:'127.0.0.1', mode});
console.log('PREPARED:' + record.id);
const prompt = createInterface({input:process.stdin, output:process.stdout});
await prompt.question('START? ');
prompt.close();
process.stdin.pause();
try {
  const result = await manager.start(record, started => {
    console.log('RUNNING:' + started.id);
    if (behavior === 'callback_error') throw new Error('test callback failure');
  });
  console.log('RETURNED:' + result.status);
} catch (error) {
  if (TunnelCancelled && error instanceof TunnelCancelled) {
    console.log('CANCELLED:' + error.message);
    process.exitCode = 130;
  } else {
    if (behavior !== 'callback_error' && behavior !== 'expected_error') throw error;
    console.log('EXPECTED_ERROR:' + error.message);
  }
}
"""

PROBE = """
const [core, config, state, operation, id] = process.argv.slice(1);
const { Manager } = await import(core);
const manager = new Manager(config, state);
console.log(JSON.stringify(await manager[operation](id)));
"""

PTY_STOP = """
const [core, config, state, id, ssh] = process.argv.slice(1);
const { Manager, TunnelCancelled } = await import(core);
const manager = new Manager(config, state);
manager.ssh = ssh;
try {
  await manager.stop(id);
  console.log('STOP_RETURNED');
} catch (error) {
  if (!(error instanceof TunnelCancelled)) throw error;
  console.log('STOP_CANCELLED');
  process.exitCode = 130;
}
"""


@unittest.skipUnless(
    os.environ.get("TUNNEL_SSH_INTEGRATION") == "1" and NODE and Path("/usr/sbin/sshd").exists(),
    "opt-in isolated system OpenSSH test; requires Node and a built TypeScript package",
)
class TypeScriptSSHTests(unittest.TestCase):
    def wait_cli(self, child):
        # Drain Inquirer's final cursor restoration before waiting for PTY exit.
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
        return [harness.config, harness.directory / "ts", harness.target_port,
                harness.port(), mode, behavior]

    def child(self, arguments):
        return _PTYChild([], command=[NODE, "--input-type=module", "-e", PTY_MANAGER,
                                      CORE, *map(str, arguments)])

    def probe(self, arguments, operation="records", tunnel_id="", check=True):
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", PROBE, CORE, str(arguments[0]),
             str(arguments[1]), operation, tunnel_id],
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
                self.assertFalse((arguments[1] / "lock").exists())
                record = self.probe(arguments)[0]
                self.assertEqual(record["status"], "Failed")
                self.assertFalse(record["successful"])

    def test_ctrl_c_during_stop_finishes_native_shutdown_and_releases_lock(self):
        with SSHHarness() as harness:
            independent, _ = harness.launch("shared", background=True)
            arguments, tunnel_id, owned = self.start_background(harness)
            entered = harness.directory / "control-entered"
            release = harness.directory / "control-release"
            wrapper = harness.directory / "gated-ssh"
            # Hold a real control-command child alive when terminal group SIGINT arrives.
            wrapper.write_text(
                "#!/bin/sh\ntrap 'exit 130' INT TERM HUP\n"
                f": > {shlex.quote(str(entered))}\n"
                f"while [ ! -f {shlex.quote(str(release))} ]; do /bin/sleep .02; done\n"
                'exec /usr/bin/ssh "$@"\n'
            )
            wrapper.chmod(0o700)
            command = [NODE, "--input-type=module", "-e", PTY_STOP, CORE,
                       str(arguments[0]), str(arguments[1]), tunnel_id, str(wrapper)]
            with _PTYChild([], command=command) as child:
                deadline = time.monotonic() + 3
                while not entered.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(entered.exists(), "Stop never launched its control command")
                self.assertTrue((arguments[1] / "lock").exists())
                child.send(b"\x03")
                time.sleep(0.05)
                release.touch()
                child.read_until(b"STOP_CANCELLED")
                self.assertEqual(self.wait_cli(child), 130)
            self.assertFalse((arguments[1] / "lock").exists())
            self.assertEqual(self.probe(arguments)[0]["status"], "Stopped")
            self.assertFalse(harness.listening(arguments[3]))
            self.assertNotEqual(harness.control(owned, "check").returncode, 0)
            self.assertEqual(harness.control(independent, "check").returncode, 0)

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
            command = [NODE, str(ROOT / "typescript-version/dist/src/cli.js"),
                       "--config", str(harness.config), "--state-dir", str(arguments[1])]
            try:
                with _PTYChild([], command=command) as child:
                    child.read_until(b"What would you like to do?")
                    child.send(b"\r")
                    child.read_until(b"Choose a machine")
                    child.send(b"\r")
                    child.read_until(b"Remote port")
                    child.send(f"{harness.target_port}\r".encode())
                    child.read_until(b"Local port")
                    child.send(f"\x15{arguments[3]}\r".encode())
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
                    child.read_until(b"Next Return to shell")
                    self.assertEqual(self.wait_cli(child), 0)

                self.assertEqual(harness.exchange(arguments[3]), b"owned-tunnel-round-trip")
                record = self.probe(arguments)[0]
                self.assertEqual(record["settings"]["local_port"], arguments[3])
                listing = subprocess.run(command + ["list"], capture_output=True, text=True, timeout=10)
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn(record["id"], listing.stdout)
                self.assertIn(f"127.0.0.1:{arguments[3]}", listing.stdout)
                self.assertIn("Running", listing.stdout)

                with _PTYChild([], command=command + ["stop", record["id"]]) as child:
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
