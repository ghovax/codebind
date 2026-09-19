# Publishing

Publish Models Provider before Codebind so PyPI can resolve Codebind's public dependency.

## One-time PyPI setup

Create a pending trusted publisher for the `codebind` project with these exact values:

- PyPI project: `codebind`
- GitHub owner: `ghovax`
- GitHub repository: `codebind`
- Workflow: `publish.yml`
- Environment: `pypi`

Create the `pypi` environment in the GitHub repository. No long-lived PyPI token is required.

## Release

After `models-provider>=0.1.0` is available from PyPI, verify registry-only resolution and release from a clean `main` checkout:

```console
uv version 0.1.0
uv lock --no-sources
uv build --no-sources
git tag -a v0.1.0 -m v0.1.0
git push origin v0.1.0
```

The tag runs `.github/workflows/publish.yml`, smoke-tests both distributions, generates attestations, and publishes through PyPI Trusted Publishing. After publication, verify the public command from any directory:

```console
uvx --refresh-package codebind codebind --version
uvx codebind
```
