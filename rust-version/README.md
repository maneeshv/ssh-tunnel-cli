# tunnel-rs

A Rust alternative to the Python and TypeScript SSH tunnel CLIs in this repository. It uses Inquire prompts and native OpenSSH, and compiles to a single executable with no Python or Node runtime dependency.

Requires macOS, system OpenSSH, and Rust **1.89 or later** to build. This is a comparison build; it has not been published to crates.io.

## Try it

From the **repository root**:

```sh
cargo run --locked --manifest-path rust-version/Cargo.toml
```

Or from `rust-version/`:

```sh
cargo run --locked
```

Install the standalone command locally, from the repository root:

```sh
cargo install --locked --path rust-version
tunnel-rs
```

The executable is `tunnel-rs` and state lives in `~/.tunnel-rs/`. It does not import or manage Python's `~/.tunnel/` or TypeScript's `~/.tunnel-ts/` records. Stop a tunnel in its original version before comparing the same local port, or choose a different local port.

```sh
tunnel-rs list
tunnel-rs stop
tunnel-rs restart
tunnel-rs --help
```

Before installation, pass arguments after `--`, for example `cargo run --locked --manifest-path rust-version/Cargo.toml -- list`.

## Compare the experience

- Select an SSH alias by typing to filter. Esc goes back while retaining answers; Ctrl+C cancels.
- Enter remote and local ports, destination, and background or foreground mode. Review the exact commands before selecting Start.
- Try editing settings, invalid inputs, recent routes, and the management menu.
- Start in background, close the terminal, then inspect and stop the tunnel from a new terminal.
- Start in foreground, then press Ctrl+C. Unrelated SSH sessions should stay connected.

All three versions bind only `127.0.0.1`, give authentication to native SSH after Start, and use a private control socket for every connection. Inherited forwards are cleared before adding the reviewed forward. Running describes the SSH connection, not the remote application's health. Browser opening requires an explicit choice.

## State and limits

- Rust uses an OS file lock, released when the process exits, including after a crash. The `lock` file may remain on disk; its existence does not mean it is locked.
- Private records and diagnostic files use owner-only permissions. Unsafe or corrupt records are reported and preserved. Stop uses the owned control socket; it never searches for or kills another process by PID or port.
- Conditional/dynamic Includes and named-user `~someone/` paths are skipped with warnings. Ordinary `~/`, relative, nested, and globbed Includes are supported. Invalid glob syntax is reported. SSH configuration remains trusted and may execute configured ProxyCommand or Match exec commands when connecting.
- Diagnostic logs are trimmed to 64 KiB when viewed and may grow between views. There is no automatic reconnect or background management service.
- macOS is the supported platform. Password/passphrase authentication and real sleep/network-loss behavior require manual testing.

## Verify

From the repository root:

```sh
cargo fmt --manifest-path rust-version/Cargo.toml --check
cargo clippy --locked --manifest-path rust-version/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path rust-version/Cargo.toml
cargo build --locked --manifest-path rust-version/Cargo.toml --bins --examples
TUNNEL_SSH_INTEGRATION=1 uv run python rust-version/tests/test_ssh_integration.py -v
cargo package --locked --manifest-path rust-version/Cargo.toml --list
```

The opt-in SSH tests use Python only to reuse this repository's isolated test fixture and control PTYs. They generate disposable keys/config, bind high loopback ports, and require `/usr/sbin/sshd` and `trash`. They do not read your keys/config or connect to real hosts. Python is not a runtime dependency of `tunnel-rs`.

Verified on macOS with Rust 1.89.0: 37 unit tests and 15 SSH/PTY tests pass. Formatting and Clippy checks pass, the crate archive builds independently, and an isolated installation of the release binary runs with only macOS system libraries. The main menu and filtered host picker were also inspected in a 96-column terminal with color disabled.

`publish = false` prevents accidental crates.io publication while you evaluate this version. The package name `ssh-tunnel-cli-rs` is provisional. A future release needs an account/name check and removal of that guard after the comparison.

## License

MIT, see [LICENSE](LICENSE).
