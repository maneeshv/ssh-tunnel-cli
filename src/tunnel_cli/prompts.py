"""Small prompt layer over Questionary: Esc raises Back, search never submits
an unmatched host, and say() prints untrusted text without control sequences."""

import os
import re

import questionary
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator
from questionary.prompts.common import InquirerControl

_HINT = "Esc back · Ctrl+C cancel"


class Back(Exception):
    """The user pressed Esc to go back one step; collected answers stay valid."""


_UNSAFE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (title/hyperlink injection)
    r"|\x1b\[[0-?]*[ -/]*[@-~]?"  # CSI sequences
    r"|\x1b.?"  # any other escape, even truncated
    r"|[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]"  # C0 except \t\n, DEL, C1
)


def say(message: str = "") -> None:
    """Print possibly untrusted text (SSH diagnostics, config warnings) with
    terminal control characters removed; newlines and tabs survive."""
    print(_UNSAFE.sub("", str(message)))


def _style() -> Style:
    return Style(
        [("qmark", "fg:ansicyan"), ("pointer", "fg:ansicyan"),
         ("highlighted", "fg:ansicyan"), ("answer", "fg:ansicyan")]
    )


def _color() -> dict:
    """NO_COLOR present (even empty) forces monochrome rendering. A Style([])
    would merge with Questionary's defaults and not suppress colors; the
    application's color depth does."""
    return {"color_depth": ColorDepth.DEPTH_1_BIT} if "NO_COLOR" in os.environ else {}


class _Checked(Validator):
    """Adapt a callable that raises ValueError (or returns False) for invalid text."""

    def __init__(self, check):
        self._check = check

    def validate(self, document):
        try:
            ok = self._check(document.text)
        except ValueError as error:
            raise ValidationError(message=str(error), cursor_position=len(document.text)) from None
        if ok is False:
            raise ValidationError(message="Invalid input.", cursor_position=len(document.text))


def _ask(question, *extra_bindings):
    """Run a questionary question with a shared Esc-goes-back binding."""
    application = question.application
    back = KeyBindings()

    @back.add("escape", eager=True)
    def _back(event):
        event.app.exit(exception=Back())

    application.key_bindings = merge_key_bindings(
        [bindings for bindings in (application.key_bindings, back, *extra_bindings) if bindings]
    )
    application.ttimeoutlen = 0.1  # resolve a lone Esc quickly
    return question.unsafe_ask()


def choose(message: str, choices: list, default=None, search: bool = False, **io):
    """Arrow-key menu. With search=True, typing filters the choices and Enter is
    ignored while the filter matches nothing, so an invisible choice can never
    be submitted and typed text is never the answer. Raises Back on Esc."""
    hint = ("type to filter · " if search else "") + f"↑↓ navigate · Enter select · {_HINT}"
    question = questionary.select(
        message, choices=choices, default=default, style=_style(),
        instruction=f"({hint})",
        use_search_filter=search, use_jk_keys=False, use_shortcuts=False,
        **_color(), **io,
    )
    extra = []
    if search:
        control = next(
            window.content
            for window in question.application.layout.find_all_windows()
            if isinstance(window.content, InquirerControl)
        )

        @Condition
        def unmatched():
            # shortcut: pinned Questionary 2.1.1 control internals, recheck on upgrade.
            needle = (control.search_filter or "").lower()
            return bool(needle) and not any(
                needle in choice.title.lower() for choice in control.choices
            )

        block = KeyBindings()
        block.add("enter", eager=True, filter=unmatched)(lambda event: None)
        extra.append(block)
    return _ask(question, *extra)


def text(message: str, default: str = "", validate=None, **io) -> str:
    """One-line answer with optional validation; Esc raises Back."""
    question = questionary.text(
        message, default=default, style=_style(), instruction=f"({_HINT}) ",
        validate=_Checked(validate) if validate else None, **_color(), **io,
    )
    return _ask(question)
