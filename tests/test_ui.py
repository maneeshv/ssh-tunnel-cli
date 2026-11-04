"""Prompt-layer tests: prompt_toolkit pipe input + DummyOutput, no keyboard,
no SSH, fixture aliases only. Every prompt run is bounded by SIGALRM."""

import contextlib
import io
import os
import signal
import unittest
from unittest.mock import patch

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import ColorDepth, DummyOutput

from tunnel_cli import prompts
from tunnel_cli.prompts import Back

ENTER = "\r"
ESC = "\x1b"
CTRL_C = "\x03"
DOWN = "\x1b[B"
BACKSPACE = "\x7f"

# Fixture aliases only; nothing here resolves or connects anywhere.
ALIASES = ["laptop", "other-mac", "web-server"]


class PromptTests(unittest.TestCase):
    def setUp(self):
        def timed_out(*_):
            raise AssertionError("prompt did not finish; test timed out")

        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(10)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(signal.alarm, 0)

    def run_prompt(self, keys, call, *args, **kwargs):
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return call(*args, input=pipe, output=DummyOutput(), **kwargs)

    def test_search_filters_and_enter_selects(self):
        result = self.run_prompt("mac" + ENTER, prompts.choose, "Machine", ALIASES, search=True)
        self.assertEqual(result, "other-mac")

    def test_enter_is_blocked_while_filter_matches_nothing(self):
        # Regression: upstream questionary falls back to all choices on a
        # no-match filter, so a bare Enter would submit the invisible
        # pointed-at "laptop". It must be ignored until the filter matches.
        keys = "zzz" + ENTER + BACKSPACE * 3 + "web" + ENTER
        result = self.run_prompt(keys, prompts.choose, "Machine", ALIASES, search=True)
        self.assertEqual(result, "web-server")

    def test_typed_text_is_never_the_answer(self):
        result = self.run_prompt("mac" + ENTER, prompts.choose, "Machine", ALIASES, search=True)
        self.assertIn(result, ALIASES)
        self.assertNotEqual(result, "mac")

    def test_arrow_keys_navigate_plain_menu(self):
        self.assertEqual(self.run_prompt(DOWN + ENTER, prompts.choose, "Menu", ALIASES), "other-mac")

    def test_default_preselects_previous_answer(self):
        result = self.run_prompt(ENTER, prompts.choose, "Menu", ALIASES, default="web-server")
        self.assertEqual(result, "web-server")

    def test_escape_raises_back_from_menu(self):
        with self.assertRaises(Back):
            self.run_prompt(ESC, prompts.choose, "Menu", ALIASES)

    def test_escape_raises_back_with_active_search_filter(self):
        with self.assertRaises(Back):
            self.run_prompt("ma" + ESC, prompts.choose, "Machine", ALIASES, search=True)

    def test_escape_raises_back_from_text(self):
        with self.assertRaises(Back):
            self.run_prompt("123" + ESC, prompts.text, "Port")

    def test_ctrl_c_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_prompt(CTRL_C, prompts.choose, "Menu", ALIASES)
        with self.assertRaises(KeyboardInterrupt):
            self.run_prompt(CTRL_C, prompts.text, "Port")

    def test_text_default_preserves_previous_answer(self):
        self.assertEqual(self.run_prompt(ENTER, prompts.text, "Local port", default="8790"), "8790")

    def test_invalid_input_keeps_prompt_editable_until_valid(self):
        def port(value):
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                raise ValueError("Enter a decimal port from 1 to 65535.")

        keys = "0" + ENTER + BACKSPACE + "8790" + ENTER
        result = self.run_prompt(keys, prompts.text, "Remote port", validate=port)
        self.assertEqual(result, "8790")

    def test_no_color_env_forces_monochrome_depth(self):
        with patch.dict(os.environ, {"NO_COLOR": ""}):  # empty value counts
            self.assertEqual(prompts._color(), {"color_depth": ColorDepth.DEPTH_1_BIT})
        environment = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(prompts._color(), {})

    def test_no_color_reaches_choose_and_text_applications(self):
        applications = []
        original = prompts._ask

        def spy(question, *extra):
            applications.append(question.application)
            return original(question, *extra)

        with patch.dict(os.environ, {"NO_COLOR": ""}), patch.object(prompts, "_ask", spy):
            self.run_prompt(ENTER, prompts.choose, "Menu", ALIASES)
            self.run_prompt(ENTER, prompts.text, "Port", default="8790")
        self.assertEqual(len(applications), 2)
        for application in applications:
            self.assertIs(application.color_depth, ColorDepth.DEPTH_1_BIT)

    def test_boolean_validator_is_supported(self):
        keys = "nope" + ENTER + BACKSPACE * 4 + "yes" + ENTER
        result = self.run_prompt(keys, prompts.text, "Word", validate=lambda value: value == "yes")
        self.assertEqual(result, "yes")


class SayTests(unittest.TestCase):
    def capture(self, message):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            prompts.say(message)
        return buffer.getvalue()

    def test_strips_csi_sequences_bell_and_carriage_return(self):
        self.assertEqual(self.capture("\x1b[2J\x1b[31mred\x1b[0m ok\x07\r"), "red ok\n")

    def test_strips_osc_title_injection_and_c1_controls(self):
        self.assertEqual(self.capture("\x1b]0;owned\x07safe\x9btail"), "safetail\n")

    def test_keeps_newlines_and_tabs(self):
        self.assertEqual(self.capture("a\tb\nc"), "a\tb\nc\n")


if __name__ == "__main__":
    unittest.main()
