# Python releases

PyPI publishing uses GitHub Actions OIDC trusted publishing, without a stored API token.

The PyPI publisher must match:

- Project: `ssh-tunnel-cli`
- Repository: `maneeshv/ssh-tunnel-cli`
- Workflow: `publish-pypi.yml`
- GitHub environment: `pypi`

Update the version in `pyproject.toml` and the CLI's `--version` output, run `uv lock`, verify the tests, and commit. Push an annotated matching `vX.Y.Z` tag. `.github/workflows/publish-pypi.yml` checks the version, tests, builds and validates both distributions, then publishes those artifacts from a separate job with `id-token: write`.

For a failed initial authentication attempt, finish the publisher configuration and rerun the failed workflow. To dispatch manually, select the release tag, not `main`. Published versions cannot be overwritten; use a new version for changes.

After release, verify `https://pypi.org/project/ssh-tunnel-cli/` and install from PyPI in an isolated environment before announcing it.

## Homebrew

The macOS formula is maintained in [maneeshv/homebrew-tap](https://github.com/maneeshv/homebrew-tap), at `Formula/ssh-tunnel-cli.rb`. It installs the published PyPI source distribution and checksummed dependencies into a private Homebrew virtual environment.

After a Python release, update the formula's source URL and SHA-256 from that release's PyPI metadata. Update dependency resources when their pinned versions change, and test the formula with `brew install --build-from-source`, `brew test`, `brew style`, and `brew audit --strict` before pushing the tap. No Homebrew Core submission is required for this independent tap.
