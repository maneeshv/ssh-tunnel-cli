# tunnel

Create and manage local SSH tunnels without remembering forwarding syntax. Pick an alias from your SSH config, answer the prompts, review the exact commands, and choose **Start tunnel**.

Python + Questionary, using your Mac's existing OpenSSH client for connections and authentication. No remote agent or installed manager service.

## Install

Requires macOS, Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and `/usr/bin/ssh`.

```sh
uv tool install ssh-tunnel-cli
tunnel
```

Alternatively, use `pipx install ssh-tunnel-cli`, or `python -m pip install ssh-tunnel-cli` inside a virtual environment. The Python package exposes the same `tunnel` command.

If your shell cannot find `tunnel`, run `uv tool update-shell` and restart the shell.

To run or develop from a checkout:

```sh
git clone https://github.com/maneeshv/ssh-tunnel-cli.git
cd ssh-tunnel-cli
uv sync --locked
uv run tunnel
```

From that checkout, `uv tool install .` installs the command for everyday use.

Your config must already contain a literal alias, for example:

```sshconfig
Host other-mac
    HostName your-other-machine
    User your-username
```

Configure and verify SSH access yourself first. `tunnel` does not edit SSH config, set up Tailscale, or save credentials. Add hosts to your config manually and reload the picker.

## Use

- **New tunnel:** machine → remote app port → local port → destination → mode → review.
- **Background:** returns to the shell; SSH keeps running after that terminal closes.
- **Foreground:** remains attached; Ctrl+C stops this tunnel.
- **Manage tunnels:** inspect commands and diagnostics, stop, or review a restart.
- **Recent tunnels:** reuse successful settings, with a fresh review before connecting.
- **Esc:** back, retaining answers. **Ctrl+C:** cancel. Arrow keys select; type to filter the host picker.

```sh
tunnel list
tunnel stop
tunnel restart
tunnel --help
```

The app port is different from the SSH server port. SSH's server port comes from your configuration. The destination `127.0.0.1` means loopback **on the selected remote machine**; the local listener is always `127.0.0.1` **on this Mac**. No public/LAN bind option is provided.

**Running means SSH is active, not that the remote app is healthy.** Browser opening is optional and only appropriate for web apps. Tailscale is optional existing connectivity, not something this utility configures or guarantees.

## Connection ownership

Every attempt has its own private control socket. The reviewed launch has two commands: first start an isolated SSH connection with inherited forwards cleared, then add the one selected local forward through that socket. This avoids accidentally launching forwards defined elsewhere in SSH configuration. SSH continues to resolve keys, users, jump hosts, and authentication settings. LocalCommand, agent/X11 forwarding, tunnel devices, and persistent/shared control settings are overridden for this utility.

Only tool-owned connections appear in the inventory. Stop never searches by port or kills an arbitrary PID. Closing the menu does not stop a background tunnel. Network failures require a manual restart; no auto-start or reconnect service is installed.

Native SSH authentication and host-key prompts appear only after Start. User SSH config is trusted: connection configuration such as ProxyCommand or Match exec can execute its configured commands at connection time. Host discovery itself does not execute those directives.

## State and limitations

Records, a lock file, SSH control sockets, and diagnostics live in `~/.tunnel/`, with owner-only permissions. Override the location with `--state-dir`; use `--config` for a separate SSH config. A short state path is required by Unix control-socket limits. An unsafe or corrupt state store is reported and preserved, not silently repaired or discarded.

Host discovery supports literal ASCII aliases and ordinary nested/globbed Includes. Conditional Includes and paths containing SSH tokens or environment expansions are skipped with a warning that the list may be incomplete. Wildcard/negated Host patterns are not selectable machines. Relative user Includes resolve under `~/.ssh`, including when `--config` points elsewhere, matching OpenSSH.

Diagnostics contain SSH errors, not remote application logs or typed authentication input. Each log is trimmed to its last 64 KiB when viewed. Without a background manager, logs can grow between views. Old records and diagnostics remain in the private state directory; no automatic history deletion is performed.

Foreground/background lifecycle is macOS-first. Other operating systems and every OpenSSH configuration combination are not promised.

## Verify

Unit and prompt tests (no real hosts):

```sh
uv run python -m unittest discover -s tests -v
```

Opt-in integration tests start an isolated loopback `sshd`, generate disposable keys, and exercise actual forwarding. They do not read your SSH keys or configuration, change system SSH settings, or contact remote machines. They require permission to bind/connect loopback ports, `/usr/sbin/sshd`, `/usr/bin/ssh-keygen`, and `trash` for fixture cleanup.

```sh
TUNNEL_SSH_INTEGRATION=1 uv run python -m unittest tests.test_ssh_integration -v
```

See [the specification](https://github.com/maneeshv/ssh-tunnel-cli/blob/main/SPEC.md) for requirements, decisions, and the [original Warp mockup](https://github.com/maneeshv/ssh-tunnel-cli/blob/main/design/tunnel-mockup.png). The mockup is a design reference, not a screenshot of the implemented terminal UI.

Verified on 2026-11-04 with Python 3.11.11 and macOS OpenSSH 10.3p1: all 73 tests passed, including 12 real-SSH integration tests. The wheel and source archive build successfully, and the wheel's CLI runs independently of the source checkout. Menus were inspected in a 96-column terminal with color disabled. Manual Warp rendering and password/passphrase authentication were not checked; generated-key authentication and native host-key confirmation were exercised through a PTY.

## TypeScript comparison

A separate [TypeScript alternative](https://github.com/maneeshv/ssh-tunnel-cli/tree/main/typescript-version) is available for comparison. Run `npm ci && npm run build && npm start` from `typescript-version/`. It uses the `tunnel-ts` command and its own state directory; npm publication is pending evaluation.

## Rust comparison

A separate [Rust alternative](https://github.com/maneeshv/ssh-tunnel-cli/tree/main/rust-version) builds a standalone `tunnel-rs` executable with its own state directory. From the repository root, run:

```sh
cargo run --locked --manifest-path rust-version/Cargo.toml
```

Requires macOS and Rust 1.89 or later to build. Crates.io publication is pending evaluation.

## Contributing

Small fixes and improvements are welcome. Open an issue for substantial behavior changes before implementing them. Use absolute imports, keep dependencies minimal, and run the checks above before submitting a pull request. Include a regression test for behavior changes. CI runs unit/prompt tests and package smoke checks on macOS; real-SSH tests remain opt-in.

When reporting a bug, include the macOS, Python, and OpenSSH versions and sanitized reproduction steps. Remove hostnames, usernames, addresses, credentials, and private configuration from pasted diagnostics.

## License

[MIT](https://github.com/maneeshv/ssh-tunnel-cli/blob/main/LICENSE). The mockup is an AI-generated design reference; the project is not affiliated with Warp.
