# Releasing

Releases are cut from `main` by pushing a tag. `.github/workflows/release.yml`
builds the sdist and wheel, checks the tag matches `pyproject.toml`, attests
build provenance, publishes to PyPI with trusted publishing (no API token is
stored anywhere), and creates the GitHub release with the changelog entry and
`requirements.lock`.

## One-time setup

1. On PyPI, add a *trusted publisher* for the project `devflow-bot`
   (a "pending publisher" before the first release): owner `fadjardev`,
   repository `devflow`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository settings, create the environment `pypi`, and
   limit it to tags matching `v*` (and require a reviewer if you want a manual
   approval before each upload).

## Each release

1. On a branch: set `version` in `pyproject.toml`, move the *Unreleased*
   entries in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD`, and refresh the
   lock file:

   ```sh
   uv pip compile --universal --generate-hashes --python-version 3.11 pyproject.toml -o requirements.lock
   ```

2. Merge it into `main` once CI is green.
3. Tag and push:

   ```sh
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "devflow X.Y.Z"
   git push origin vX.Y.Z
   ```

4. Check the release page and `pipx install devflow-bot==X.Y.Z`.

Verify a downloaded artifact's provenance with
`gh attestation verify <file> --repo fadjardev/devflow`.
