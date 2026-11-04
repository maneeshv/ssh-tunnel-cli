import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnel_cli.config import discover_hosts, is_alias


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.root = self.home / ".ssh"
        self.root.mkdir()
        home = patch.dict("os.environ", {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
        self.config = self.root / "config"

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_literal_aliases_comments_quotes_equals_and_order(self):
        self.write("config", '''
            # Host ignored
            hOsT = "first" second *.example !excluded first
            Host third # comment
            Host=second fourth
            Host -option bad;command "two words" user@host
        ''')
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["first", "second", "third", "fourth"])
        self.assertEqual(len(result.warnings), 4)

    def test_alias_validation(self):
        for value in ["host", "mac-2", "dev.example", "_alias", "123"]:
            self.assertTrue(is_alias(value), value)
        for value in ["", "-oProxyCommand=exec", "a\nb", "a b", "a/b", "a:b", "a;b", "a$()", "*", "!host", "[host]", "höst"]:
            self.assertFalse(is_alias(value), value)

    def test_nested_glob_and_quoted_relative_includes(self):
        self.write("config", 'Include "parts with spaces/*.conf"\nHost end\n')
        self.write("parts with spaces/02.conf", "Host second first\n")
        self.write("parts with spaces/01.conf", "Include nested/one\nHost first\nHost *\n")
        self.write("nested/one", "Host nested\nHost *\n")
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["nested", "first", "second", "end"])
        self.assertEqual(result.warnings, [])

    def test_tilde_and_default_path(self):
        self.write("config", 'Include "~/extra config"\n')
        (self.home / "extra config").write_text("Host home-host\n", encoding="utf-8")
        self.assertEqual(discover_hosts().aliases, ["home-host"])

    def test_custom_config_relative_include_still_uses_user_ssh_directory(self):
        self.write("included.conf", "Host from-user-ssh\n")
        project = self.home / "project"
        project.mkdir()
        custom_config = project / "custom.conf"
        custom_config.write_text("Include included.conf\n", encoding="utf-8")
        (project / "included.conf").write_text("Host wrong-directory\n", encoding="utf-8")
        result = discover_hosts(custom_config)
        self.assertEqual(result.aliases, ["from-user-ssh"])
        self.assertEqual(result.warnings, [])

    def test_cycles_missing_files_and_depth_are_warnings(self):
        self.write("config", "Include config missing*.conf\nInclude chain/0\nHost usable\n")
        for number in range(18):
            self.write(f"chain/{number}", f"Include chain/{number + 1}\n")
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["usable"])
        self.assertTrue(any("cycle" in warning for warning in result.warnings))
        self.assertTrue(any("matched no files" in warning for warning in result.warnings))
        self.assertTrue(any("depth limit" in warning for warning in result.warnings))

    def test_conditional_includes_and_match_exec_are_not_evaluated(self):
        self.write("config", '''
            Host selected
            Include hidden
            Match exec "touch NEVER_EXECUTE"
            Include hidden
            Host * !excluded
            Include hidden
            Match all
            Include visible
        ''')
        self.write("hidden", "Host hidden-host\n")
        self.write("visible", "Host visible-host\n")
        with patch("subprocess.run", side_effect=AssertionError("must not execute commands")):
            result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["selected", "visible-host"])
        self.assertEqual(len(result.warnings), 3)
        self.assertTrue(all("Conditional Include" in warning for warning in result.warnings))

    def test_included_host_context_propagates_to_parent(self):
        self.write("config", "Include child\nInclude hidden\nHost *\nInclude visible\n")
        self.write("child", "Host child-host\n")
        self.write("hidden", "Host hidden-host\n")
        self.write("visible", "Host visible-host\n")
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["child-host", "visible-host"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("Conditional Include", result.warnings[0])

    def test_unconditional_include_context_can_return_to_parent(self):
        self.write("config", "Include child\nInclude visible\n")
        self.write("child", "Host child-host\nHost *\n")
        self.write("visible", "Host visible-host\n")
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["child-host", "visible-host"])
        self.assertEqual(result.warnings, [])

    def test_bad_syntax_and_dynamic_paths_are_reported(self):
        self.write("config", 'Include ${CONFIG} %h.conf\nHost "unterminated\nInclude hidden\nHost valid\n')
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, ["valid"])
        self.assertEqual(len(result.warnings), 4)

    def test_unreadable_or_missing_config_is_reported(self):
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, [])
        self.assertIn("Cannot read SSH config", result.warnings[0])
        self.config.write_bytes(b"\xff")
        result = discover_hosts(self.config)
        self.assertEqual(result.aliases, [])
        self.assertIn("Cannot read SSH config", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
