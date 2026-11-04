"""Guided setup and management, with no connection before a reviewed Start."""

import argparse
from pathlib import Path
import shlex
import shutil
import sys
import textwrap
import webbrowser

from questionary import Choice

from tunnel_cli.config import discover_hosts
from tunnel_cli.core import Manager, Settings, TunnelError, available_port, validate_destination, validate_port
from tunnel_cli.prompts import Back, choose, say, text


def label(record: dict) -> str:
    s = record["settings"]
    return f"{s['alias']}  127.0.0.1:{s['local_port']} → {s['destination']}:{s['remote_port']}  {record['status']}"


def show_commands(record: dict):
    say("\nExact commands — private SSH connection, then the selected forward:")
    width = max(40, shutil.get_terminal_size().columns - 4)
    for index, command in enumerate(record["commands"], 1):
        say(textwrap.fill(shlex.join(command), width=width,
                          initial_indent=f"  {index}. ", subsequent_indent="     ",
                          break_long_words=False, break_on_hyphens=False))


def show_details(record: dict):
    say(f"\n{label(record)}\nID: {record['id']}\nMode: {record['settings']['mode']}\nCreated: {record['created_at']}")
    if record.get("last_error"):
        say(f"Last diagnostic: {record['last_error']}")
    say("Running describes the SSH tunnel; application health is not checked.")


def _host(manager: Manager, default: str | None):
    while True:
        discovery = discover_hosts(manager.config_path)
        for warning in discovery.warnings:
            say(f"Warning: {warning}")
        if discovery.aliases:
            return choose("Choose a machine", discovery.aliases,
                          default=default if default in discovery.aliases else None, search=True)
        say("No literal SSH aliases found. Add a Host alias to your SSH config manually, then reload.")
        if choose("Next", ["Reload hosts", "Back"]) == "Back":
            raise Back


def same_route(first: dict, second: dict) -> bool:
    return all(first[key] == second[key] for key in ("alias", "local_port", "remote_port", "destination"))


def _started(record: dict):
    settings = record["settings"]
    say(f"\nTunnel running in {settings['mode']}: 127.0.0.1:{settings['local_port']}\nID: {record['id']}")
    say("Tunnel active; application health not checked.")
    if settings["mode"] == "foreground":
        say("Ctrl+C stops this tunnel.")


def run_wizard(manager: Manager, initial: dict | None = None, restart: dict | None = None) -> bool:
    answers = dict(initial or {})
    step = 5 if initial else 0
    editing = False
    fields = ["Machine", "Remote port", "Local port", "Destination", "Run mode"]
    while True:
        try:
            if step == 0:
                answers["alias"] = _host(manager, answers.get("alias"))
            elif step == 1:
                say("Remote application port — the SSH server port comes from SSH config.")
                answers["remote_port"] = validate_port(text("Remote port", str(answers.get("remote_port", "")),
                                                            validate=validate_port))
            elif step == 2:
                def local_port(value):
                    port = validate_port(value)
                    owned_port = restart and restart["status"] == "Running" and restart["settings"]["local_port"] == port
                    if not owned_port and not available_port(port):
                        raise ValueError(f"Port {port} is unavailable. Enter another local port.")
                    return port
                answers["local_port"] = validate_port(text("Local port", str(answers.get("local_port", answers["remote_port"])),
                                                           validate=local_port))
            elif step == 3:
                say("127.0.0.1 means the selected remote machine. Advanced: another host/IP reachable from it.")
                answers["destination"] = validate_destination(text("Destination", answers.get("destination", "127.0.0.1"),
                                                                   validate=validate_destination))
            elif step == 4:
                say("Background returns to your shell. Foreground stays attached until Ctrl+C.")
                answers["mode"] = choose("How should it run?", [Choice("Background", "background"), Choice("Foreground", "foreground")],
                                         default=answers.get("mode", "background"))
            else:
                record = manager.prepare(Settings(**answers))
                say("\nReady to start")
                say(f"  Machine       {answers['alias']}\n  Local         127.0.0.1:{answers['local_port']}"
                    f"\n  Destination   {answers['destination']}:{answers['remote_port']} on {answers['alias']}"
                    f"\n  Mode          {answers['mode'].capitalize()}")
                if restart:
                    say(f"Restart will stop tunnel {restart['id']} before starting this route. Existing connections will be interrupted.")
                show_commands(record)
                action = choose("Review", ["Start tunnel", "Edit settings", "Cancel"])
                if action == "Cancel":
                    return False
                if action == "Edit settings":
                    editing = True
                    step = choose("Edit which setting?", [Choice(name, index) for index, name in enumerate(fields)])
                    continue
                current = manager.records()
                for existing in current:
                    if existing["status"] == "Running" and same_route(existing["settings"], answers) and (not restart or existing["id"] != restart["id"]):
                        say("That route is already running. Opening its management actions.")
                        return manage(manager, existing["id"])
                if restart:
                    if answers["alias"] not in discover_hosts(manager.config_path).aliases:
                        raise TunnelError("Selected alias is no longer discoverable. Edit Machine before restarting; existing tunnel was left running.")
                    original = next((item for item in current if item["id"] == restart["id"]), None)
                    owns_port = original and original["status"] == "Running" and original["settings"]["local_port"] == answers["local_port"]
                    if not owns_port and not available_port(answers["local_port"]):
                        raise TunnelError("Replacement port is unavailable. Edit Local port; existing tunnel was left running.")
                    manager.stop(restart["id"])
                    restart = None
                say(f"Connecting to {answers['alias']}… SSH may request authentication.")
                result = manager.start(record, on_started=_started)
                if answers["mode"] == "foreground":
                    say(f"Tunnel {result['status'].lower()}.")
                else:
                    try:
                        action = choose("Next", ["Return to shell", "Open as HTTP website", "Open as HTTPS website"])
                        if action != "Return to shell":
                            scheme = "https" if action == "Open as HTTPS website" else "http"
                            if not webbrowser.open(f"{scheme}://127.0.0.1:{answers['local_port']}"):
                                say("Could not open the browser. Open the local address manually.")
                    except (Back, KeyboardInterrupt):
                        say("Tunnel remains running in the background.")
                return True
            step = 5 if editing else step + 1
            editing = False
        except Back:
            if editing:
                step, editing = 5, False
            elif step == 0:
                return False
            else:
                step -= 1
        except (TunnelError, OSError, ValueError) as exc:
            say(f"Could not start tunnel: {exc}")
            # Review is rebuilt for every attempt, so retry cannot reuse a reserved ID.


def manage(manager: Manager, tunnel_id: str | None = None, action: str | None = None) -> bool:
    while True:
        try:
            records = manager.records()
            if not records:
                say("No managed tunnels yet.")
                return False
            if tunnel_id is None:
                tunnel_id = choose("Manage tunnels", [Choice(label(item), item["id"]) for item in records] + [Choice("Back", "back")])
                if tunnel_id == "back":
                    return False
            record = next((item for item in records if item["id"] == tunnel_id), None)
            if record is None:
                raise TunnelError("Tunnel not found. Use tunnel list for managed IDs.")
            selected = action or choose(label(record), ["Details", "Show command", "View logs", "Stop tunnel", "Restart tunnel", "Back"])
            if selected == "Details":
                show_details(record)
            elif selected == "Show command":
                show_commands(record)
            elif selected == "View logs":
                say(manager.logs(tunnel_id))
            elif selected in ("Stop tunnel", "stop"):
                if choose(f"Stop {record['settings']['alias']} on local port {record['settings']['local_port']}?", ["Cancel", "Stop tunnel"]) == "Stop tunnel":
                    manager.stop(tunnel_id)
                    say("Tunnel stopped.")
            elif selected in ("Restart tunnel", "restart"):
                return run_wizard(manager, record["settings"], restart=record)
            else:
                return False
            if action:
                return False
        except Back:
            return False


def recent(manager: Manager) -> bool:
    records = [item for item in reversed(manager.records()) if item["successful"]]
    unique = {}
    for item in records:
        unique.setdefault(tuple(sorted(item["settings"].items())), item)
    if not unique:
        say("No successful tunnels to reuse yet.")
        return False
    try:
        item = choose("Recent tunnels", [Choice(label(record), record) for record in unique.values()])
        if item["status"] == "Running":
            return manage(manager, item["id"])
        return run_wizard(manager, item["settings"])
    except Back:
        return False


def home(manager: Manager):
    while True:
        try:
            count = sum(item["status"] == "Running" for item in manager.records())
            choice = choose("What would you like to do?", ["New tunnel", f"Manage tunnels · {count} running", "Recent tunnels", "Exit"])
            if choice == "Exit":
                return
            if choice == "New tunnel":
                finished = run_wizard(manager)
            elif choice == "Recent tunnels":
                finished = recent(manager)
            else:
                finished = manage(manager)
            if finished:
                return
        except Back:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guided local SSH tunnels using existing config aliases.")
    parser.add_argument("--config", type=Path, help="SSH config to use (default: ~/.ssh/config)")
    parser.add_argument("--state-dir", type=Path, help="Private, short state directory (default: ~/.tunnel)")
    parser.add_argument("--version", action="version", version="tunnel 0.1.1")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="Print owned tunnel status without prompting")
    for name in ("stop", "restart"):
        command = commands.add_parser(name, help=f"Interactively {name} a managed tunnel")
        command.add_argument("id", nargs="?", help="Tunnel ID; omit to select interactively")
    args = parser.parse_args(argv)
    if args.command != "list" and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        say("Interactive commands need a terminal. Use tunnel list for noninteractive status.")
        return 2
    try:
        manager = Manager(args.config, args.state_dir)
        if args.command == "list":
            records = manager.records()
            if not records:
                say("No managed tunnels.")
            else:
                say("ID            MACHINE / LOCAL → DESTINATION / STATUS")
                for record in records:
                    say(f"{record['id']}  {label(record)}")
        elif args.command:
            manage(manager, args.id, args.command)
        else:
            home(manager)
        return 0
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 130
    except Back:
        return 0
    except (TunnelError, OSError, ValueError) as exc:
        say(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
