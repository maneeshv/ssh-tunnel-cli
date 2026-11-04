from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tunnel_cli.core import Manager, Settings, TunnelError, validate_destination, validate_port


class ValidationTests(unittest.TestCase):
    def test_ports_reject_options_shell_text_and_out_of_range(self):
        for value in (0, 65536, True, "-L", "22;id", " 22", "22.0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_port(value)
        self.assertEqual(validate_port("08790"), 8790)

    def test_destinations_reject_urls_and_shell_fragments(self):
        for value in ("https://app", "127.0.0.1:80", "x;id", "$(id)", "-o", "", "x\nfoo", {}, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_destination(value)
        for value in ("localhost", "my-host.internal", "127.0.0.1", "::1"):
            self.assertEqual(validate_destination(value), value)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tc-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.write_text("Host example\n HostName 127.0.0.1\n")
        self.manager = Manager(self.config, self.root / "state")
        self.settings = Settings("example", 8790, 18790)

    def test_prepare_does_not_connect_and_commands_clear_then_add_forward(self):
        with patch("subprocess.Popen") as spawn, patch("subprocess.run") as run:
            record = self.manager.prepare(self.settings)
            spawn.assert_not_called()
            run.assert_not_called()
        self.assertEqual(self.manager.records(), [])
        master, forward = record["commands"]
        self.assertIn("ClearAllForwardings=yes", master)
        self.assertNotIn("-L", master)
        self.assertIn("ClearAllForwardings=no", forward)
        self.assertIn("127.0.0.1:18790:127.0.0.1:8790", forward)
        self.assertEqual(forward[1:3], ["-F", "/dev/null"])
        self.assertIn("PermitLocalCommand=no", master)
        self.assertIn("ControlPersist=no", master)

    def test_ipv6_is_bracketed_in_forward(self):
        record = self.manager.prepare(Settings("example", 8790, 18790, "::1"))
        self.assertIn("127.0.0.1:18790:[::1]:8790", record["commands"][1])

    def test_private_files_and_stale_state(self):
        record = self.manager.prepare(self.settings)
        record.update(status="Running", successful=True)
        with self.manager._locked():
            self.manager._save([record])
        self.assertEqual(self.manager.records()[0]["status"], "Disconnected")
        self.assertEqual((self.root / "state").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "state/registry.json").stat().st_mode & 0o777, 0o600)

    def test_corrupt_registry_is_preserved(self):
        path = self.root / "state/registry.json"
        path.write_text("broken JSON")
        path.chmod(0o600)
        with self.assertRaisesRegex(TunnelError, "preserved"):
            self.manager.records()
        self.assertEqual(path.read_text(), "broken JSON")

    def test_malicious_id_in_registry_is_rejected(self):
        record = self.manager.prepare(self.settings)
        record["id"] = "../../other"
        self.manager._save([record])
        with self.assertRaises(TunnelError):
            self.manager.records()

    def test_malformed_settings_are_reported_without_replacing_registry(self):
        record = self.manager.prepare(self.settings)
        record["settings"]["destination"] = {}
        self.manager._save([record])
        before = (self.root / "state/registry.json").read_bytes()
        with self.assertRaisesRegex(TunnelError, "preserved"):
            self.manager.records()
        self.assertEqual((self.root / "state/registry.json").read_bytes(), before)

    def test_rejects_shared_directory_and_symlink_file(self):
        insecure = self.root / "shared"
        insecure.mkdir(mode=0o755)
        with self.assertRaises(TunnelError):
            Manager(self.config, insecure)
        victim = self.root / "victim"
        victim.write_text("untouched")
        (self.root / "state/registry.json").symlink_to(victim)
        with self.assertRaises(OSError):
            self.manager.records()
        self.assertEqual(victim.read_text(), "untouched")

    def test_atomic_write_failure_preserves_previous_registry(self):
        first = self.manager.prepare(self.settings)
        self.manager._save([first])
        with patch("tunnel_cli.core.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.manager._save([])
        self.assertEqual(self.manager._read(), [first])

    def test_concurrent_mutation_fails_without_waiting_for_auth(self):
        with self.manager._locked():
            with self.assertRaisesRegex(TunnelError, "Another tunnel command"):
                with self.manager._locked():
                    self.fail("must not acquire twice")

    def test_unknown_socket_does_not_trigger_stop_or_pid_kill(self):
        record = self.manager.prepare(self.settings)
        self.manager._save([record])
        self.manager._path(record["id"], "sock").write_text("not a socket")
        with patch("subprocess.run") as run:
            with self.assertRaisesRegex(TunnelError, "No process was killed"):
                self.manager.stop(record["id"])
            run.assert_not_called()

    def test_missing_alias_and_changed_preview_never_launch(self):
        record = self.manager.prepare(Settings("missing", 8790, 18790))
        with patch("subprocess.Popen") as spawn:
            with self.assertRaisesRegex(TunnelError, "no longer"):
                self.manager.start(record)
            record = self.manager.prepare(self.settings)
            record["commands"][0].append("unexpected")
            with patch("tunnel_cli.core.available_port", return_value=True):
                with self.assertRaisesRegex(TunnelError, "changed after review"):
                    self.manager.start(record)
            spawn.assert_not_called()

    def test_startup_failure_records_failure_and_cleans_owned_attempt(self):
        record = self.manager.prepare(self.settings)
        process = Mock()
        process.poll.return_value = 255
        process.returncode = 255
        with patch("tunnel_cli.core.available_port", return_value=True), \
             patch("subprocess.Popen", return_value=process), \
             patch.object(self.manager, "_cleanup_attempt") as cleanup:
            with self.assertRaisesRegex(TunnelError, "SSH startup failed"):
                self.manager.start(record)
            cleanup.assert_called_once()
        stored = self.manager._read()[0]
        self.assertEqual(stored["status"], "Failed")
        self.assertFalse(stored["successful"])

    def test_cancel_during_startup_cleans_and_records_failure(self):
        record = self.manager.prepare(self.settings)
        with patch("tunnel_cli.core.available_port", return_value=True), \
             patch("subprocess.Popen"), \
             patch.object(self.manager, "_probe", side_effect=KeyboardInterrupt), \
             patch.object(self.manager, "_cleanup_attempt") as cleanup:
            with self.assertRaises(KeyboardInterrupt):
                self.manager.start(record)
            cleanup.assert_called_once()
        stored = self.manager._read()[0]
        self.assertEqual(stored["status"], "Failed")
        self.assertEqual(stored["last_error"], "Cancelled during startup.")

    def test_log_open_failure_records_reason_without_spawning(self):
        record = self.manager.prepare(self.settings)
        self.manager._path(record["id"], "log").write_text("existing")
        with patch("tunnel_cli.core.available_port", return_value=True), patch("subprocess.Popen") as spawn:
            with self.assertRaises(TunnelError):
                self.manager.start(record)
            spawn.assert_not_called()
        stored = self.manager._read()[0]
        self.assertEqual(stored["status"], "Failed")
        self.assertTrue(stored["last_error"])

    def test_persist_failure_after_forward_rolls_back_connection(self):
        record = self.manager.prepare(self.settings)
        process = Mock(returncode=0)
        process.poll.return_value = 0
        original_save = self.manager._save
        calls = 0

        def save(records):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            original_save(records)

        with patch("tunnel_cli.core.available_port", return_value=True), \
             patch("subprocess.Popen", return_value=process), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(self.manager, "_probe", return_value="Alive"), \
             patch.object(self.manager, "_save", side_effect=save), \
             patch.object(self.manager, "_cleanup_attempt") as cleanup:
            with self.assertRaisesRegex(TunnelError, "disk full"):
                self.manager.start(record)
            cleanup.assert_called_once()
        self.assertEqual(self.manager._read()[0]["status"], "Failed")

    def test_logs_are_trimmed_on_access(self):
        record = self.manager.prepare(self.settings)
        self.manager._save([record])
        path = self.manager._path(record["id"], "log")
        path.write_bytes(b"a" * 100000 + b"last line")
        path.chmod(0o600)
        self.assertTrue(self.manager.logs(record["id"]).endswith("last line"))
        self.assertEqual(path.stat().st_size, 65536)


if __name__ == "__main__":
    unittest.main()
