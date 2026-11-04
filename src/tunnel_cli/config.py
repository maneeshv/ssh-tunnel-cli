"""Discover explicit SSH aliases without evaluating SSH conditions or commands."""

import glob
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Discovery:
    aliases: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_alias(value: str) -> bool:
    """Accept literal aliases that cannot be interpreted as SSH options."""
    return re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) is not None


def discover_hosts(config_path: Path | None = None) -> Discovery:
    """Read aliases, conservatively skipping conditional and dynamic Includes.

    Relative user Includes use ~/.ssh, including with a custom config path.
    Included Host/Match directives affect the following parent-file lines.
    This is a discovery reader, not an evaluator of OpenSSH configuration.
    """
    config_path = config_path or Path.home() / ".ssh" / "config"
    base = Path.home() / ".ssh"
    result = Discovery()
    seen: set[str] = set()
    active: set[Path] = set()

    def read(path: Path, unconditional: bool, depth: int) -> bool:
        try:
            path = path.resolve()
            if path in active:
                result.warnings.append(f"{path}: Include cycle skipped; host list may be incomplete.")
                return unconditional
            if depth > 16:
                result.warnings.append(f"{path}: Include depth limit reached; host list may be incomplete.")
                return unconditional
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError, RuntimeError) as exc:
            result.warnings.append(f"{path}: Cannot read SSH config ({exc}); host list may be incomplete.")
            return unconditional

        active.add(path)
        try:
            for number, line in enumerate(lines, 1):
                match = re.match(r"\s*([^\s=#]+)(?:\s*=\s*|\s+)?(.*)", line)
                if not match:
                    continue
                keyword, arguments = match.groups()
                keyword = keyword.lower()
                if keyword not in {"host", "match", "include"}:
                    continue
                location = f"{path}:{number}"
                try:
                    values = shlex.split(arguments, comments=True)
                    if not values:
                        raise ValueError("missing argument")
                except ValueError as exc:
                    result.warnings.append(f"{location}: Invalid {keyword} ({exc}); host list may be incomplete.")
                    if keyword in {"host", "match"}:
                        unconditional = False
                    continue

                if keyword == "host":
                    unconditional = values == ["*"]
                    for alias in values:
                        if any(character in alias for character in "*?!"):
                            continue
                        if not is_alias(alias):
                            result.warnings.append(f"{location}: Unsupported alias {alias!r} skipped.")
                        elif alias not in seen:
                            seen.add(alias)
                            result.aliases.append(alias)
                elif keyword == "match":
                    unconditional = len(values) == 1 and values[0].lower() == "all"
                elif not unconditional:
                    result.warnings.append(f"{location}: Conditional Include skipped; host list may be incomplete.")
                else:
                    for value in values:
                        if "$" in value or "%" in value:
                            result.warnings.append(f"{location}: Dynamic Include {value!r} skipped; host list may be incomplete.")
                            continue
                        try:
                            pattern = Path(value).expanduser()
                        except (KeyError, RuntimeError) as exc:
                            result.warnings.append(f"{location}: Cannot expand Include {value!r} ({exc}).")
                            continue
                        if not pattern.is_absolute():
                            pattern = base / pattern
                        paths = sorted(glob.glob(str(pattern)))
                        if not paths:
                            result.warnings.append(f"{location}: Include {value!r} matched no files; host list may be incomplete.")
                        for included in paths:
                            unconditional = read(Path(included), unconditional, depth + 1)
        finally:
            active.remove(path)
        return unconditional

    read(config_path.expanduser(), True, 0)
    return result
