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

The TypeScript comparison has its own package and no automatic npm publishing workflow. See [typescript-version/README.md](typescript-version/README.md).
