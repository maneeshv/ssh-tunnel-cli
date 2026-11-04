# tunnel-ts

A TypeScript alternative to the Python [`tunnel`](https://github.com/maneeshv/ssh-tunnel-cli) CLI, built for a side-by-side comparison. It uses Inquirer prompts and native OpenSSH. There is no Python runtime dependency.

Requires macOS, Node.js **22.17 or later**, npm, and `/usr/bin/ssh`. This comparison package is **not published to npm yet**; the provisional package name is `ssh-tunnel-cli-ts`.

## Try it

From this repository:

```sh
cd typescript-version
npm ci
npm run build
npm start
```

Or install the packaged command locally on your machine:

```sh
npm pack
npm install --global ./ssh-tunnel-cli-ts-0.1.0.tgz
tunnel-ts
```

The executable is `tunnel-ts`, so installing it does not replace the Python `tunnel` command. State lives in `~/.tunnel-ts/`; it does not import Python's `~/.tunnel/` records. Each version manages its own connections. Stop an existing tunnel or choose a different local port before testing the same route in the other version.

```sh
tunnel-ts list
tunnel-ts stop
tunnel-ts restart
tunnel-ts --help
```

## Compare the experience

- Run `tunnel`, then `tunnel-ts`, using the same SSH alias and remote application.
- Type to filter the machine picker. Check Esc/back, defaults, invalid ports, editing, and the exact command review.
- Start in background, close that terminal, and check the local application and management menu from a new terminal.
- Stop it, then try foreground and Ctrl+C. Check that unrelated SSH sessions stay connected.
- Reuse recent settings, inspect diagnostics, and confirm a restart.

Both implementations bind only `127.0.0.1`, require an explicit Start, let native SSH handle authentication, and create a private control socket per attempt. They clear inherited forwards before adding the reviewed route. Running describes the SSH connection, not the health of the remote app. Browser opening is explicit.

## Differences and limits

- Python uses Questionary; TypeScript uses Inquirer. Both implement the same prompt sequence, including Esc/back and type-to-filter.
- TypeScript uses an exclusive lockfile rather than Python's OS flock. If the process is forcibly killed, verify that no `tunnel-ts` command is running before moving its stale `~/.tunnel-ts/lock` to Trash. Never remove another running command's lock.
- Discovery skips conditional/dynamic Includes and named-user `~someone/` paths with a warning. Ordinary `~/`, relative, nested, and globbed Includes are supported. Existing SSH configuration is trusted and may run ProxyCommand or Match exec when you explicitly connect.
- Diagnostic logs are trimmed to 64 KiB when viewed and may grow between views. There is no auto-reconnect or installed background service.
- Only macOS is supported for this comparison. Password/passphrase prompts and real sleep/network-loss behavior still need manual testing.

## Verify

```sh
npm test
npm run test:ssh
npm pack --dry-run
```

The opt-in SSH integration suite uses Python only as a test harness shared with the Python version. It needs Python 3, `trash`, `/usr/sbin/sshd`, and permission to bind loopback ports. It generates its own SSH keys/config in temporary directories, never contacting existing hosts or reading your keys. Runtime installation includes only compiled TypeScript, package metadata, this README, and the MIT license.

Verified locally on macOS with Node.js 25.6.1: 51 unit/prompt tests and 10 real-SSH/PTY integration tests pass. The npm tarball installs into an isolated directory and its executable runs independently of the source checkout.

## Publishing later

After the comparison and a final package-name/account check:

```sh
npm ci
npm test
npm run test:ssh
npm pack --dry-run
npm publish --access public
```

No npm publish is performed by the build, tests, or pack scripts.

## License

MIT, see [LICENSE](LICENSE).
