from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tunnel_cli.cli import main, run_wizard, manage
from tunnel_cli.config import Discovery
from tunnel_cli.core import Manager, Settings
from tunnel_cli.prompts import Back


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"alias": "example", "remote_port": 8790, "local_port": 18790,
                         "destination": "127.0.0.1", "mode": "background"}
        self.record = {"id": "a" * 12, "settings": self.settings, "created_at": "2026-11-04",
                       "status": "Running", "successful": True, "commands": [["ssh", "example"]]}
        self.manager = Mock(spec=Manager)
        self.manager.config_path = None
        self.manager.prepare.return_value = self.record
        self.manager.start.return_value = self.record
        self.manager.records.return_value = []
        self.output = patch("tunnel_cli.cli.say")
        self.output.start()
        self.addCleanup(self.output.stop)

    def test_cancel_review_never_starts(self):
        with patch("tunnel_cli.cli.choose", return_value="Cancel"):
            self.assertFalse(run_wizard(self.manager, self.settings))
        self.manager.start.assert_not_called()

    def test_edit_returns_to_review_and_never_starts_until_confirmed(self):
        with patch("tunnel_cli.cli.choose", side_effect=["Edit settings", 1, "Cancel"]), \
             patch("tunnel_cli.cli.text", return_value="9090"):
            self.assertFalse(run_wizard(self.manager, self.settings))
        self.manager.start.assert_not_called()
        self.assertEqual(self.manager.prepare.call_args.args[0].remote_port, 9090)

    def test_explicit_start_exactly_once(self):
        with patch("tunnel_cli.cli.choose", side_effect=["Start tunnel", "Return to shell"]):
            self.assertTrue(run_wizard(self.manager, self.settings))
        self.manager.start.assert_called_once()

    def test_escape_during_edit_preserves_answers(self):
        with patch("tunnel_cli.cli.choose", side_effect=["Edit settings", 1, "Cancel"]), \
             patch("tunnel_cli.cli.text", side_effect=Back):
            self.assertFalse(run_wizard(self.manager, self.settings))
        self.assertEqual(self.manager.prepare.call_args.args[0].remote_port, 8790)
        self.manager.start.assert_not_called()

    def test_escape_from_edit_picker_returns_directly_to_review(self):
        with patch("tunnel_cli.cli.choose", side_effect=["Edit settings", Back(), "Cancel"]) as choose:
            self.assertFalse(run_wizard(self.manager, self.settings))
        self.assertEqual([call.args[0] for call in choose.call_args_list], ["Review", "Edit which setting?", "Review"])
        self.manager.start.assert_not_called()

    def test_new_tunnel_back_preserves_remote_port(self):
        with patch("tunnel_cli.cli.discover_hosts", return_value=Discovery(["example"], [])), \
             patch("tunnel_cli.cli.choose", side_effect=["example", "background", "Cancel"]), \
             patch("tunnel_cli.cli.text", side_effect=["8790", Back(), "9090", "19090", "127.0.0.1"]) as prompt:
            self.assertFalse(run_wizard(self.manager))
        self.assertEqual(prompt.call_args_list[2].args[1], "8790")
        self.assertEqual(self.manager.prepare.call_args.args[0].remote_port, 9090)
        self.manager.start.assert_not_called()

    def test_existing_equivalent_route_opens_management(self):
        self.manager.records.return_value = [self.record]
        with patch("tunnel_cli.cli.choose", return_value="Start tunnel"), \
             patch("tunnel_cli.cli.manage", return_value=False) as manage_existing:
            run_wizard(self.manager, dict(self.settings, mode="foreground"))
        manage_existing.assert_called_once_with(self.manager, self.record["id"])
        self.manager.start.assert_not_called()

    def test_restart_cancel_never_stops(self):
        with patch("tunnel_cli.cli.choose", return_value="Cancel"):
            run_wizard(self.manager, self.settings, restart=self.record)
        self.manager.stop.assert_not_called()
        self.manager.start.assert_not_called()

    def test_restart_stops_only_after_review(self):
        self.manager.records.return_value = [self.record]
        with patch("tunnel_cli.cli.choose", side_effect=["Start tunnel", "Return to shell"]), \
             patch("tunnel_cli.cli.discover_hosts", return_value=Discovery(["example"], [])):
            run_wizard(self.manager, self.settings, restart=self.record)
        self.manager.stop.assert_called_once_with(self.record["id"])
        self.manager.start.assert_called_once()
        methods = [call[0] for call in self.manager.method_calls]
        self.assertLess(methods.index("stop"), methods.index("start"))

    def test_restart_missing_alias_keeps_existing_tunnel(self):
        self.manager.records.return_value = [self.record]
        with patch("tunnel_cli.cli.choose", side_effect=["Start tunnel", "Cancel"]), \
             patch("tunnel_cli.cli.discover_hosts", return_value=Discovery([], [])):
            run_wizard(self.manager, self.settings, restart=self.record)
        self.manager.stop.assert_not_called()
        self.manager.start.assert_not_called()

    def test_restart_occupied_replacement_port_keeps_existing_tunnel(self):
        self.manager.records.return_value = [self.record]
        with patch("tunnel_cli.cli.choose", side_effect=["Start tunnel", "Cancel"]), \
             patch("tunnel_cli.cli.available_port", return_value=False), \
             patch("tunnel_cli.cli.discover_hosts", return_value=Discovery(["example"], [])):
            run_wizard(self.manager, dict(self.settings, local_port=19000), restart=self.record)
        self.manager.stop.assert_not_called()
        self.manager.start.assert_not_called()

    def test_stop_confirmation_cancel_does_not_stop(self):
        self.manager.records.return_value = [self.record]
        with patch("tunnel_cli.cli.choose", return_value="Cancel"):
            manage(self.manager, self.record["id"], "stop")
        self.manager.stop.assert_not_called()

    def test_background_post_start_ctrl_c_preserves_connection(self):
        with patch("tunnel_cli.cli.choose", side_effect=["Start tunnel", KeyboardInterrupt()]):
            self.assertTrue(run_wizard(self.manager, self.settings))
        self.manager.start.assert_called_once()
        self.manager.stop.assert_not_called()


class EntryPointTests(unittest.TestCase):
    def test_noninteractive_list_uses_isolated_state(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as root, redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(Path(root) / "state"), "list"]), 0)
        self.assertIn("No managed tunnels", output.getvalue())

    def test_noninteractive_wizard_fails_before_state_or_manager_access(self):
        with patch("sys.stdin.isatty", return_value=False), patch("tunnel_cli.cli.Manager") as manager, redirect_stdout(StringIO()):
            self.assertEqual(main([]), 2)
        manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
